#!/usr/bin/env python3
"""Cell-isolated multi-tenant workload service for Cloud Run and GKE.

Supports:
- Cell-scoped Spanner reads/writes when SPANNER_INSTANCE and SPANNER_DATABASE are set
  (auto-discovers GOOGLE_CLOUD_PROJECT via environment or Cloud Run metadata server, and
  falls back cleanly to an in-memory cell store for local blast-radius chaos testing).
- Strict server-side GCP Project ID obfuscation (exposes only first 3 characters + mask).
- Spanner-backed per-cell chaos state (`__cell_health__` row with 180s auto-heal TTL) so
  poison-pill faults apply consistently across autoscaled Cloud Run instances within a cell
  without crossing physical Spanner cell boundaries.
- Interactive visual topology & browser-triggered chaos studio at `GET /`.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

CELL_ID = os.environ.get("CELL_ID", "cell-00")
REGION = os.environ.get("CELL_REGION", "us-central1")
SPANNER_INSTANCE = os.environ.get("SPANNER_INSTANCE", "")
SPANNER_DATABASE = os.environ.get("SPANNER_DATABASE", "cell_ledger")
APP_VERSION = os.environ.get("APP_VERSION", "v1.4.2")
REVISION_ID = os.environ.get("K_REVISION", f"{CELL_ID}-local")

HEALTH_ROW_KEY = "__cell_health__"
AUTO_HEAL_SECONDS = int(os.environ.get("CHAOS_AUTO_HEAL_SECONDS", "180"))
SPANNER_HEALTH_CACHE_TTL = 0.75


def _detect_project_id() -> str:
    for env_key in ("GOOGLE_CLOUD_PROJECT", "GCP_PROJECT", "GCLOUD_PROJECT"):
        val = os.environ.get(env_key, "").strip()
        if val:
            return val
    if SPANNER_INSTANCE:
        try:
            req = urllib.request.Request(
                "http://metadata.google.internal/computeMetadata/v1/project/project-id",
                headers={"Metadata-Flavor": "Google"},
            )
            with urllib.request.urlopen(req, timeout=0.5) as resp:
                proj = resp.read().decode("utf-8").strip()
                if proj:
                    return proj
        except Exception:
            pass
    return f"local-{CELL_ID}"


def _mask_project_id(raw_project_id: str) -> str:
    """Expose only the first 3 characters of the GCP project ID and obfuscate the rest."""
    cleaned = (raw_project_id or "").strip()
    if not cleaned:
        return "***"
    if len(cleaned) <= 3:
        return f"{cleaned[:1]}***"
    return f"{cleaned[:3]}{'*' * 12}"


# Keep _RAW_PROJECT_ID private in process memory solely for the Spanner SDK client.
_RAW_PROJECT_ID = _detect_project_id()
MASKED_PROJECT_ID = _mask_project_id(_RAW_PROJECT_ID)

_state_lock = threading.Lock()
_cell_state: dict[str, Any] = {
    "healthy": True,
    "poisoned_by_tenant": None,
    "poisoned_at": None,
    "requests_served": 0,
    "tenant_hits": {},
}
_spanner_health_cache: dict[str, Any] = {
    "checked_at": 0.0,
    "healthy": True,
    "poisoned_by": None,
    "auto_heal_in_sec": 0,
}

_spanner_module = None
_spanner_db = None
if SPANNER_INSTANCE:
    try:
        from google.cloud import spanner as _spanner_sdk  # type: ignore

        _spanner_module = _spanner_sdk
        _spanner_client = _spanner_sdk.Client(project=_RAW_PROJECT_ID)
        _spanner_db = _spanner_client.instance(SPANNER_INSTANCE).database(SPANNER_DATABASE)
    except Exception as exc:  # pragma: no cover - optional runtime dependency
        print(f"[{CELL_ID}] Spanner client unavailable ({type(exc).__name__}); using in-memory cell store.")


def _sanitize_actor(actor: str | None) -> str:
    cleaned = "".join(ch for ch in (actor or "chaos-operator") if ch.isalnum() or ch in "-_.")
    return (cleaned[:22] or "chaos-operator")


def _get_cell_health(force_refresh: bool = False) -> tuple[bool, str | None, int]:
    """Returns (healthy, poisoned_by, auto_heal_remaining_sec) for this isolated cell."""
    now = time.time()

    if _spanner_db is not None:
        with _state_lock:
            if not force_refresh and (now - _spanner_health_cache["checked_at"]) < SPANNER_HEALTH_CACHE_TTL:
                return (
                    bool(_spanner_health_cache["healthy"]),
                    _spanner_health_cache["poisoned_by"],
                    int(_spanner_health_cache["auto_heal_in_sec"]),
                )
        try:
            with _spanner_db.snapshot() as snapshot:
                rows = list(
                    snapshot.execute_sql(
                        "SELECT Tier, TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), CreatedAt, SECOND) "
                        "FROM Tenants WHERE TenantId = @row_key",
                        params={"row_key": HEALTH_ROW_KEY},
                        param_types={"row_key": _spanner_module.param_types.STRING},
                        timeout=3.5,
                    )
                )
            healthy = True
            poisoned_by: str | None = None
            remaining = 0
            if rows:
                tier_val = str(rows[0][0] or "HEALTHY")
                age_sec = int(rows[0][1] or 0)
                if tier_val.startswith("POISON:") and 0 <= age_sec < AUTO_HEAL_SECONDS:
                    healthy = False
                    poisoned_by = tier_val.split(":", 1)[1] or "noisy-tenant"
                    remaining = max(1, AUTO_HEAL_SECONDS - age_sec)
            with _state_lock:
                _spanner_health_cache.update(
                    {
                        "checked_at": time.time(),
                        "healthy": healthy,
                        "poisoned_by": poisoned_by,
                        "auto_heal_in_sec": remaining,
                    }
                )
                _cell_state["healthy"] = healthy
                _cell_state["poisoned_by_tenant"] = poisoned_by
            return healthy, poisoned_by, remaining
        except Exception as exc:
            print(f"[{CELL_ID}] Spanner health read warning: {type(exc).__name__}")

    with _state_lock:
        if not _cell_state["healthy"] and _cell_state["poisoned_at"] is not None:
            elapsed = now - float(_cell_state["poisoned_at"])
            if elapsed >= AUTO_HEAL_SECONDS:
                _cell_state["healthy"] = True
                _cell_state["poisoned_by_tenant"] = None
                _cell_state["poisoned_at"] = None
            else:
                return (
                    False,
                    _cell_state["poisoned_by_tenant"],
                    max(1, int(AUTO_HEAL_SECONDS - elapsed)),
                )
        return bool(_cell_state["healthy"]), _cell_state["poisoned_by_tenant"], 0


def _set_cell_health(healthy: bool, actor: str | None = None) -> tuple[bool, str | None, int]:
    """Updates cell health in isolated cell Spanner ledger + local process cache."""
    now = time.time()
    safe_actor = None if healthy else _sanitize_actor(actor)
    remaining = 0 if healthy else AUTO_HEAL_SECONDS

    with _state_lock:
        _cell_state["healthy"] = healthy
        _cell_state["poisoned_by_tenant"] = safe_actor
        _cell_state["poisoned_at"] = None if healthy else now
        _spanner_health_cache.update(
            {
                "checked_at": now,
                "healthy": healthy,
                "poisoned_by": safe_actor,
                "auto_heal_in_sec": remaining,
            }
        )

    if _spanner_db is not None and _spanner_module is not None:
        tier_val = "HEALTHY" if healthy else f"POISON:{safe_actor}"[:32]
        try:
            with _spanner_db.batch() as batch:
                batch.insert_or_update(
                    table="Tenants",
                    columns=("TenantId", "Tier", "CreatedAt"),
                    values=[(HEALTH_ROW_KEY, tier_val, _spanner_module.COMMIT_TIMESTAMP)],
                )
        except Exception as exc:
            print(f"[{CELL_ID}] Spanner health write warning: {type(exc).__name__}")

    return healthy, safe_actor, remaining


def _record_tenant_op(tenant_id: str) -> str:
    """Reads transaction timestamp from this cell's isolated Spanner database."""
    if _spanner_db is not None:
        try:
            with _spanner_db.snapshot() as snapshot:
                results = list(snapshot.execute_sql("SELECT CURRENT_TIMESTAMP()", timeout=4.0))
                ts = str(results[0][0]) if results else "ok"
                return f"spanner:{SPANNER_INSTANCE}:{ts}"
        except Exception as exc:
            print(f"[{CELL_ID}] Spanner read warning for tenant={tenant_id}: {type(exc).__name__}")
            return f"spanner-degraded:{SPANNER_INSTANCE}:{type(exc).__name__}"
    return f"local-spanner-sim:{CELL_ID}"


def _render_browser_dashboard(
    virtual_shard: str,
    ordered_hand: str,
    routed_cell_header: str,
    fleet_config: str,
    storage_token: str,
) -> bytes:
    esc = html.escape
    spanner_label = SPANNER_INSTANCE if SPANNER_INSTANCE else f"{CELL_ID}-mem-ledger"
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="description" content="Interactive Google Cloud cell-based architecture, Proxy-Wasm shuffle sharding, and live poison-pill chaos studio." />
  <title>Google Cloud Cell Architecture &amp; Shuffle-Sharding Studio — {esc(CELL_ID)}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet" />
  <style>
    :root {{
      --canvas: #f8f9fa;
      --surface: #ffffff;
      --surface-subtle: #f1f3f4;
      --border: #dadce0;
      --border-strong: #bdc1c6;
      --text-primary: #202124;
      --text-secondary: #5f6368;
      --blue: #1a73e8;
      --blue-hover: #1557b0;
      --blue-soft: #e8f0fe;
      --blue-ink: #174ea6;
      --green: #1e8e3e;
      --green-soft: #e6f4ea;
      --green-ink: #137333;
      --amber: #e37400;
      --amber-soft: #fef7e0;
      --amber-ink: #b06000;
      --red: #d93025;
      --red-soft: #fce8e6;
      --red-ink: #c5221f;
      --shadow-sm: 0 1px 2px rgba(60,64,67,0.08), 0 1px 3px 1px rgba(60,64,67,0.04);
      --shadow-md: 0 2px 6px rgba(60,64,67,0.12), 0 1px 2px rgba(60,64,67,0.08);
      --radius-lg: 12px;
      --radius-md: 8px;
      --font-sans: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      --font-mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 24px 20px 48px;
      background: var(--canvas);
      color: var(--text-primary);
      font-family: var(--font-sans);
      line-height: 1.5;
      -webkit-font-smoothing: antialiased;
    }}
    .container {{ max-width: 1180px; margin: 0 auto; }}
    header.hero {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      padding: 22px 24px;
      margin-bottom: 18px;
      box-shadow: var(--shadow-sm);
    }}
    .hero-top {{
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: flex-start;
      gap: 16px;
    }}
    h1 {{
      margin: 0 0 6px;
      font-size: 22px;
      font-weight: 700;
      letter-spacing: -0.015em;
      color: var(--text-primary);
    }}
    .subtitle {{
      margin: 0;
      font-size: 14px;
      color: var(--text-secondary);
      max-width: 780px;
    }}
    .meta-strip {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(175px, 1fr));
      gap: 10px;
      margin-top: 18px;
      padding-top: 16px;
      border-top: 1px solid #eceff1;
    }}
    .meta-item {{
      background: #fafbfc;
      border: 1px solid #e8eaed;
      border-radius: var(--radius-md);
      padding: 10px 12px;
    }}
    .meta-label {{
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--text-secondary);
      display: flex;
      align-items: center;
      gap: 6px;
    }}
    .meta-value {{
      margin-top: 3px;
      font-family: var(--font-mono);
      font-size: 13px;
      font-weight: 600;
      color: var(--text-primary);
      word-break: break-all;
    }}
    .mask-tag {{
      font-family: var(--font-sans);
      font-size: 10px;
      font-weight: 600;
      padding: 1px 6px;
      border-radius: 4px;
      background: var(--surface-subtle);
      color: var(--text-secondary);
      text-transform: none;
      letter-spacing: 0;
    }}

    .panel {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      padding: 20px 24px;
      margin-bottom: 18px;
      box-shadow: var(--shadow-sm);
    }}
    .panel-header {{
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-bottom: 14px;
    }}
    h2 {{
      margin: 0;
      font-size: 16px;
      font-weight: 600;
      letter-spacing: -0.01em;
    }}
    .panel-sub {{
      margin: 3px 0 0;
      font-size: 13px;
      color: var(--text-secondary);
    }}

    .btn-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }}
    button {{
      font-family: var(--font-sans);
      font-size: 13px;
      font-weight: 600;
      padding: 8px 14px;
      border-radius: 6px;
      border: 1px solid transparent;
      cursor: pointer;
      transition: background 0.15s ease, border-color 0.15s ease, transform 0.08s ease;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      white-space: nowrap;
    }}
    button:active {{ transform: translateY(1px); }}
    button:disabled {{ opacity: 0.55; cursor: not-allowed; }}
    .btn-primary {{ background: var(--blue); color: #fff; }}
    .btn-primary:hover:not(:disabled) {{ background: var(--blue-hover); }}
    .btn-outline {{
      background: #fff;
      color: var(--blue-ink);
      border-color: var(--border);
    }}
    .btn-outline:hover:not(:disabled) {{ background: var(--blue-soft); border-color: #aecbfa; }}
    .btn-danger {{
      background: var(--red-soft);
      color: var(--red-ink);
      border-color: #f6aea9;
    }}
    .btn-danger:hover:not(:disabled) {{ background: #fad2cf; }}
    .btn-heal {{
      background: var(--green-soft);
      color: var(--green-ink);
      border-color: #a8dab5;
    }}
    .btn-heal:hover:not(:disabled) {{ background: #ceead6; }}

    .narrative-bar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 11px 14px;
      border-radius: var(--radius-md);
      background: var(--blue-soft);
      border: 1px solid #d2e3fc;
      color: var(--blue-ink);
      font-size: 13px;
      font-weight: 500;
      margin-bottom: 14px;
    }}
    .narrative-bar.warn {{
      background: var(--amber-soft);
      border-color: #fde293;
      color: var(--amber-ink);
    }}
    .narrative-bar.err {{
      background: var(--red-soft);
      border-color: #fad2cf;
      color: var(--red-ink);
    }}
    .narrative-bar.ok {{
      background: var(--green-soft);
      border-color: #ceead6;
      color: var(--green-ink);
    }}

    .topology-wrap {{
      width: 100%;
      background: #fafbfc;
      border: 1px solid #e8eaed;
      border-radius: 10px;
      padding: 12px;
      overflow-x: auto;
    }}
    svg#topologySvg {{
      width: 100%;
      min-width: 820px;
      height: auto;
      display: block;
    }}
    .svg-cell-node {{ cursor: pointer; transition: filter 0.15s ease; }}
    .svg-cell-node:hover {{ filter: brightness(0.98); }}

    @keyframes dashFlow {{
      to {{ stroke-dashoffset: -28; }}
    }}
    .anim-flow {{
      stroke-dasharray: 8 6;
      animation: dashFlow 0.75s linear infinite;
    }}

    .legend-bar {{
      display: flex;
      flex-wrap: wrap;
      gap: 18px;
      margin-top: 12px;
      font-size: 12px;
      color: var(--text-secondary);
    }}
    .legend-item {{ display: inline-flex; align-items: center; gap: 6px; }}
    .dot {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; }}

    .two-col {{
      display: grid;
      grid-template-columns: 1.25fr 1fr;
      gap: 18px;
      margin-bottom: 18px;
    }}
    @media (max-width: 960px) {{
      .two-col {{ grid-template-columns: 1fr; }}
    }}

    .cell-cards-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(215px, 1fr));
      gap: 12px;
    }}
    .cell-card {{
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 14px;
      background: #fff;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 10px;
      transition: border-color 0.15s ease, box-shadow 0.15s ease;
    }}
    .cell-card.poisoned {{
      border-color: var(--red);
      background: #fffafa;
    }}
    .cell-card-head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
    }}
    .cell-title {{
      font-family: var(--font-mono);
      font-size: 14px;
      font-weight: 700;
    }}
    .status-pill {{
      font-size: 11px;
      font-weight: 600;
      padding: 2px 8px;
      border-radius: 999px;
    }}
    .status-pill.ok {{ background: var(--green-soft); color: var(--green-ink); }}
    .status-pill.down {{ background: var(--red-soft); color: var(--red-ink); }}
    .cell-meta {{
      font-size: 12px;
      color: var(--text-secondary);
      display: grid;
      gap: 3px;
    }}
    .cell-meta code {{
      font-family: var(--font-mono);
      font-size: 11.5px;
      color: var(--text-primary);
    }}

    .tenant-lab-box {{
      background: #fafbfc;
      border: 1px solid #e8eaed;
      border-radius: 10px;
      padding: 14px;
      margin-bottom: 14px;
    }}
    .input-group {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 8px;
    }}
    input[type="text"] {{
      flex: 1;
      min-width: 180px;
      padding: 8px 12px;
      border: 1px solid var(--border);
      border-radius: 6px;
      font-family: var(--font-mono);
      font-size: 13px;
      color: var(--text-primary);
      background: #fff;
    }}
    input[type="text"]:focus {{
      outline: 2px solid #aecbfa;
      border-color: var(--blue);
    }}
    .hand-preview {{
      margin-top: 8px;
      font-size: 12px;
      color: var(--text-secondary);
    }}
    .hand-preview code {{
      font-family: var(--font-mono);
      background: var(--blue-soft);
      color: var(--blue-ink);
      padding: 2px 6px;
      border-radius: 4px;
      font-weight: 600;
    }}

    .sweep-stats {{
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 8px;
      margin-bottom: 12px;
    }}
    .stat-box {{
      border: 1px solid #e8eaed;
      background: #fafbfc;
      border-radius: 8px;
      padding: 8px 10px;
      text-align: center;
    }}
    .stat-box .num {{
      font-family: var(--font-mono);
      font-size: 18px;
      font-weight: 700;
    }}
    .stat-box .lbl {{
      font-size: 11px;
      color: var(--text-secondary);
    }}
    .tenant-matrix {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(112px, 1fr));
      gap: 6px;
    }}
    .tenant-tile {{
      border: 1px solid #e8eaed;
      border-radius: 6px;
      padding: 6px 8px;
      font-size: 11px;
      background: #fff;
      cursor: pointer;
      transition: transform 0.08s ease;
    }}
    .tenant-tile:hover {{ transform: translateY(-1px); }}
    .tenant-tile .t-name {{
      font-family: var(--font-mono);
      font-weight: 600;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .tenant-tile .t-sub {{
      font-size: 10px;
      margin-top: 2px;
    }}
    .tenant-tile.primary-ok {{ background: var(--green-soft); border-color: #ceead6; color: var(--green-ink); }}
    .tenant-tile.failover-ok {{ background: var(--amber-soft); border-color: #fde293; color: var(--amber-ink); }}
    .tenant-tile.shard-down {{ background: var(--red-soft); border-color: #fad2cf; color: var(--red-ink); }}

    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 12.5px;
    }}
    th, td {{
      text-align: left;
      padding: 9px 10px;
      border-bottom: 1px solid #eef0f2;
    }}
    th {{
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.03em;
      color: var(--text-secondary);
      background: #fafbfc;
    }}
    td.mono {{ font-family: var(--font-mono); font-size: 12px; }}
  </style>
</head>
<body>
  <div class="container">
    <header class="hero">
      <div class="hero-top">
        <div>
          <h1>Cell-based architecture &amp; shuffle-sharding live studio</h1>
          <p class="subtitle">
            Connected to your live Google Cloud External Application Load Balancer. Trigger poison-pill faults from your browser to watch Proxy-Wasm shuffle sharding isolate blast radius and fail over across isolated Cloud Run + Spanner cells.
          </p>
        </div>
        <div class="btn-row">
          <button id="btnRefreshCells" class="btn-outline" onclick="refreshAllCells()">↻ Refresh Fleet</button>
          <button id="btnHealAllTop" class="btn-heal" onclick="healAllCells()">✓ Heal All Cells</button>
        </div>
      </div>

      <div class="meta-strip">
        <div class="meta-item">
          <div class="meta-label">Initial Serving Cell</div>
          <div class="meta-value" id="metaServingCell">{esc(CELL_ID)} ({esc(REGION)})</div>
        </div>
        <div class="meta-item">
          <div class="meta-label">GCP Project <span class="mask-tag">obfuscated</span></div>
          <div class="meta-value" id="metaProjectId">{esc(MASKED_PROJECT_ID)}</div>
        </div>
        <div class="meta-item">
          <div class="meta-label">Shuffle Combinatorics</div>
          <div class="meta-value" id="metaFleetConfig">{esc(fleet_config or "Discovering...")}</div>
        </div>
        <div class="meta-item">
          <div class="meta-label">Edge Header (x-cell-id)</div>
          <div class="meta-value">{esc(routed_cell_header)}</div>
        </div>
        <div class="meta-item">
          <div class="meta-label">Cell Spanner Instance</div>
          <div class="meta-value">{esc(spanner_label)}</div>
        </div>
      </div>
    </header>

    <main>
      <section class="panel" aria-labelledby="headingTopology">
        <div class="panel-header">
          <div>
            <h2 id="headingTopology">Live Anycast edge routing &amp; cellular fault-boundary visualizer</h2>
            <p class="panel-sub">
              Click any cell in the diagram (or run a guided chaos experiment) to inject a poison-pill crash and watch live HTTP failover trajectories.
            </p>
          </div>
          <div class="btn-row">
            <button id="btnGuidedDemo" class="btn-primary" onclick="runGuidedFailoverDemo()">
              ▶ Run Failover Walkthrough (Alice vs. Noisy Tenant)
            </button>
            <button id="btnBlastSweep" class="btn-outline" onclick="runBlastRadiusSweep()">
              ⊞ Run 24-Tenant Blast-Radius Sweep
            </button>
          </div>
        </div>

        <div id="narrativeBanner" class="narrative-bar">
          <span id="narrativeText">Ready — all isolated cells are polled live through the Global External ALB. Try clicking "Run Failover Walkthrough" or crashing a cell below.</span>
          <span id="narrativeStep" style="font-family:var(--font-mono);font-size:12px;">IDLE</span>
        </div>

        <div class="topology-wrap">
          <svg id="topologySvg" viewBox="0 0 980 390" role="img" aria-label="Live Google Cloud cell routing architecture diagram"></svg>
        </div>

        <div class="legend-bar">
          <span class="legend-item"><span class="dot" style="background:var(--green)"></span> Primary Cell Route (Attempt #0 → 200 OK)</span>
          <span class="legend-item"><span class="dot" style="background:var(--red)"></span> Poisoned Cell Blocked (503 CELL_UNAVAILABLE)</span>
          <span class="legend-item"><span class="dot" style="background:var(--amber)"></span> Automatic Shuffle-Shard Failover (Retry #1 → Backup Cell 200 OK)</span>
          <span class="legend-item"><span class="dot" style="background:#9aa0a6"></span> Unassigned Cell (Zero Cross-Cell Impact)</span>
        </div>
      </section>

      <div class="two-col">
        <section class="panel" style="margin-bottom:0;" aria-labelledby="headingCells">
          <div class="panel-header">
            <div>
              <h2 id="headingCells">Isolated cell fleet controls (Spanner-backed state)</h2>
              <p class="panel-sub">Poisoning a cell writes to its dedicated Spanner database (auto-heals in {AUTO_HEAL_SECONDS}s).</p>
            </div>
          </div>
          <div id="cellCardsContainer" class="cell-cards-grid"></div>
        </section>

        <section class="panel" style="margin-bottom:0;" aria-labelledby="headingSweep">
          <div class="panel-header">
            <div>
              <h2 id="headingSweep">Interactive tenant router &amp; 24-tenant blast matrix</h2>
              <p class="panel-sub">Send live requests or poison pills for any custom tenant ID.</p>
            </div>
          </div>

          <div class="tenant-lab-box">
            <label for="customTenantInput" style="font-size:12px;font-weight:600;color:var(--text-secondary);">
              Custom Request Header (<code>X-Tenant-Id</code>)
            </label>
            <div class="input-group">
              <input id="customTenantInput" type="text" value="alice-corp" oninput="updateCustomHandPreview()" />
              <button id="btnRouteCustom" class="btn-primary" onclick="sendCustomTenant(false)">Send Request</button>
              <button id="btnPoisonCustom" class="btn-danger" onclick="sendCustomTenant(true)">Poison Primary</button>
            </div>
            <div id="customHandPreview" class="hand-preview">Computing Fisher-Yates virtual shard...</div>
          </div>

          <div class="sweep-stats">
            <div class="stat-box">
              <div class="num" id="statAvailability" style="color:var(--green-ink);">—</div>
              <div class="lbl">Fleet Availability</div>
            </div>
            <div class="stat-box">
              <div class="num" id="statRescued" style="color:var(--amber-ink);">—</div>
              <div class="lbl">Rescued via Failover</div>
            </div>
            <div class="stat-box">
              <div class="num" id="statImpacted" style="color:var(--red-ink);">—</div>
              <div class="lbl">Impacted (All k Down)</div>
            </div>
          </div>

          <div id="tenantMatrix" class="tenant-matrix"></div>
        </section>
      </div>

      <section class="panel" aria-labelledby="headingTrace">
        <div class="panel-header">
          <div>
            <h2 id="headingTrace">Live HTTP edge routing &amp; failover trace log</h2>
            <p class="panel-sub">Real-time responses from Cloud Load Balancing + Proxy-Wasm + Cloud Run + Spanner ({esc(storage_token[:34])}...).</p>
          </div>
          <button id="btnClearLog" class="btn-outline" onclick="clearTraceLog()">Clear Log</button>
        </div>
        <div style="overflow-x:auto;">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Tenant ID</th>
                <th>Attempt</th>
                <th>Target / Serving Cell</th>
                <th>HTTP Status</th>
                <th>Latency</th>
                <th>Ordered Virtual Shard</th>
                <th>Isolated Spanner Read</th>
              </tr>
            </thead>
            <tbody id="traceTableBody"></tbody>
          </table>
        </div>
      </section>
    </main>
  </div>

  <script>
    const INITIAL_FLEET_HEADER = {json.dumps(fleet_config)};
    const INITIAL_VIRTUAL_SHARD = {json.dumps(virtual_shard)};
    const INITIAL_ORDERED_HAND = {json.dumps(ordered_hand)};
    const HASH_SALT = "gcp-cell-v1-prod";
    const MASK_64 = 0xffffffffffffffffn;

    let fleetN = 4;
    let shardK = 2;
    let cellsState = {{}};
    let pinTenantByCell = {{}};
    let activeTrace = null;
    let busyDemo = false;

    const SWEEP_TENANTS = Array.from({{ length: 24 }}, (_, i) =>
      [
        "alice-corp", "bob-retail", "charlie-bank", "delta-health",
        "echo-logistics", "foxtrot-ai", "globex-pay", "helios-bio",
        "initech-erp", "jupiter-sec", "kestrel-air", "lumina-med",
        "meridian-fx", "nimbus-cloud", "omnicorp-hq", "pinnacle-ins",
        "quantum-sim", "redwood-fin", "solaris-grid", "titan-mfg",
        "umbrella-rx", "vanguard-ops", "waypoint-nav", "zenith-labs"
      ][i]
    );

    // Exact 64-bit FNV-1a + fmix64 matching router-wasm/src/lib.rs & tests/shuffle_router.py
    function fnv1a64(str, seed) {{
      const bytes = new TextEncoder().encode(str);
      let h = 0xcbf29ce484222325n;
      for (const b of bytes) {{
        h ^= BigInt(b);
        h = (h * 0x100000001b3n) & MASK_64;
      }}
      h = (h ^ ((BigInt(seed) + 0x9e3779b97f4a7c15n) & MASK_64)) & MASK_64;
      h ^= h >> 33n;
      h = (h * 0xff51afd7ed558ccdn) & MASK_64;
      h ^= h >> 33n;
      h = (h * 0xc4ceb9fe1a85ec53n) & MASK_64;
      h ^= h >> 33n;
      return h;
    }}

    function computeVirtualShard(tenantId, totalCells = fleetN, shardSize = shardK, salt = HASH_SALT) {{
      const deck = Array.from({{ length: totalCells }}, (_, idx) => idx);
      const key = `${{salt}}:${{tenantId}}`;
      const k = Math.min(shardSize, totalCells);
      for (let i = 0; i < k; i++) {{
        const rem = BigInt(totalCells - i);
        const pick = i + Number(fnv1a64(key, i) % rem);
        const tmp = deck[i];
        deck[i] = deck[pick];
        deck[pick] = tmp;
      }}
      return deck.slice(0, k);
    }}

    function nChooseK(n, k) {{
      if (k < 0 || k > n) return 0;
      let res = 1;
      for (let i = 1; i <= k; i++) {{
        res = Math.round((res * (n - i + 1)) / i);
      }}
      return res;
    }}

    function formatCellId(idx) {{
      return `cell-${{String(idx).padStart(2, "0")}}`;
    }}

    function parseFleetHeader(hdr) {{
      if (!hdr) return null;
      const m = hdr.match(/N=(\\d+),k=(\\d+)/);
      if (!m) return null;
      return {{ n: parseInt(m[1], 10), k: parseInt(m[2], 10) }};
    }}

    function rebuildPinTenants() {{
      pinTenantByCell = {{}};
      for (let target = 0; target < fleetN; target++) {{
        for (let n = 0; n < 2000; n++) {{
          const candidate = `ops-pin-${{target}}-${{n}}`;
          if (computeVirtualShard(candidate, fleetN, shardK)[0] === target) {{
            pinTenantByCell[formatCellId(target)] = candidate;
            break;
          }}
        }}
      }}
    }}

    function updateCombinatoricsBanner() {{
      const combos = nChooseK(fleetN, shardK);
      const overlapPct = combos > 0 ? (100 / combos).toFixed(1) : "0";
      document.getElementById("metaFleetConfig").textContent =
        `N=${{fleetN}}, k=${{shardK}} → ${{combos}} shards (${{overlapPct}}% overlap)`;
    }}

    function setNarrative(text, tone = "info", stepLabel = "LIVE") {{
      const bar = document.getElementById("narrativeBanner");
      bar.className = "narrative-bar" + (tone === "info" ? "" : ` ${{tone}}`);
      document.getElementById("narrativeText").textContent = text;
      document.getElementById("narrativeStep").textContent = stepLabel;
    }}

    async function callSpecificCell(cellId, path, method = "GET", bodyObj = null) {{
      const pinTenant = pinTenantByCell[cellId] || `probe-${{cellId}}`;
      const headers = {{
        "X-Target-Cell": cellId,
        "X-Cell-Override": cellId,
        "X-Tenant-Id": pinTenant,
        "X-Retry-Attempt": "0"
      }};
      const opts = {{ method, headers }};
      if (method === "POST") {{
        headers["Content-Type"] = "application/json";
        opts.body = JSON.stringify(bodyObj || {{}});
      }}
      const t0 = performance.now();
      const resp = await fetch(path, opts);
      const ms = Math.round(performance.now() - t0);
      const data = await resp.json();
      return {{ status: resp.status, ms, data }};
    }}

    async function refreshAllCells() {{
      const promises = [];
      for (let i = 0; i < fleetN; i++) {{
        const cid = formatCellId(i);
        promises.push(
          callSpecificCell(cid, "/chaos/status", "GET")
            .then((res) => {{
              if (res.data && res.data.fleet_config) {{
                const parsed = parseFleetHeader(res.data.fleet_config);
                if (parsed && (parsed.n !== fleetN || parsed.k !== shardK)) {{
                  fleetN = parsed.n;
                  shardK = parsed.k;
                  rebuildPinTenants();
                  updateCombinatoricsBanner();
                }}
              }}
              const reportedCell = res.data.cell_id || cid;
              cellsState[reportedCell] = {{
                cell_id: reportedCell,
                healthy: res.data.healthy !== false,
                poisoned_by: res.data.poisoned_by || null,
                auto_heal_in_sec: res.data.auto_heal_in_sec || 0,
                region: res.data.region || (i % 2 === 0 ? "us-central1" : "us-east4"),
                spanner_instance: res.data.spanner_instance || `${{reportedCell}}-spanner`,
                requests_served: res.data.requests_served ?? 0,
                latency_ms: res.ms
              }};
            }})
            .catch(() => {{
              cellsState[cid] = cellsState[cid] || {{
                cell_id: cid,
                healthy: true,
                region: i % 2 === 0 ? "us-central1" : "us-east4",
                spanner_instance: `${{cid}}-spanner`,
                requests_served: 0,
                latency_ms: 0
              }};
            }})
        );
      }}
      await Promise.all(promises);
      renderCellCards();
      renderTopologySvg();
      updateCustomHandPreview();
    }}

    async function toggleCellChaos(cellId) {{
      const cur = cellsState[cellId];
      const shouldPoison = !cur || cur.healthy;
      if (shouldPoison) {{
        setNarrative(`Injecting poison-pill state into ${{cellId}} Spanner ledger...`, "warn", "CHAOS");
        await callSpecificCell(cellId, "/chaos/poison", "POST", {{ actor: "browser-chaos-btn" }});
        await refreshAllCells();
        setNarrative(
          `${{cellId}} is now POISONED (returning HTTP 503). Route a tenant on ${{cellId}} to watch automatic failover to its backup cell!`,
          "err",
          "CELL DOWN"
        );
      }} else {{
        setNarrative(`Healing ${{cellId}} in Spanner...`, "info", "HEALING");
        await callSpecificCell(cellId, "/chaos/reset", "POST", {{}});
        await refreshAllCells();
        setNarrative(`${{cellId}} restored to SERVING (HTTP 200).`, "ok", "HEALTHY");
      }}
    }}

    async function healAllCells() {{
      setNarrative("Healing all cells across the fleet...", "info", "RESET");
      const tasks = [];
      for (let i = 0; i < fleetN; i++) {{
        tasks.push(callSpecificCell(formatCellId(i), "/chaos/reset", "POST", {{}}).catch(() => null));
      }}
      await Promise.all(tasks);
      activeTrace = null;
      await refreshAllCells();
      setNarrative("All cells are SERVING (100% fleet health restored).", "ok", "READY");
    }}

    function appendTraceRow(entry) {{
      const tbody = document.getElementById("traceTableBody");
      const tr = document.createElement("tr");
      const badgeColor = entry.status === 200
        ? (entry.attempt > 0 ? "background:var(--amber-soft);color:var(--amber-ink);" : "background:var(--green-soft);color:var(--green-ink);")
        : "background:var(--red-soft);color:var(--red-ink);";
      const statusLabel = entry.status === 200
        ? (entry.attempt > 0 ? `200 OK (Failover #${{entry.attempt}})` : "200 OK (Primary)")
        : `${{entry.status}} ${{entry.error || "UNAVAILABLE"}}`;

      const cols = [
        {{ text: new Date().toLocaleTimeString(), mono: true }},
        {{ text: entry.tenantId, mono: true }},
        {{ text: `#${{entry.attempt}}`, mono: true }},
        {{ text: `${{entry.cellId}} (${{entry.region || "-"}})`, mono: true }},
        {{ htmlBadge: statusLabel, style: badgeColor }},
        {{ text: `${{entry.ms}} ms`, mono: true }},
        {{ text: entry.orderedHand, mono: true }},
        {{ text: entry.storage || "—", mono: true }}
      ];
      for (const c of cols) {{
        const td = document.createElement("td");
        if (c.mono) td.className = "mono";
        if (c.htmlBadge) {{
          const span = document.createElement("span");
          span.className = "status-pill";
          span.setAttribute("style", c.style);
          span.textContent = c.htmlBadge;
          td.appendChild(span);
        }} else {{
          td.textContent = c.text;
        }}
        tr.appendChild(td);
      }}
      tbody.prepend(tr);
      while (tbody.children.length > 30) {{
        tbody.removeChild(tbody.lastChild);
      }}
    }}

    function clearTraceLog() {{
      document.getElementById("traceTableBody").innerHTML = "";
    }}

    // Routes a tenant request with shuffle-shard retry awareness and animates the topology SVG
    async function routeTenantWithShuffleFailover(tenantId, options = {{ poison: false, animate: true, pauseBetweenRetriesMs: 420 }}) {{
      const handIndices = computeVirtualShard(tenantId, fleetN, shardK);
      const handIds = handIndices.map(formatCellId);
      const orderedHandStr = handIds.join(" → ");
      const maxAttempts = options.poison ? 1 : shardK;
      const attemptsLog = [];

      for (let attempt = 0; attempt < maxAttempts; attempt++) {{
        const headers = {{
          "Content-Type": "application/json",
          "X-Tenant-Id": tenantId,
          "X-Retry-Attempt": String(attempt)
        }};
        if (options.poison) {{
          headers["X-Poison-Payload"] = "crash";
        }}

        const t0 = performance.now();
        let status = 503;
        let data = {{}};
        try {{
          const resp = await fetch("/v1/infer", {{ method: "POST", headers, body: "{{}}" }});
          status = resp.status;
          data = await resp.json();
        }} catch (err) {{
          status = 503;
          data = {{ error: "NETWORK_ERROR" }};
        }}
        const ms = Math.round(performance.now() - t0);
        const actualCell = data.cell_id || handIds[attempt % handIds.length];

        attemptsLog.push({{
          attempt,
          cellId: actualCell,
          status,
          ms,
          error: data.error || null,
          storage: data.storage || data.error || ""
        }});

        appendTraceRow({{
          tenantId,
          attempt,
          cellId: actualCell,
          region: data.region || (cellsState[actualCell] && cellsState[actualCell].region) || "",
          status,
          error: data.error,
          ms,
          orderedHand: data.ordered_hand ? data.ordered_hand.split(",").join(" → ") : orderedHandStr,
          storage: data.storage || (data.poisoned_by ? `poisoned_by:${{data.poisoned_by}}` : data.error)
        }});

        if (options.animate) {{
          activeTrace = {{
            tenantId,
            handIds,
            attempts: [...attemptsLog],
            finalStatus: status
          }};
          renderTopologySvg();
        }}

        if (status === 200 || options.poison) {{
          break;
        }}
        if (attempt + 1 < maxAttempts && options.pauseBetweenRetriesMs > 0) {{
          await new Promise((r) => setTimeout(r, options.pauseBetweenRetriesMs));
        }}
      }}

      return {{
        tenantId,
        handIds,
        attempts: attemptsLog,
        finalStatus: attemptsLog[attemptsLog.length - 1].status,
        rescuedByFailover: attemptsLog.length > 1 && attemptsLog[attemptsLog.length - 1].status === 200
      }};
    }}

    function updateCustomHandPreview() {{
      const tid = (document.getElementById("customTenantInput").value || "anonymous").trim();
      const hand = computeVirtualShard(tid, fleetN, shardK).map(formatCellId);
      const previewEl = document.getElementById("customHandPreview");
      previewEl.innerHTML = "";
      previewEl.appendChild(document.createTextNode("Proxy-Wasm Fisher-Yates deal: Primary "));
      const c0 = document.createElement("code");
      c0.textContent = hand[0] || "-";
      previewEl.appendChild(c0);
      if (hand.length > 1) {{
        previewEl.appendChild(document.createTextNode(" → Backup "));
        const cRest = document.createElement("code");
        cRest.textContent = hand.slice(1).join(" → ");
        previewEl.appendChild(cRest);
      }}
    }}

    async function sendCustomTenant(poison) {{
      const tid = (document.getElementById("customTenantInput").value || "anonymous").trim();
      if (poison) {{
        setNarrative(`Sending poison-pill payload (X-Poison-Payload: crash) from ${{tid}}...`, "warn", "POISON");
        const res = await routeTenantWithShuffleFailover(tid, {{ poison: true, animate: true, pauseBetweenRetriesMs: 0 }});
        await refreshAllCells();
        setNarrative(
          `Poison-pill from ${{tid}} crashed primary ${{res.attempts[0].cellId}} (HTTP 503)! Now click "Send Request" for ${{tid}} or another tenant to watch shuffle-shard failover.`,
          "err",
          "CRASHED"
        );
      }} else {{
        setNarrative(`Routing live HTTP request for X-Tenant-Id: ${{tid}}...`, "info", "ROUTING");
        const res = await routeTenantWithShuffleFailover(tid, {{ poison: false, animate: true, pauseBetweenRetriesMs: 480 }});
        await refreshAllCells();
        if (res.finalStatus === 200 && res.rescuedByFailover) {{
          const failedCell = res.attempts[0].cellId;
          const okCell = res.attempts[res.attempts.length - 1].cellId;
          setNarrative(
            `Zero-downtime failover! Primary ${{failedCell}} returned 503, so Proxy-Wasm steered Retry #1 to healthy backup ${{okCell}} → HTTP 200 OK.`,
            "ok",
            "RESCUED"
          );
        }} else if (res.finalStatus === 200) {{
          setNarrative(
            `Served ${{tid}} on primary ${{res.attempts[0].cellId}} → HTTP 200 OK.`,
            "ok",
            "200 OK"
          );
        }} else {{
          setNarrative(
            `All ${{shardK}} cells in ${{tid}}'s virtual shard (${{res.handIds.join(", ")}}) are currently poisoned → HTTP 503.`,
            "err",
            "503 SHARD DOWN"
          );
        }}
      }}
    }}

    async function runGuidedFailoverDemo() {{
      if (busyDemo) return;
      busyDemo = true;
      document.getElementById("btnGuidedDemo").disabled = true;
      try {{
        // Pick noisy tenant & innocent tenant that share Primary hand[0] but differ on Backup hand[1]
        const noisyTenant = "noisy-tenant-x";
        const noisyHand = computeVirtualShard(noisyTenant, fleetN, shardK);
        let aliceTenant = "alice-corp";
        for (const cand of ["alice-corp", ...SWEEP_TENANTS, "alice-fin", "alice-saas", "alice-global"]) {{
          const h = computeVirtualShard(cand, fleetN, shardK);
          if (h[0] === noisyHand[0] && h.some((c) => c !== noisyHand[0])) {{
            aliceTenant = cand;
            break;
          }}
        }}
        const aliceHandIds = computeVirtualShard(aliceTenant, fleetN, shardK).map(formatCellId);
        const sharedPrimary = formatCellId(noisyHand[0]);

        setNarrative("Step 1/4: Ensuring all cells start healthy before chaos injection...", "info", "STEP 1/4");
        await healAllCells();
        await new Promise((r) => setTimeout(r, 450));

        setNarrative(
          `Step 2/4: Baseline — routing innocent customer '${{aliceTenant}}' (hand: ${{aliceHandIds.join(" → ")}}) → serving normally on ${{sharedPrimary}}...`,
          "info",
          "STEP 2/4"
        );
        await routeTenantWithShuffleFailover(aliceTenant, {{ poison: false, animate: true, pauseBetweenRetriesMs: 350 }});
        await refreshAllCells();
        await new Promise((r) => setTimeout(r, 950));

        setNarrative(
          `Step 3/4: Chaos! '${{noisyTenant}}' shares primary ${{sharedPrimary}} with '${{aliceTenant}}' and fires a poison-pill bug (X-Poison-Payload: crash)...`,
          "warn",
          "STEP 3/4"
        );
        await routeTenantWithShuffleFailover(noisyTenant, {{ poison: true, animate: true, pauseBetweenRetriesMs: 0 }});
        await refreshAllCells();
        await new Promise((r) => setTimeout(r, 1050));

        setNarrative(
          `Step 4/4: Sending '${{aliceTenant}}' again — watch Attempt #0 hit poisoned ${{sharedPrimary}} (503) and automatically fail over to '${{aliceTenant}}'s healthy backup cell!`,
          "info",
          "STEP 4/4"
        );
        const failoverRes = await routeTenantWithShuffleFailover(aliceTenant, {{
          poison: false,
          animate: true,
          pauseBetweenRetriesMs: 650
        }});
        await refreshAllCells();

        const backupCell = failoverRes.attempts[failoverRes.attempts.length - 1].cellId;
        setNarrative(
          `Walkthrough complete! Shared primary ${{sharedPrimary}} stayed crashed (503), yet '${{aliceTenant}}' succeeded with HTTP 200 OK on isolated backup ${{backupCell}} — zero customer outage.`,
          "ok",
          "PASS ✓"
        );
      }} finally {{
        busyDemo = false;
        document.getElementById("btnGuidedDemo").disabled = false;
      }}
    }}

    async function runBlastRadiusSweep() {{
      if (busyDemo) return;
      busyDemo = true;
      document.getElementById("btnBlastSweep").disabled = true;
      try {{
        setNarrative("Sweeping 24 live multi-tenant requests through the Anycast Cell Router...", "info", "SWEEPING");
        const matrixEl = document.getElementById("tenantMatrix");
        matrixEl.innerHTML = "";

        let okCount = 0;
        let rescuedCount = 0;
        let downCount = 0;

        const results = await Promise.all(
          SWEEP_TENANTS.map((tid) =>
            routeTenantWithShuffleFailover(tid, {{ poison: false, animate: false, pauseBetweenRetriesMs: 0 }})
          )
        );

        for (const r of results) {{
          const tile = document.createElement("div");
          let cls = "primary-ok";
          let label = `200 · ${{r.attempts[0].cellId}}`;
          if (r.finalStatus !== 200) {{
            cls = "shard-down";
            label = `503 · [${{r.handIds.join(",")}}]`;
            downCount++;
          }} else if (r.rescuedByFailover) {{
            cls = "failover-ok";
            label = `↻ 200 · ${{r.attempts[r.attempts.length - 1].cellId}}`;
            okCount++;
            rescuedCount++;
          }} else {{
            okCount++;
          }}
          tile.className = `tenant-tile ${{cls}}`;
          tile.title = `Click to visualize ${{r.tenantId}} (${{r.handIds.join(" → ")}})`;
          tile.onclick = () => {{
            document.getElementById("customTenantInput").value = r.tenantId;
            updateCustomHandPreview();
            activeTrace = r;
            renderTopologySvg();
          }};

          const tName = document.createElement("div");
          tName.className = "t-name";
          tName.textContent = r.tenantId;
          const tSub = document.createElement("div");
          tSub.className = "t-sub";
          tSub.textContent = label;
          tile.appendChild(tName);
          tile.appendChild(tSub);
          matrixEl.appendChild(tile);
        }}

        const availPct = ((100 * okCount) / SWEEP_TENANTS.length).toFixed(1) + "%";
        document.getElementById("statAvailability").textContent = availPct;
        document.getElementById("statRescued").textContent = `${{rescuedCount}} / ${{SWEEP_TENANTS.length}}`;
        document.getElementById("statImpacted").textContent = `${{downCount}} / ${{SWEEP_TENANTS.length}}`;

        await refreshAllCells();
        setNarrative(
          `24-tenant sweep complete: ${{okCount}}/${{SWEEP_TENANTS.length}} (${{availPct}}) available — ${{rescuedCount}} seamlessly rescued via backup cells, ${{downCount}} impacted. Click any tenant tile to inspect its path!`,
          downCount === 0 ? "ok" : "warn",
          "SWEEP DONE"
        );
      }} finally {{
        busyDemo = false;
        document.getElementById("btnBlastSweep").disabled = false;
      }}
    }}

    function renderCellCards() {{
      const container = document.getElementById("cellCardsContainer");
      container.innerHTML = "";
      for (let i = 0; i < fleetN; i++) {{
        const cid = formatCellId(i);
        const st = cellsState[cid] || {{
          cell_id: cid,
          healthy: true,
          region: i % 2 === 0 ? "us-central1" : "us-east4",
          spanner_instance: `${{cid}}-spanner`,
          requests_served: 0,
          latency_ms: 0
        }};

        const card = document.createElement("div");
        card.className = "cell-card" + (st.healthy ? "" : " poisoned");

        const head = document.createElement("div");
        head.className = "cell-card-head";
        const title = document.createElement("span");
        title.className = "cell-title";
        title.textContent = cid;
        const pill = document.createElement("span");
        pill.className = "status-pill " + (st.healthy ? "ok" : "down");
        pill.textContent = st.healthy
          ? "● SERVING"
          : `✕ POISONED (${{st.auto_heal_in_sec || 0}}s)`;
        head.appendChild(title);
        head.appendChild(pill);

        const meta = document.createElement("div");
        meta.className = "cell-meta";
        const rows = [
          ["Region", st.region],
          ["Spanner", st.spanner_instance],
          ["Probe RTT", st.latency_ms ? `${{st.latency_ms}} ms` : "—"],
          ["State", st.healthy ? "Isolated & Healthy" : `Crashed by ${{st.poisoned_by || "chaos"}}`]
        ];
        for (const [k, v] of rows) {{
          const line = document.createElement("div");
          line.textContent = `${{k}}: `;
          const code = document.createElement("code");
          code.textContent = v;
          line.appendChild(code);
          meta.appendChild(line);
        }}

        const btn = document.createElement("button");
        btn.id = `btnToggle-${{cid}}`;
        btn.className = st.healthy ? "btn-danger" : "btn-heal";
        btn.style.width = "100%";
        btn.style.justifyContent = "center";
        btn.textContent = st.healthy ? `⚡ Crash ${{cid}} (Inject 503)` : `✓ Heal ${{cid}}`;
        btn.onclick = () => toggleCellChaos(cid);

        card.appendChild(head);
        card.appendChild(meta);
        card.appendChild(btn);
        container.appendChild(card);
      }}
    }}

    function renderTopologySvg() {{
      const svg = document.getElementById("topologySvg");
      const rowH = fleetN <= 4 ? 82 : 52;
      const topPad = 32;
      const svgH = Math.max(360, topPad * 2 + fleetN * rowH);
      svg.setAttribute("viewBox", `0 0 980 ${{svgH}}`);

      const midY = Math.round(svgH / 2);
      const clientX = 24, clientW = 205, clientH = 128, clientY = midY - 64;
      const albX = 318, albW = 232, albH = 152, albY = midY - 76;
      const cellX = 668, cellW = 286, cellBoxH = fleetN <= 4 ? 68 : 42;

      const cellCenters = {{}};
      for (let i = 0; i < fleetN; i++) {{
        const cid = formatCellId(i);
        const y = topPad + i * rowH + (rowH - cellBoxH) / 2;
        cellCenters[cid] = {{ x: cellX, y, cy: y + cellBoxH / 2, idx: i }};
      }}

      const trace = activeTrace;
      const activeTenant = trace ? trace.tenantId : "alice-corp";
      const activeHand = trace ? trace.handIds : computeVirtualShard(activeTenant, fleetN, shardK).map(formatCellId);
      const combos = nChooseK(fleetN, shardK);

      let pathsMarkup = "";
      // 1. Subtle structural mesh lines from ALB to every cell
      for (let i = 0; i < fleetN; i++) {{
        const cid = formatCellId(i);
        const c = cellCenters[cid];
        const sx = albX + albW;
        const sy = midY;
        const ex = c.x;
        const ey = c.cy;
        const inHand = activeHand.includes(cid);
        const d = `M ${{sx}} ${{sy}} C ${{sx + 60}} ${{sy}}, ${{ex - 60}} ${{ey}}, ${{ex}} ${{ey}}`;
        pathsMarkup += `<path d="${{d}}" fill="none" stroke="${{inHand ? "#aecbfa" : "#e5e7eb"}}" stroke-width="${{inHand ? "2" : "1.2"}}" stroke-dasharray="${{inHand ? "4 4" : "none"}}" />`;
      }}

      // 2. Active request attempt arcs + animated orbs
      if (trace && trace.attempts && trace.attempts.length > 0) {{
        const inSx = clientX + clientW;
        const inEx = albX;
        pathsMarkup += `
          <path d="M ${{inSx}} ${{midY}} L ${{inEx}} ${{midY}}" stroke="#1a73e8" stroke-width="3" class="anim-flow" />
          <circle r="5" fill="#1a73e8">
            <animateMotion dur="0.7s" repeatCount="indefinite" path="M ${{inSx}} ${{midY}} L ${{inEx}} ${{midY}}" />
          </circle>
        `;

        trace.attempts.forEach((att) => {{
          const target = cellCenters[att.cellId];
          if (!target) return;
          const sx = albX + albW;
          const sy = midY;
          const ex = target.x;
          const ey = target.cy;
          const d = `M ${{sx}} ${{sy}} C ${{sx + 62}} ${{sy}}, ${{ex - 62}} ${{ey}}, ${{ex}} ${{ey}}`;
          const isOk = att.status === 200;
          const isFailover = isOk && att.attempt > 0;
          const stroke = !isOk ? "#d93025" : (isFailover ? "#e37400" : "#1e8e3e");
          const labelBg = !isOk ? "#fce8e6" : (isFailover ? "#fef7e0" : "#e6f4ea");
          const labelFg = !isOk ? "#c5221f" : (isFailover ? "#b06000" : "#137333");
          const calloutText = !isOk
            ? `✕ #${{att.attempt}} → 503 Blocked`
            : (isFailover ? `↻ Retry #${{att.attempt}} → 200 OK` : `✓ #0 Primary → 200 OK`);
          const midCurveX = Math.round((sx + ex) / 2) - 56;
          const midCurveY = Math.round((sy + ey) / 2) - 11;

          pathsMarkup += `
            <path d="${{d}}" fill="none" stroke="${{stroke}}" stroke-width="3.2" class="anim-flow" />
            <circle r="5.5" fill="${{stroke}}">
              <animateMotion dur="${{isOk ? "0.95s" : "0.65s"}}" repeatCount="indefinite" path="${{d}}" />
            </circle>
            <g transform="translate(${{midCurveX}}, ${{midCurveY}})">
              <rect width="118" height="20" rx="10" fill="${{labelBg}}" stroke="${{stroke}}" stroke-width="1" />
              <text x="59" y="13.5" text-anchor="middle" font-family="Inter, sans-serif" font-size="10" font-weight="700" fill="${{labelFg}}">${{calloutText}}</text>
            </g>
          `;
        }});
      }}

      // 3. Isolated Cell Boundary Nodes
      let cellsMarkup = "";
      for (let i = 0; i < fleetN; i++) {{
        const cid = formatCellId(i);
        const c = cellCenters[cid];
        const st = cellsState[cid] || {{ healthy: true, region: i % 2 === 0 ? "us-central1" : "us-east4", spanner_instance: `${{cid}}-spanner` }};
        const healthy = st.healthy !== false;
        const fill = healthy ? "#ffffff" : "#fff5f5";
        const border = healthy ? (activeHand.includes(cid) ? "#1a73e8" : "#dadce0") : "#d93025";
        const badgeBg = healthy ? "#e6f4ea" : "#fce8e6";
        const badgeFg = healthy ? "#137333" : "#c5221f";
        const badgeTxt = healthy ? "SERVING" : "503 DOWN";
        const roleIdx = activeHand.indexOf(cid);
        const roleTag = roleIdx === 0 ? "Primary" : (roleIdx > 0 ? `Backup #${{roleIdx}}` : "Isolated");

        if (fleetN <= 4) {{
          cellsMarkup += `
            <g class="svg-cell-node" onclick="toggleCellChaos('${{cid}}')" transform="translate(${{c.x}}, ${{c.y}})">
              <rect width="${{cellW}}" height="${{cellBoxH}}" rx="10" fill="${{fill}}" stroke="${{border}}" stroke-width="${{healthy ? "1.6" : "2.2"}}" />
              <circle cx="18" cy="22" r="5" fill="${{healthy ? "#1e8e3e" : "#d93025"}}" />
              <text x="30" y="26" font-family="JetBrains Mono, monospace" font-size="13" font-weight="700" fill="#202124">${{cid}}</text>
              <text x="96" y="25.5" font-family="Inter, sans-serif" font-size="11" fill="#5f6368">${{st.region}} · ${{roleTag}}</text>
              <rect x="204" y="11" width="70" height="20" rx="10" fill="${{badgeBg}}" />
              <text x="239" y="24.5" text-anchor="middle" font-family="Inter, sans-serif" font-size="10" font-weight="700" fill="${{badgeFg}}">${{badgeTxt}}</text>
              <text x="18" y="50" font-family="JetBrains Mono, monospace" font-size="11" fill="#5f6368">Cloud Run + Spanner (${{st.spanner_instance}})</text>
            </g>
          `;
        }} else {{
          cellsMarkup += `
            <g class="svg-cell-node" onclick="toggleCellChaos('${{cid}}')" transform="translate(${{c.x}}, ${{c.y}})">
              <rect width="${{cellW}}" height="${{cellBoxH}}" rx="8" fill="${{fill}}" stroke="${{border}}" stroke-width="1.6" />
              <circle cx="15" cy="21" r="4.5" fill="${{healthy ? "#1e8e3e" : "#d93025"}}" />
              <text x="26" y="25" font-family="JetBrains Mono, monospace" font-size="12" font-weight="700" fill="#202124">${{cid}}</text>
              <text x="86" y="24.5" font-family="Inter, sans-serif" font-size="10.5" fill="#5f6368">${{st.region}} (${{roleTag}})</text>
              <rect x="206" y="11" width="68" height="20" rx="10" fill="${{badgeBg}}" />
              <text x="240" y="24.5" text-anchor="middle" font-family="Inter, sans-serif" font-size="9.5" font-weight="700" fill="${{badgeFg}}">${{badgeTxt}}</text>
            </g>
          `;
        }}
      }}

      const outcomeStatus = trace ? trace.finalStatus : 200;
      const outcomeBg = outcomeStatus === 200 ? "#e6f4ea" : "#fce8e6";
      const outcomeFg = outcomeStatus === 200 ? "#137333" : "#c5221f";
      const outcomeLabel = trace
        ? (outcomeStatus === 200
            ? (trace.attempts.length > 1 ? "HTTP 200 (Failover)" : "HTTP 200 (Primary)")
            : "HTTP 503 (Shard Down)")
        : "Ready to Route";

      svg.innerHTML = `
        <text x="${{clientX}}" y="18" font-family="Inter, sans-serif" font-size="11" font-weight="600" fill="#5f6368">1. MULTI-TENANT CLIENT</text>
        <text x="${{albX}}" y="18" font-family="Inter, sans-serif" font-size="11" font-weight="600" fill="#5f6368">2. GLOBAL ANYCAST EDGE (PROXY-WASM)</text>
        <text x="${{cellX}}" y="18" font-family="Inter, sans-serif" font-size="11" font-weight="600" fill="#5f6368">3. ISOLATED CELLS (CLICK TO CRASH / HEAL)</text>

        ${{pathsMarkup}}

        <!-- Client Node -->
        <g transform="translate(${{clientX}}, ${{clientY}})">
          <rect width="${{clientW}}" height="${{clientH}}" rx="10" fill="#ffffff" stroke="#dadce0" stroke-width="1.6" />
          <text x="16" y="28" font-family="Inter, sans-serif" font-size="12" font-weight="700" fill="#202124">SaaS Tenant Request</text>
          <text x="16" y="49" font-family="JetBrains Mono, monospace" font-size="11.5" fill="#174ea6">X-Tenant-Id: ${{activeTenant.slice(0, 16)}}</text>
          <text x="16" y="69" font-family="Inter, sans-serif" font-size="11" fill="#5f6368">Hand: ${{activeHand.join(" → ")}}</text>
          <rect x="16" y="86" width="172" height="26" rx="6" fill="${{outcomeBg}}" />
          <text x="102" y="103" text-anchor="middle" font-family="Inter, sans-serif" font-size="11" font-weight="700" fill="${{outcomeFg}}">${{outcomeLabel}}</text>
        </g>

        <!-- Global External ALB + Service Extensions Node -->
        <g transform="translate(${{albX}}, ${{albY}})">
          <rect width="${{albW}}" height="${{albH}}" rx="10" fill="#ffffff" stroke="#1a73e8" stroke-width="1.8" />
          <rect x="14" y="14" width="204" height="24" rx="6" fill="#e8f0fe" />
          <text x="116" y="30" text-anchor="middle" font-family="Inter, sans-serif" font-size="11.5" font-weight="700" fill="#174ea6">Global External ALB + Cloud Armor</text>
          <text x="16" y="58" font-family="Inter, sans-serif" font-size="12" font-weight="600" fill="#202124">LbEdgeExtension (Rust Wasm)</text>
          <text x="16" y="77" font-family="JetBrains Mono, monospace" font-size="11" fill="#5f6368">FNV-1a + Fisher-Yates (k=${{shardK}})</text>
          <text x="16" y="96" font-family="JetBrains Mono, monospace" font-size="11" fill="#5f6368">C(${{fleetN}}, ${{shardK}}) = ${{combos}} virtual shards</text>
          <rect x="14" y="110" width="204" height="28" rx="6" fill="#f8f9fa" stroke="#e8eaed" />
          <text x="116" y="128" text-anchor="middle" font-family="JetBrains Mono, monospace" font-size="10.5" fill="#202124">x-cell-id → URL Map RouteRule</text>
        </g>

        ${{cellsMarkup}}
      `;
    }}

    (async function initStudio() {{
      const parsed = parseFleetHeader(INITIAL_FLEET_HEADER);
      if (parsed) {{
        fleetN = parsed.n;
        shardK = parsed.k;
      }} else if (INITIAL_VIRTUAL_SHARD && INITIAL_VIRTUAL_SHARD.includes(",")) {{
        const parts = INITIAL_VIRTUAL_SHARD.split(",").filter(Boolean);
        shardK = parts.length;
        fleetN = shardK >= 3 ? 8 : 4;
      }}
      rebuildPinTenants();
      updateCombinatoricsBanner();
      updateCustomHandPreview();
      await refreshAllCells();
      await routeTenantWithShuffleFailover("alice-corp", {{ poison: false, animate: true, pauseBetweenRetriesMs: 350 }});

      setInterval(() => {{
        if (!document.hidden && !busyDemo) {{
          refreshAllCells();
        }}
      }}, 5000);
    }})();
  </script>
</body>
</html>
"""
    return page.encode("utf-8")


class CellRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("QUIET_LOGS") == "1":
            return
        super().log_message(fmt, *args)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Served-By-Cell", CELL_ID)
        self.send_header("X-App-Version", APP_VERSION)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Served-By-Cell", CELL_ID)
        self.send_header("X-App-Version", APP_VERSION)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        clean_path = self.path.split("?", 1)[0]

        if clean_path == "/healthz":
            healthy, poisoned_by, remaining = _get_cell_health(force_refresh=False)
            self._send_json(
                200,
                {
                    "status": "SERVING" if healthy else "DEGRADED_POISONED",
                    "healthy": healthy,
                    "poisoned_by": poisoned_by,
                    "auto_heal_in_sec": remaining,
                    "cell_id": CELL_ID,
                    "project_id": MASKED_PROJECT_ID,
                    "region": REGION,
                    "spanner_instance": SPANNER_INSTANCE or f"{CELL_ID}-mem-ledger",
                    "version": APP_VERSION,
                },
            )
            return

        if clean_path == "/chaos/status":
            healthy, poisoned_by, remaining = _get_cell_health(force_refresh=True)
            with _state_lock:
                served = int(_cell_state["requests_served"])
            self._send_json(
                200,
                {
                    "status": "SERVING" if healthy else "POISONED",
                    "healthy": healthy,
                    "poisoned_by": poisoned_by,
                    "auto_heal_in_sec": remaining,
                    "cell_id": CELL_ID,
                    "project_id": MASKED_PROJECT_ID,
                    "region": REGION,
                    "spanner_instance": SPANNER_INSTANCE or f"{CELL_ID}-mem-ledger",
                    "requests_served": served,
                    "fleet_config": self.headers.get("X-Fleet-Config", ""),
                    "version": APP_VERSION,
                },
            )
            return

        if clean_path == "/metrics":
            healthy, poisoned_by, remaining = _get_cell_health(force_refresh=False)
            with _state_lock:
                served = int(_cell_state["requests_served"])
                hits = dict(_cell_state["tenant_hits"])
            self._send_json(
                200,
                {
                    "cell_id": CELL_ID,
                    "project_id": MASKED_PROJECT_ID,
                    "region": REGION,
                    "healthy": healthy,
                    "poisoned_by_tenant": poisoned_by,
                    "auto_heal_in_sec": remaining,
                    "requests_served": served,
                    "tenant_hits": hits,
                },
            )
            return

        # Always render the interactive Chaos Studio when opened in a web browser
        accept = self.headers.get("Accept", "")
        if clean_path in ("/", "/index.html") and "text/html" in accept:
            tenant_id = self.headers.get("X-Tenant-Id", "browser-visitor")
            virtual_shard = self.headers.get("X-Virtual-Shard", "")
            ordered_hand = self.headers.get("X-Ordered-Hand", virtual_shard)
            routed_cell = self.headers.get("X-Cell-Id", CELL_ID)
            fleet_config = self.headers.get("X-Fleet-Config", "")
            storage_token = _record_tenant_op(tenant_id)
            self._send_html(
                200,
                _render_browser_dashboard(
                    virtual_shard=virtual_shard,
                    ordered_hand=ordered_hand,
                    routed_cell_header=routed_cell,
                    fleet_config=fleet_config,
                    storage_token=storage_token,
                ),
            )
            return

        self._handle_tenant_request()

    def do_POST(self) -> None:
        clean_path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length) if length > 0 else b""

        if clean_path == "/chaos/reset":
            _set_cell_health(True, None)
            self._send_json(
                200,
                {
                    "status": "RESET",
                    "healthy": True,
                    "cell_id": CELL_ID,
                    "project_id": MASKED_PROJECT_ID,
                    "region": REGION,
                },
            )
            return

        if clean_path == "/chaos/poison":
            actor = self.headers.get("X-Poison-Actor", "")
            if raw_body:
                try:
                    parsed = json.loads(raw_body.decode("utf-8", errors="replace"))
                    if isinstance(parsed, dict) and parsed.get("actor"):
                        actor = str(parsed["actor"])
                except Exception:
                    pass
            if not actor:
                actor = self.headers.get("X-Tenant-Id", "chaos-operator")
            _, poisoned_by, remaining = _set_cell_health(False, actor)
            self._send_json(
                200,
                {
                    "status": "POISONED",
                    "healthy": False,
                    "poisoned_by": poisoned_by,
                    "auto_heal_in_sec": remaining,
                    "cell_id": CELL_ID,
                    "project_id": MASKED_PROJECT_ID,
                    "region": REGION,
                },
            )
            return

        self._handle_tenant_request()

    def _handle_tenant_request(self) -> None:
        tenant_id = self.headers.get("X-Tenant-Id", "anonymous")
        poison_header = self.headers.get("X-Poison-Payload", "").lower()
        virtual_shard = self.headers.get("X-Virtual-Shard", "unassigned")
        ordered_hand = self.headers.get("X-Ordered-Hand", virtual_shard)
        fleet_config = self.headers.get("X-Fleet-Config", "")

        healthy, poisoned_by, remaining = _get_cell_health(force_refresh=False)
        if not healthy:
            self._send_json(
                503,
                {
                    "error": "CELL_UNAVAILABLE",
                    "cell_id": CELL_ID,
                    "project_id": MASKED_PROJECT_ID,
                    "region": REGION,
                    "poisoned_by": poisoned_by,
                    "auto_heal_in_sec": remaining,
                    "virtual_shard": virtual_shard,
                    "ordered_hand": ordered_hand,
                    "fleet_config": fleet_config,
                },
            )
            return

        if poison_header == "crash":
            _, actor_saved, rem = _set_cell_health(False, tenant_id)
            self._send_json(
                503,
                {
                    "error": "POISON_PILL_TRIGGERED",
                    "cell_id": CELL_ID,
                    "project_id": MASKED_PROJECT_ID,
                    "region": REGION,
                    "tenant_id": tenant_id,
                    "poisoned_by": actor_saved,
                    "auto_heal_in_sec": rem,
                    "virtual_shard": virtual_shard,
                    "ordered_hand": ordered_hand,
                    "fleet_config": fleet_config,
                },
            )
            return

        with _state_lock:
            _cell_state["requests_served"] += 1
            _cell_state["tenant_hits"][tenant_id] = (
                _cell_state["tenant_hits"].get(tenant_id, 0) + 1
            )

        storage_token = _record_tenant_op(tenant_id)
        self._send_json(
            200,
            {
                "status": "OK",
                "cell_id": CELL_ID,
                "project_id": MASKED_PROJECT_ID,
                "region": REGION,
                "version": APP_VERSION,
                "revision": REVISION_ID,
                "tenant_id": tenant_id,
                "virtual_shard": virtual_shard,
                "ordered_hand": ordered_hand,
                "fleet_config": fleet_config,
                "storage": storage_token,
            },
        )


class CellHTTPServer(ThreadingHTTPServer):
    request_queue_size = 256
    allow_reuse_address = True


def serve(host: str, port: int) -> None:
    server = CellHTTPServer((host, port), CellRequestHandler)
    print(f"[{CELL_ID}] Listening on http://{host}:{port} (project={MASKED_PROJECT_ID}, version={APP_VERSION})")
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run isolated SaaS cell backend")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = parser.parse_args()
    serve(args.host, args.port)
