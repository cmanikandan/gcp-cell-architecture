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
- Static architecture blueprint diagrams (`/static/*.png`) + guided interactive live routing
  and chaos studio at `GET /`.
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
from pathlib import Path
from typing import Any

CELL_ID = os.environ.get("CELL_ID", "cell-00")
REGION = os.environ.get("CELL_REGION", "us-central1")
SPANNER_INSTANCE = os.environ.get("SPANNER_INSTANCE", "")
SPANNER_DATABASE = os.environ.get("SPANNER_DATABASE", "cell_ledger")
APP_VERSION = os.environ.get("APP_VERSION", "v1.4.3")
REVISION_ID = os.environ.get("K_REVISION", f"{CELL_ID}-local")

HEALTH_ROW_KEY = "__cell_health__"
AUTO_HEAL_SECONDS = int(os.environ.get("CHAOS_AUTO_HEAL_SECONDS", "180"))
SPANNER_HEALTH_CACHE_TTL = 0.75

_HERE = Path(__file__).resolve().parent
_ALLOWED_STATIC_PNGS = {
    "01_cell_reference_architecture.png",
    "02_shuffle_sharding_combinatorics.png",
    "03_global_vs_regional_alb_ingress.png",
    "04_cloud_deploy_wave_pipeline.png",
}


def _load_static_png(filename: str) -> bytes | None:
    if filename not in _ALLOWED_STATIC_PNGS:
        return None
    for folder in (_HERE / "static", _HERE.parent / "diagrams"):
        candidate = folder / filename
        if candidate.is_file():
            return candidate.read_bytes()
    return None


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
    return cleaned[:22] or "chaos-operator"


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
      --canvas: #f4f6f9;
      --surface: #ffffff;
      --surface-subtle: #f8f9fa;
      --border: #dadce0;
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
      --shadow-sm: 0 1px 3px rgba(60,64,67,0.08), 0 1px 2px rgba(60,64,67,0.05);
      --radius-lg: 12px;
      --radius-md: 8px;
      --font-sans: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      --font-mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 24px 20px 56px;
      background: var(--canvas);
      color: var(--text-primary);
      font-family: var(--font-sans);
      line-height: 1.5;
      -webkit-font-smoothing: antialiased;
    }}
    .container {{ max-width: 1220px; margin: 0 auto; }}

    header.hero {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      padding: 22px 26px;
      margin-bottom: 20px;
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
    }}
    .subtitle {{
      margin: 0;
      font-size: 14px;
      color: var(--text-secondary);
      max-width: 800px;
    }}
    .meta-strip {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(185px, 1fr));
      gap: 10px;
      margin-top: 16px;
      padding-top: 16px;
      border-top: 1px solid #eceff1;
    }}
    .meta-item {{
      background: var(--surface-subtle);
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
    }}
    .mask-tag {{
      font-family: var(--font-sans);
      font-size: 10px;
      font-weight: 600;
      padding: 1px 6px;
      border-radius: 4px;
      background: #e8eaed;
      color: var(--text-secondary);
      text-transform: none;
      letter-spacing: 0;
    }}

    .panel {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      padding: 22px 26px;
      margin-bottom: 20px;
      box-shadow: var(--shadow-sm);
    }}
    .panel-header {{
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      margin-bottom: 12px;
    }}
    .step-kicker {{
      display: inline-block;
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--blue);
      margin-bottom: 3px;
    }}
    h2 {{
      margin: 0;
      font-size: 17px;
      font-weight: 700;
      letter-spacing: -0.01em;
    }}

    .guide-box {{
      background: #f8fafd;
      border: 1px solid #d2e3fc;
      border-radius: var(--radius-md);
      padding: 12px 16px;
      margin-bottom: 16px;
      font-size: 13px;
      color: #3c4043;
      line-height: 1.55;
    }}
    .guide-box strong {{ color: var(--text-primary); }}
    .guide-steps {{
      margin: 6px 0 0;
      padding-left: 18px;
    }}
    .guide-steps li {{ margin-bottom: 3px; }}

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
      transition: background 0.15s ease, border-color 0.15s ease;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      white-space: nowrap;
    }}
    button:disabled {{ opacity: 0.55; cursor: not-allowed; }}
    .btn-primary {{ background: var(--blue); color: #fff; }}
    .btn-primary:hover:not(:disabled) {{ background: var(--blue-hover); }}
    .btn-outline {{ background: #fff; color: var(--blue-ink); border-color: var(--border); }}
    .btn-outline:hover:not(:disabled) {{ background: var(--blue-soft); border-color: #aecbfa; }}
    .btn-danger {{ background: var(--red-soft); color: var(--red-ink); border-color: #f6aea9; }}
    .btn-danger:hover:not(:disabled) {{ background: #fad2cf; }}
    .btn-heal {{ background: var(--green-soft); color: var(--green-ink); border-color: #a8dab5; }}
    .btn-heal:hover:not(:disabled) {{ background: #ceead6; }}

    /* Architecture Blueprint Tabs */
    .arch-tabs {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 14px;
      border-bottom: 1px solid #e8eaed;
      padding-bottom: 12px;
    }}
    .arch-tab {{
      background: var(--surface-subtle);
      color: var(--text-secondary);
      border: 1px solid var(--border);
      font-size: 12.5px;
      padding: 7px 13px;
    }}
    .arch-tab.active {{
      background: var(--blue-soft);
      color: var(--blue-ink);
      border-color: var(--blue);
    }}
    .arch-figure {{
      margin: 0;
      background: #ffffff;
      border: 1px solid #e8eaed;
      border-radius: 10px;
      padding: 12px;
      text-align: center;
    }}
    .arch-figure img {{
      max-width: 100%;
      height: auto;
      max-height: 460px;
      object-fit: contain;
      border-radius: 6px;
      display: block;
      margin: 0 auto;
    }}
    .arch-caption {{
      margin-top: 10px;
      font-size: 13px;
      color: var(--text-secondary);
      text-align: left;
      padding: 8px 12px;
      background: var(--surface-subtle);
      border-radius: 6px;
    }}

    /* Narrative Banner & Live SVG Visualizer */
    .narrative-bar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 11px 16px;
      border-radius: var(--radius-md);
      background: var(--blue-soft);
      border: 1px solid #d2e3fc;
      color: var(--blue-ink);
      font-size: 13px;
      font-weight: 500;
      margin-bottom: 14px;
    }}
    .narrative-bar.warn {{ background: var(--amber-soft); border-color: #fde293; color: var(--amber-ink); }}
    .narrative-bar.err {{ background: var(--red-soft); border-color: #fad2cf; color: var(--red-ink); }}
    .narrative-bar.ok {{ background: var(--green-soft); border-color: #ceead6; color: var(--green-ink); }}

    .topology-wrap {{
      width: 100%;
      background: #fbfcfd;
      border: 1px solid #e0e3e7;
      border-radius: 10px;
      padding: 14px 16px;
      overflow-x: auto;
    }}
    svg#topologySvg {{
      width: 100%;
      min-width: 960px;
      height: auto;
      display: block;
    }}
    .svg-cell-node {{ cursor: pointer; }}
    .svg-cell-node:hover rect.cell-frame {{ stroke-width: 2.4; }}

    @keyframes dashFlow {{
      to {{ stroke-dashoffset: -28; }}
    }}
    .anim-flow {{
      stroke-dasharray: 8 6;
      animation: dashFlow 0.8s linear infinite;
    }}

    .legend-bar {{
      display: flex;
      flex-wrap: wrap;
      gap: 20px;
      margin-top: 12px;
      font-size: 12px;
      color: var(--text-secondary);
    }}
    .legend-item {{ display: inline-flex; align-items: center; gap: 6px; }}
    .dot {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; }}

    .two-col {{
      display: grid;
      grid-template-columns: 1.15fr 1fr;
      gap: 20px;
      margin-bottom: 20px;
    }}
    @media (max-width: 980px) {{
      .two-col {{ grid-template-columns: 1fr; }}
    }}

    .cell-cards-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(225px, 1fr));
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
    }}
    .cell-card.poisoned {{
      border-color: var(--red);
      background: #fff8f8;
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
      white-space: nowrap;
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
      background: var(--surface-subtle);
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
      min-width: 170px;
      padding: 8px 12px;
      border: 1px solid var(--border);
      border-radius: 6px;
      font-family: var(--font-mono);
      font-size: 13px;
      color: var(--text-primary);
      background: #fff;
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
      background: var(--surface-subtle);
      border-radius: 8px;
      padding: 8px 10px;
      text-align: center;
    }}
    .stat-box .num {{ font-family: var(--font-mono); font-size: 18px; font-weight: 700; }}
    .stat-box .lbl {{ font-size: 11px; color: var(--text-secondary); }}

    .tenant-matrix {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(116px, 1fr));
      gap: 6px;
    }}
    .tenant-tile {{
      border: 1px solid #e8eaed;
      border-radius: 6px;
      padding: 6px 8px;
      font-size: 11px;
      background: #fff;
      cursor: pointer;
    }}
    .tenant-tile .t-name {{
      font-family: var(--font-mono);
      font-weight: 600;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .tenant-tile .t-sub {{ font-size: 10px; margin-top: 2px; }}
    .tenant-tile.primary-ok {{ background: var(--green-soft); border-color: #ceead6; color: var(--green-ink); }}
    .tenant-tile.failover-ok {{ background: var(--amber-soft); border-color: #fde293; color: var(--amber-ink); }}
    .tenant-tile.shard-down {{ background: var(--red-soft); border-color: #fad2cf; color: var(--red-ink); }}

    table {{ width: 100%; border-collapse: collapse; font-size: 12.5px; }}
    th, td {{ text-align: left; padding: 9px 10px; border-bottom: 1px solid #eef0f2; }}
    th {{
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.03em;
      color: var(--text-secondary);
      background: var(--surface-subtle);
    }}
    td.mono {{ font-family: var(--font-mono); font-size: 12px; }}
  </style>
</head>
<body>
  <!-- Shared official Google Cloud SVG symbols (explicit fills so classes never collide) -->
  <svg width="0" height="0" style="position:absolute;visibility:hidden;" aria-hidden="true">
    <defs>
      <symbol id="gcp-clb" viewBox="0 0 24 24">
        <rect fill="#669df6" x="18" y="12" width="2" height="4"/>
        <rect fill="#669df6" x="11" y="12" width="2" height="4"/>
        <rect fill="#669df6" x="4" y="12" width="2" height="4"/>
        <polygon fill="#4285f4" points="13 11 11 11 11 7 13 7 13 11"/>
        <rect fill="#669df6" x="4" y="11" width="16" height="2"/>
        <rect fill="#aecbfa" x="6" y="2" width="12" height="5"/>
        <rect fill="#669df6" x="12" y="2" width="6" height="5"/>
        <rect fill="#aecbfa" x="16" y="16" width="6" height="6"/>
        <rect fill="#aecbfa" x="2" y="16" width="6" height="6"/>
        <rect fill="#669df6" x="5" y="16" width="3" height="6"/>
        <rect fill="#aecbfa" x="9" y="16" width="6" height="6"/>
        <rect fill="#669df6" x="12" y="16" width="3" height="6"/>
        <rect fill="#669df6" x="19" y="16" width="3" height="6"/>
      </symbol>
      <symbol id="gcp-armor" viewBox="0 0 24 24">
        <polygon fill="#aecbfa" points="9.76 20.6 8.72 19.55 14.17 14.07 15.21 15.12 9.76 20.6"/>
        <polygon fill="#aecbfa" points="7.03 18.32 5.99 17.27 13.34 9.88 14.38 10.93 7.03 18.32"/>
        <polygon fill="#aecbfa" points="5.34 14.98 4.3 13.93 9.18 9.03 10.22 10.08 5.34 14.98"/>
        <path fill="#669df6" d="M12,3.61l6.78,3v4.55A9.71,9.71,0,0,1,12,20.48a9.7,9.7,0,0,1-6.78-9.31V6.63l6.78-3M12,2,3.75,5.68v5.49A11.17,11.17,0,0,0,11.85,22L12,22l.15,0a11.17,11.17,0,0,0,8.1-10.78V5.68L12,2Z"/>
      </symbol>
      <symbol id="gcp-run" viewBox="0 0 24 24">
        <polygon fill="#aecbfa" points="8.9 2.63 12.02 12 21.38 12 8.9 2.63"/>
        <polygon fill="#4285f4" points="21.38 12 12.02 12 8.9 21.38 21.38 12"/>
        <polygon fill="#4285f4" points="3.44 21.38 6.57 19.81 8.9 12 5.78 12 3.44 21.38"/>
        <polygon fill="#aecbfa" points="3.44 2.63 5.78 12 8.9 12 6.57 4.19 3.44 2.63"/>
      </symbol>
      <symbol id="gcp-spanner" viewBox="0 0 24 24">
        <polygon fill="#4285f4" points="13.34 12.76 13.34 8.4 10.66 8.4 10.66 12.76 6.89 14.94 8.23 17.26 12 15.08 15.77 17.26 17.11 14.94 13.34 12.76"/>
        <polygon fill="#aecbfa" points="10.65 2.77 8.62 3.99 8.62 7.72 12 9.55 12 6.44 10.65 5.82 10.65 2.77"/>
        <polygon fill="#669df6" points="13.35 5.82 12 6.44 12 9.55 15.38 7.72 15.38 3.99 13.35 2.77 13.35 5.82"/>
        <polygon fill="#669df6" points="22 17.87 21.95 15.38 18.72 13.52 15.45 15.53 18.14 17.08 19.3 16.32 22 17.87"/>
        <polygon fill="#669df6" points="2 17.87 4.7 16.32 5.86 17.08 8.55 15.53 5.28 13.52 2.05 15.38 2 17.87"/>
      </symbol>
    </defs>
  </svg>

  <div class="container">
    <header class="hero">
      <div class="hero-top">
        <div>
          <h1>Cell-based architecture &amp; shuffle-sharding live explorer</h1>
          <p class="subtitle">
            You are viewing a live Google Cloud External Application Load Balancer running a Rust WebAssembly shuffle-sharding router ahead of isolated Cloud Run + Spanner cells. Every section below explains <strong>what it represents</strong> and <strong>exact steps to try</strong>.
          </p>
        </div>
        <div class="btn-row">
          <button id="btnRefreshCells" class="btn-outline" onclick="refreshAllCells(true)">↻ Refresh Status</button>
          <button id="btnHealAllTop" class="btn-heal" onclick="healAllCells()">✓ Heal All Cells</button>
        </div>
      </div>

      <div class="meta-strip">
        <div class="meta-item">
          <div class="meta-label">Page Served By Cell</div>
          <div class="meta-value" id="metaServingCell">{esc(CELL_ID)} ({esc(REGION)})</div>
        </div>
        <div class="meta-item">
          <div class="meta-label">GCP Project <span class="mask-tag">obfuscated</span></div>
          <div class="meta-value" id="metaProjectId">{esc(MASKED_PROJECT_ID)}</div>
        </div>
        <div class="meta-item">
          <div class="meta-label">Active Fleet &amp; Shards</div>
          <div class="meta-value" id="metaFleetConfig">{esc(fleet_config or "Discovering...")}</div>
        </div>
        <div class="meta-item">
          <div class="meta-label">Edge Header (x-cell-id)</div>
          <div class="meta-value">{esc(routed_cell_header)}</div>
        </div>
        <div class="meta-item">
          <div class="meta-label">Isolated Spanner DB</div>
          <div class="meta-value">{esc(spanner_label)}</div>
        </div>
      </div>
    </header>

    <main>
      <!-- BOX 1: Architecture Blueprint -->
      <section class="panel" aria-labelledby="headingArch">
        <div class="panel-header">
          <div>
            <span class="step-kicker">Box 1 · Architecture Blueprint</span>
            <h2 id="headingArch">How this Google Cloud cell architecture works</h2>
          </div>
        </div>

        <div class="guide-box">
          <strong>What is happening under the hood?</strong> Instead of putting all customers into one shared cluster and database (where one "noisy neighbor" crash takes down 100% of customers), your stack is split into independent watertight compartments called <strong>cells</strong> (each with its own Cloud Run service, Spanner instance, and VPC). At the front door, <strong>shuffle sharding</strong> deals each customer a 2-cell "hand of cards" (a <strong>Primary cell</strong> + a <strong>Backup cell</strong>)—so if a customer's primary cell crashes, traffic automatically fails over to their backup cell!<br/>
          <strong>What to do here:</strong> Click through the 4 tabs below to inspect the end-to-end Google Cloud architecture, shuffle-sharding math, ingress modes, and wave rollouts.
        </div>

        <div class="arch-tabs" role="tablist">
          <button id="archTab0" class="arch-tab active" onclick="selectArchTab(0)">1. End-to-End Cell Architecture</button>
          <button id="archTab1" class="arch-tab" onclick="selectArchTab(1)">2. Why Shuffle Sharding Works (Alice vs. Tenant X)</button>
          <button id="archTab2" class="arch-tab" onclick="selectArchTab(2)">3. Global Anycast vs. Regional Ingress</button>
          <button id="archTab3" class="arch-tab" onclick="selectArchTab(3)">4. Cloud Deploy Wave Rollouts</button>
        </div>

        <figure class="arch-figure">
          <img id="archDiagramImg" src="/static/01_cell_reference_architecture.png" alt="Google Cloud cell-based reference architecture diagram" />
          <figcaption id="archDiagramCaption" class="arch-caption">
            <strong>End-to-end reference topology:</strong> Public requests pass through Cloud Armor and the Global External Application Load Balancer, where a stateless Proxy-Wasm plugin stamps each customer's cell hand without any database lookup. Cloud WAN (NCC STAR mode) connects enterprise branches while blocking sideways cell-to-cell traffic, and App Hub + Cloud Hub monitor all cells out-of-band.
          </figcaption>
        </figure>
      </section>

      <!-- BOX 2: Live Routing & Failover Visualizer -->
      <section class="panel" aria-labelledby="headingTopology">
        <div class="panel-header">
          <div>
            <span class="step-kicker">Box 2 · Live Interactive Traffic &amp; Chaos Diagram</span>
            <h2 id="headingTopology">Watch live requests route &amp; fail over across your cells</h2>
          </div>
          <div class="btn-row">
            <button id="btnGuidedDemo" class="btn-primary" onclick="runGuidedFailoverDemo()">
              ▶ Run Guided Failover Demo (4 Steps)
            </button>
            <button id="btnBlastSweep" class="btn-outline" onclick="runBlastRadiusSweep()">
              ⊞ Sweep 24 Customers
            </button>
          </div>
        </div>

        <div class="guide-box">
          <strong>What this diagram shows:</strong> Every arrow below is a <strong>real live HTTP request</strong> sent from your browser through this load balancer—never a fake animation. Whenever you crash or heal a cell, the diagram immediately re-sends the active customer's request so the arrows always match live reality.<br/>
          <strong>What to do here (try these 3 actions):</strong>
          <ol class="guide-steps">
            <li>Click <strong>"▶ Run Guided Failover Demo"</strong> (top right) to watch a noisy neighbor crash a shared primary cell while innocent customer <code>alice-corp</code> automatically fails over to her backup cell with zero downtime.</li>
            <li><strong>Click directly on any cell box on the right</strong> (for example, whichever cell says <code>1st Card</code>) to crash it with HTTP 503—watch the red blocked line appear on the crashed primary cell and the amber failover line rescue the request on the <code>2nd Card</code> backup cell!</li>
            <li>Click that red cell again (or click <strong>"✓ Heal All Cells"</strong> at the top) to heal it and watch traffic return cleanly to the green primary path.</li>
          </ol>
        </div>

        <div id="narrativeBanner" class="narrative-bar">
          <span id="narrativeText">Initializing live cell status across your Google Cloud fleet...</span>
          <span id="narrativeStep" style="font-family:var(--font-mono);font-size:12px;">LIVE</span>
        </div>

        <div class="topology-wrap">
          <svg id="topologySvg" viewBox="0 0 1160 440" role="img" aria-label="Live Google Cloud cell routing architecture diagram"></svg>
        </div>

        <div class="legend-bar">
          <span class="legend-item"><span class="dot" style="background:var(--green)"></span> <strong>Green path:</strong> Primary cell healthy (Attempt #0 → 200 OK)</span>
          <span class="legend-item"><span class="dot" style="background:var(--red)"></span> <strong>Red path:</strong> Primary cell poisoned &amp; blocked (Attempt #0 → 503)</span>
          <span class="legend-item"><span class="dot" style="background:var(--amber)"></span> <strong>Amber path:</strong> Automatic shuffle-shard failover to Backup cell (Retry #1 → 200 OK)</span>
          <span class="legend-item"><span class="dot" style="background:#9aa0a6"></span> <strong>Grey line:</strong> Cell not in this customer's hand (100% isolated)</span>
        </div>
      </section>

      <!-- BOX 3 & BOX 4 -->
      <div class="two-col">
        <section class="panel" style="margin-bottom:0;" aria-labelledby="headingCells">
          <div class="panel-header">
            <div>
              <span class="step-kicker">Box 3 · Per-Cell Chaos Controls</span>
              <h2 id="headingCells">Crash or heal individual Google Cloud cells</h2>
            </div>
          </div>

          <div class="guide-box">
            <strong>What this box does:</strong> Each card controls one physical cell (its Cloud Run service + dedicated Spanner database). Crashing a cell writes a poison marker into that cell's own Spanner ledger so all Cloud Run instances in that cell return <code>HTTP 503</code> (auto-heals in {AUTO_HEAL_SECONDS}s).<br/>
            <strong>What to try:</strong> Crash <strong>1 cell</strong>, then click <em>"Sweep 24 Customers"</em> in Box 4—you'll see <strong>100% of customers survive</strong>! Then crash a <strong>2nd cell</strong> and sweep again to see that only customers dealt <em>both</em> crashed cells are affected.
          </div>

          <div id="cellCardsContainer" class="cell-cards-grid"></div>
        </section>

        <section class="panel" style="margin-bottom:0;" aria-labelledby="headingSweep">
          <div class="panel-header">
            <div>
              <span class="step-kicker">Box 4 · Multi-Tenant Simulator &amp; Blast Matrix</span>
              <h2 id="headingSweep">Test any customer ID or sweep 24 customers</h2>
            </div>
          </div>

          <div class="guide-box">
            <strong>What to do here:</strong>
            <ul class="guide-steps">
              <li>Type any customer ID (e.g. <code>acme-bank</code>) to preview its 2-cell hand, then click <strong>Send Request</strong> to trace it in Box 2—or <strong>Poison Primary</strong> to simulate that customer crashing its primary cell.</li>
              <li>Below, view the <strong>24-customer blast matrix</strong>: <strong>Green</strong> = served on Primary, <strong>Amber</strong> = Primary crashed but <em>rescued by Backup cell</em>, <strong>Red</strong> = both cells in hand crashed. <strong>Click any tile</strong> to inspect that customer in Box 2!</li>
            </ul>
          </div>

          <div class="tenant-lab-box">
            <label for="customTenantInput" style="font-size:12px;font-weight:600;color:var(--text-secondary);">
              Customer Header (<code>X-Tenant-Id</code>)
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
              <div class="lbl">Customers Online</div>
            </div>
            <div class="stat-box">
              <div class="num" id="statRescued" style="color:var(--amber-ink);">—</div>
              <div class="lbl">Rescued by Backup</div>
            </div>
            <div class="stat-box">
              <div class="num" id="statImpacted" style="color:var(--red-ink);">—</div>
              <div class="lbl">Offline (All k Down)</div>
            </div>
          </div>

          <div id="tenantMatrix" class="tenant-matrix"></div>
        </section>
      </div>

      <!-- BOX 5: Live Audit Log -->
      <section class="panel" aria-labelledby="headingTrace">
        <div class="panel-header">
          <div>
            <span class="step-kicker">Box 5 · Live Edge &amp; Spanner Request Log</span>
            <h2 id="headingTrace">Inspect raw HTTP attempts, retries &amp; Spanner reads</h2>
          </div>
          <button id="btnClearLog" class="btn-outline" onclick="clearTraceLog()">Clear Log</button>
        </div>

        <div class="guide-box" style="margin-bottom:12px;">
          <strong>How to read this log:</strong> Every row is a real HTTP call to <code>/v1/infer</code>. During a failover, you will see <strong>two rows</strong> for the same customer: <code>Attempt #0</code> hitting the crashed primary cell (<code>503 CELL_UNAVAILABLE</code>), followed immediately by <code>Attempt #1</code> succeeding (<code>200 OK (Failover #1)</code>) on a healthy backup cell and reading a timestamp from that backup cell's isolated Spanner database.
        </div>

        <div style="overflow-x:auto;">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Customer (X-Tenant-Id)</th>
                <th>Attempt</th>
                <th>Serving Cell &amp; Region</th>
                <th>HTTP Outcome</th>
                <th>Latency</th>
                <th>Customer's Hand (1st → 2nd)</th>
                <th>Isolated Spanner Proof</th>
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
    const HASH_SALT = "gcp-cell-v1-prod";
    const MASK_64 = 0xffffffffffffffffn;

    const ARCH_TABS = [
      {{
        src: "/static/01_cell_reference_architecture.png",
        alt: "Google Cloud end-to-end cell reference architecture",
        caption: "<strong>End-to-end reference topology:</strong> Public requests pass through Cloud Armor and the Global External Application Load Balancer, where a stateless Proxy-Wasm plugin stamps each customer's cell hand without any database lookup. Cloud WAN (NCC STAR mode) connects enterprise branches while blocking sideways cell-to-cell traffic, and App Hub + Cloud Hub monitor all cells out-of-band."
      }},
      {{
        src: "/static/02_shuffle_sharding_combinatorics.png",
        alt: "Standard cell sharding compared with shuffle sharding",
        caption: "<strong>Why shuffle sharding beats 1-cell assignment:</strong> On the left (standard sharding), when Tenant X crashes cell-02, innocent neighbor Alice goes completely offline too. On the right (shuffle sharding), every customer gets a unique multi-cell combination—so when Tenant X crashes cell-02 and cell-05, Alice automatically fails over to cell-04 and Bob fails over to cell-07 with 200 OK!"
      }},
      {{
        src: "/static/03_global_vs_regional_alb_ingress.png",
        alt: "Global Anycast vs sovereign regional external Application Load Balancers",
        caption: "<strong>Choosing your front door:</strong> Option A (deployed live here) uses a single Global External ALB Anycast IP for sub-5-second cross-region cell failover. Option B uses Regional External ALBs per jurisdiction when strict data residency requires keeping TLS termination, URL maps, and Spanner inside one region."
      }},
      {{
        src: "/static/04_cloud_deploy_wave_pipeline.png",
        alt: "Progressive wave-by-wave rollout pipeline in Cloud Deploy",
        caption: "<strong>Containing bad deployments:</strong> Even isolated cells will fail together if you push a broken release everywhere at once. Cloud Deploy rolls updates out in waves—Canary (cell-00) → Wave 1 (cell-01) → Multi-cell waves—with automated SLO verification and instant rollback."
      }}
    ];

    let fleetN = 4;
    let shardK = 2;
    let cellsState = {{}};
    let pinTenantByCell = {{}};
    let activeTrace = null;
    let busyDemo = false;

    const SWEEP_TENANTS = [
      "alice-corp", "bob-retail", "charlie-bank", "delta-health",
      "echo-logistics", "foxtrot-ai", "globex-pay", "helios-bio",
      "initech-erp", "jupiter-sec", "kestrel-air", "lumina-med",
      "meridian-fx", "nimbus-cloud", "omnicorp-hq", "pinnacle-ins",
      "quantum-sim", "redwood-fin", "solaris-grid", "titan-mfg",
      "umbrella-rx", "vanguard-ops", "waypoint-nav", "zenith-labs"
    ];

    function selectArchTab(idx) {{
      const item = ARCH_TABS[idx] || ARCH_TABS[0];
      document.getElementById("archDiagramImg").src = item.src;
      document.getElementById("archDiagramImg").alt = item.alt;
      document.getElementById("archDiagramCaption").innerHTML = item.caption;
      for (let i = 0; i < ARCH_TABS.length; i++) {{
        const btn = document.getElementById(`archTab${{i}}`);
        if (btn) btn.className = "arch-tab" + (i === idx ? " active" : "");
      }}
    }}

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
        `${{fleetN}} cells (k=${{shardK}}) → ${{combos}} shards (${{overlapPct}}% overlap)`;
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

    function getActiveTenantId() {{
      if (activeTrace && activeTrace.tenantId) return activeTrace.tenantId;
      const inp = document.getElementById("customTenantInput");
      return (inp && inp.value.trim()) || "alice-corp";
    }}

    async function refreshAllCells(reprobeIfChanged = false) {{
      const prevSignature = JSON.stringify(
        Object.keys(cellsState).sort().map((k) => [k, cellsState[k].healthy])
      );
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
      const nextSignature = JSON.stringify(
        Object.keys(cellsState).sort().map((k) => [k, cellsState[k].healthy])
      );
      renderCellCards();
      renderLiveBlastMatrixPreview();
      updateCustomHandPreview();

      // If any cell changed health (e.g. auto-heal expired), re-probe active tenant so SVG arrows never go stale!
      if (reprobeIfChanged && prevSignature !== "[]" && prevSignature !== nextSignature && !busyDemo) {{
        await routeTenantWithShuffleFailover(getActiveTenantId(), {{
          poison: false,
          animate: true,
          pauseBetweenRetriesMs: 250
        }});
      }} else {{
        renderTopologySvg();
      }}
    }}

    // Crash or heal a cell AND immediately re-route live traffic so arrows always match cell state!
    async function toggleCellChaos(cellId) {{
      if (busyDemo) return;
      const cur = cellsState[cellId];
      const shouldPoison = !cur || cur.healthy;
      const currentTid = getActiveTenantId();

      if (shouldPoison) {{
        setNarrative(`Crashing ${{cellId}} (writing poison marker to ${{cellId}}-spanner)...`, "warn", "CHAOS");
        await callSpecificCell(cellId, "/chaos/poison", "POST", {{ actor: "manual-chaos-click" }});
        await refreshAllCells(false);

        // If current tenant doesn't use cellId as Primary, pick a tenant whose Primary IS cellId so the user sees immediate failover!
        let probeTid = currentTid;
        const curHand = computeVirtualShard(currentTid, fleetN, shardK).map(formatCellId);
        if (curHand[0] !== cellId) {{
          const match = SWEEP_TENANTS.find(
            (t) => formatCellId(computeVirtualShard(t, fleetN, shardK)[0]) === cellId
          );
          if (match) {{
            probeTid = match;
            document.getElementById("customTenantInput").value = probeTid;
            updateCustomHandPreview();
          }}
        }}

        const res = await routeTenantWithShuffleFailover(probeTid, {{
          poison: false,
          animate: true,
          pauseBetweenRetriesMs: 380
        }});
        await refreshAllCells(false);

        if (res.finalStatus === 200 && res.rescuedByFailover) {{
          const backupCell = res.attempts[res.attempts.length - 1].cellId;
          setNarrative(
            `${{cellId}} is CRASHED (HTTP 503) — live probe for '${{probeTid}}' hit ${{cellId}} (#0 Blocked) and automatically failed over to backup ${{backupCell}} (200 OK)!`,
            "ok",
            "FAILOVER OK"
          );
        }} else if (res.finalStatus !== 200) {{
          setNarrative(
            `${{cellId}} is CRASHED — both cells in '${{probeTid}}'s hand (${{res.handIds.join(" & ")}}) are now down, returning HTTP 503!`,
            "err",
            "503 SHARD DOWN"
          );
        }} else {{
          setNarrative(
            `${{cellId}} is CRASHED (HTTP 503). '${{probeTid}}' stays healthy on ${{res.attempts[0].cellId}}.`,
            "warn",
            "CELL DOWN"
          );
        }}
      }} else {{
        setNarrative(`Healing ${{cellId}} in Spanner and verifying live traffic recovery...`, "info", "HEALING");
        await callSpecificCell(cellId, "/chaos/reset", "POST", {{}});
        await refreshAllCells(false);

        // Immediately re-route current tenant so stale 503 failover arrows update to green 200 OK!
        const res = await routeTenantWithShuffleFailover(currentTid, {{
          poison: false,
          animate: true,
          pauseBetweenRetriesMs: 250
        }});
        await refreshAllCells(false);

        setNarrative(
          `${{cellId}} healed to SERVING! Live request for '${{currentTid}}' now routes cleanly to ${{res.attempts[res.attempts.length - 1].cellId}} (HTTP 200 OK).`,
          "ok",
          "HEALTHY"
        );
      }}
    }}

    async function healAllCells() {{
      setNarrative("Healing all cells across your Google Cloud fleet...", "info", "RESETTING");
      const tasks = [];
      for (let i = 0; i < fleetN; i++) {{
        tasks.push(callSpecificCell(formatCellId(i), "/chaos/reset", "POST", {{}}).catch(() => null));
      }}
      await Promise.all(tasks);
      await refreshAllCells(false);
      const tid = getActiveTenantId();
      await routeTenantWithShuffleFailover(tid, {{ poison: false, animate: true, pauseBetweenRetriesMs: 200 }});
      await refreshAllCells(false);
      setNarrative(`All ${{fleetN}} cells restored to SERVING (100% healthy). '${{tid}}' is routing on its primary cell.`, "ok", "ALL HEALTHY");
    }}

    function appendTraceRow(entry) {{
      const tbody = document.getElementById("traceTableBody");
      const tr = document.createElement("tr");
      const badgeStyle = entry.status === 200
        ? (entry.attempt > 0 ? "background:var(--amber-soft);color:var(--amber-ink);" : "background:var(--green-soft);color:var(--green-ink);")
        : "background:var(--red-soft);color:var(--red-ink);";
      const statusLabel = entry.status === 200
        ? (entry.attempt > 0 ? `200 OK (Failover #${{entry.attempt}})` : "200 OK (Primary)")
        : `${{entry.status}} ${{entry.error || "UNAVAILABLE"}}`;

      const cols = [
        {{ text: new Date().toLocaleTimeString(), mono: true }},
        {{ text: entry.tenantId, mono: true }},
        {{ text: `Attempt #${{entry.attempt}}`, mono: true }},
        {{ text: `${{entry.cellId}} (${{entry.region || "-"}})`, mono: true }},
        {{ htmlBadge: statusLabel, style: badgeStyle }},
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

    async function routeTenantWithShuffleFailover(tenantId, options = {{ poison: false, animate: true, pauseBetweenRetriesMs: 400 }}) {{
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
      previewEl.appendChild(document.createTextNode("Assigned Hand → 1st (Primary): "));
      const c0 = document.createElement("code");
      c0.textContent = hand[0] || "-";
      previewEl.appendChild(c0);
      if (hand.length > 1) {{
        previewEl.appendChild(document.createTextNode(" · 2nd (Backup): "));
        const cRest = document.createElement("code");
        cRest.textContent = hand.slice(1).join(" → ");
        previewEl.appendChild(cRest);
      }}
    }}

    async function sendCustomTenant(poison) {{
      const tid = (document.getElementById("customTenantInput").value || "anonymous").trim();
      if (poison) {{
        setNarrative(`Sending poison-pill payload (X-Poison-Payload: crash) from '${{tid}}'...`, "warn", "POISON");
        const res = await routeTenantWithShuffleFailover(tid, {{ poison: true, animate: true, pauseBetweenRetriesMs: 0 }});
        await refreshAllCells(false);
        const crashedCell = res.attempts[0].cellId;
        setNarrative(
          `Poison pill from '${{tid}}' crashed primary ${{crashedCell}} (503)! Now re-sending normal traffic for '${{tid}}' so you can watch backup failover...`,
          "warn",
          "FAILOVER"
        );
        await new Promise((r) => setTimeout(r, 600));
        const followUp = await routeTenantWithShuffleFailover(tid, {{ poison: false, animate: true, pauseBetweenRetriesMs: 420 }});
        await refreshAllCells(false);
        if (followUp.finalStatus === 200) {{
          setNarrative(
            `Primary ${{crashedCell}} is down (503), so '${{tid}}' automatically failed over to backup ${{followUp.attempts[followUp.attempts.length - 1].cellId}} → 200 OK!`,
            "ok",
            "RESCUED"
          );
        }}
      }} else {{
        setNarrative(`Routing live HTTP request for X-Tenant-Id: '${{tid}}'...`, "info", "ROUTING");
        const res = await routeTenantWithShuffleFailover(tid, {{ poison: false, animate: true, pauseBetweenRetriesMs: 420 }});
        await refreshAllCells(false);
        if (res.finalStatus === 200 && res.rescuedByFailover) {{
          const failedCell = res.attempts[0].cellId;
          const okCell = res.attempts[res.attempts.length - 1].cellId;
          setNarrative(
            `Zero-downtime failover! Primary ${{failedCell}} returned 503, so Retry #1 routed '${{tid}}' to healthy backup ${{okCell}} → HTTP 200 OK.`,
            "ok",
            "RESCUED"
          );
        }} else if (res.finalStatus === 200) {{
          setNarrative(
            `Served '${{tid}}' cleanly on primary ${{res.attempts[0].cellId}} → HTTP 200 OK.`,
            "ok",
            "200 OK"
          );
        }} else {{
          setNarrative(
            `All ${{shardK}} cells in '${{tid}}'s hand (${{res.handIds.join(", ")}}) are currently poisoned → HTTP 503.`,
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
        const noisyTenant = "noisy-tenant-x";
        const noisyHand = computeVirtualShard(noisyTenant, fleetN, shardK);
        let aliceTenant = "alice-corp";
        for (const cand of ["alice-corp", ...SWEEP_TENANTS, "alice-fin", "alice-saas"]) {{
          const h = computeVirtualShard(cand, fleetN, shardK);
          if (h[0] === noisyHand[0] && h.some((c) => c !== noisyHand[0])) {{
            aliceTenant = cand;
            break;
          }}
        }}
        document.getElementById("customTenantInput").value = aliceTenant;
        updateCustomHandPreview();

        const aliceHandIds = computeVirtualShard(aliceTenant, fleetN, shardK).map(formatCellId);
        const sharedPrimary = formatCellId(noisyHand[0]);

        setNarrative("Step 1/4: Resetting all cells to SERVING so we start from a clean 100% healthy baseline...", "info", "STEP 1/4");
        const resets = [];
        for (let i = 0; i < fleetN; i++) {{
          resets.push(callSpecificCell(formatCellId(i), "/chaos/reset", "POST", {{}}).catch(() => null));
        }}
        await Promise.all(resets);
        await refreshAllCells(false);
        await new Promise((r) => setTimeout(r, 500));

        setNarrative(
          `Step 2/4: Normal traffic — customer '${{aliceTenant}}' (hand: ${{aliceHandIds.join(" → ")}}) routes to primary ${{sharedPrimary}} → HTTP 200 OK.`,
          "info",
          "STEP 2/4"
        );
        await routeTenantWithShuffleFailover(aliceTenant, {{ poison: false, animate: true, pauseBetweenRetriesMs: 350 }});
        await refreshAllCells(false);
        await new Promise((r) => setTimeout(r, 1100));

        setNarrative(
          `Step 3/4: Poison-pill bug! '${{noisyTenant}}' shares primary ${{sharedPrimary}} with '${{aliceTenant}}' and crashes ${{sharedPrimary}} (HTTP 503)...`,
          "warn",
          "STEP 3/4"
        );
        await routeTenantWithShuffleFailover(noisyTenant, {{ poison: true, animate: true, pauseBetweenRetriesMs: 0 }});
        await refreshAllCells(false);
        await new Promise((r) => setTimeout(r, 1100));

        setNarrative(
          `Step 4/4: Sending '${{aliceTenant}}' again — Attempt #0 hits crashed ${{sharedPrimary}} (503), and Retry #1 automatically fails over to backup ${{aliceHandIds[1]}}!`,
          "info",
          "STEP 4/4"
        );
        const failoverRes = await routeTenantWithShuffleFailover(aliceTenant, {{
          poison: false,
          animate: true,
          pauseBetweenRetriesMs: 650
        }});
        await refreshAllCells(false);

        const backupCell = failoverRes.attempts[failoverRes.attempts.length - 1].cellId;
        setNarrative(
          `Demo complete! Shared primary ${{sharedPrimary}} is still crashed (503), yet '${{aliceTenant}}' succeeded with HTTP 200 OK on backup ${{backupCell}} — zero downtime!`,
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
        setNarrative("Sending live HTTP requests for 24 distinct customers through the Anycast Cell Router...", "info", "SWEEPING");
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
          tile.title = `Click to visualize ${{r.tenantId}} (${{r.handIds.join(" → ")}}) in Box 2`;
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
        document.getElementById("statAvailability").textContent = `${{okCount}}/${{SWEEP_TENANTS.length}} (${{availPct}})`;
        document.getElementById("statRescued").textContent = `${{rescuedCount}} customers`;
        document.getElementById("statImpacted").textContent = `${{downCount}} customers`;

        // Also update Box 2 to show an interesting rescued or impacted customer from the sweep
        const highlight = results.find((r) => r.rescuedByFailover) || results.find((r) => r.finalStatus !== 200) || results[0];
        if (highlight) {{
          activeTrace = highlight;
        }}
        await refreshAllCells(false);
        setNarrative(
          `24-customer sweep complete: ${{okCount}}/${{SWEEP_TENANTS.length}} (${{availPct}}) online — ${{rescuedCount}} rescued via Backup cells, ${{downCount}} offline. Click any customer tile to view its path!`,
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
          ? "● SERVING (200)"
          : `✕ CRASHED (${{st.auto_heal_in_sec || 0}}s)`;
        head.appendChild(title);
        head.appendChild(pill);

        const meta = document.createElement("div");
        meta.className = "cell-meta";
        const rows = [
          ["Region", st.region],
          ["Spanner DB", st.spanner_instance],
          ["Probe Latency", st.latency_ms ? `${{st.latency_ms}} ms` : "—"],
          ["Status", st.healthy ? "Healthy & Isolated" : `Poisoned (${{st.poisoned_by || "chaos"}})`]
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
        btn.textContent = st.healthy ? `⚡ Crash ${{cid}} (Simulate 503)` : `✓ Heal ${{cid}} (Restore 200)`;
        btn.onclick = () => toggleCellChaos(cid);

        card.appendChild(head);
        card.appendChild(meta);
        card.appendChild(btn);
        container.appendChild(card);
      }}
    }}

    // Keeps the 24-customer matrix in Box 4 live-synced with current cell health (and clicking any tile sends a real HTTP probe!)
    function renderLiveBlastMatrixPreview() {{
      const matrixEl = document.getElementById("tenantMatrix");
      if (!matrixEl) return;
      matrixEl.innerHTML = "";

      let okCount = 0;
      let rescuedCount = 0;
      let downCount = 0;

      for (const tid of SWEEP_TENANTS) {{
        const handIds = computeVirtualShard(tid, fleetN, shardK).map(formatCellId);
        let servingCell = null;
        let attemptUsed = 0;
        for (let i = 0; i < handIds.length; i++) {{
          const cid = handIds[i];
          const st = cellsState[cid];
          if (!st || st.healthy !== false) {{
            servingCell = cid;
            attemptUsed = i;
            break;
          }}
        }}

        const tile = document.createElement("div");
        let cls = "primary-ok";
        let label = `200 · ${{servingCell}}`;
        if (!servingCell) {{
          cls = "shard-down";
          label = `503 · [${{handIds.join(",")}}]`;
          downCount++;
        }} else if (attemptUsed > 0) {{
          cls = "failover-ok";
          label = `↻ 200 · ${{servingCell}}`;
          okCount++;
          rescuedCount++;
        }} else {{
          okCount++;
        }}

        tile.className = `tenant-tile ${{cls}}`;
        tile.title = `Click to send live HTTP request for ${{tid}} (${{handIds.join(" → ")}})`;
        tile.onclick = () => {{
          document.getElementById("customTenantInput").value = tid;
          updateCustomHandPreview();
          sendCustomTenant(false);
        }};

        const tName = document.createElement("div");
        tName.className = "t-name";
        tName.textContent = tid;
        const tSub = document.createElement("div");
        tSub.className = "t-sub";
        tSub.textContent = label;
        tile.appendChild(tName);
        tile.appendChild(tSub);
        matrixEl.appendChild(tile);
      }}

      const availPct = ((100 * okCount) / SWEEP_TENANTS.length).toFixed(1) + "%";
      document.getElementById("statAvailability").textContent = `${{okCount}}/${{SWEEP_TENANTS.length}} (${{availPct}})`;
      document.getElementById("statRescued").textContent = `${{rescuedCount}} customers`;
      document.getElementById("statImpacted").textContent = `${{downCount}} customers`;
    }}

    function escSvg(str) {{
      return String(str || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }}

    // Spacious 1160px GCP Architecture Center style SVG with dedicated horizontal entry lanes (zero overlapping!)
    function renderTopologySvg() {{
      const svg = document.getElementById("topologySvg");
      const rowH = fleetN <= 4 ? 94 : 62;
      const topPad = 40;
      const svgH = Math.max(430, topPad + fleetN * rowH + 20);
      svg.setAttribute("viewBox", `0 0 1160 ${{svgH}}`);

      const midY = Math.round(topPad + (fleetN * rowH) / 2 - 6);
      const clientX = 16, clientW = 248, clientH = 152, clientY = midY - 76;
      const albX = 336, albW = 256, albH = 172, albY = midY - 86;
      const cellX = 808, cellW = 336, cellBoxH = fleetN <= 4 ? 76 : 50;
      const laneStartX = 688; // Bezier curves finish at x=688; flat horizontal lane runs x=688..808!

      const cellCenters = {{}};
      for (let i = 0; i < fleetN; i++) {{
        const cid = formatCellId(i);
        const y = topPad + i * rowH + Math.round((rowH - cellBoxH) / 2);
        cellCenters[cid] = {{ x: cellX, y, cy: Math.round(y + cellBoxH / 2), idx: i }};
      }}

      const trace = activeTrace;
      const activeTenant = trace ? trace.tenantId : getActiveTenantId();
      const activeHand = trace ? trace.handIds : computeVirtualShard(activeTenant, fleetN, shardK).map(formatCellId);
      const combos = nChooseK(fleetN, shardK);
      const lastServingCell = trace && trace.attempts && trace.attempts.length
        ? trace.attempts[trace.attempts.length - 1].cellId
        : (activeHand[0] || "cell-00");

      let bgPaths = "";
      let activePaths = "";
      let badgesLayer = "";

      // 1. Subtle background routing fan-out to all N cells
      for (let i = 0; i < fleetN; i++) {{
        const cid = formatCellId(i);
        const c = cellCenters[cid];
        const sx = albX + albW;
        const sy = midY;
        const ey = c.cy;
        const inHand = activeHand.includes(cid);
        const d = `M ${{sx}} ${{sy}} C ${{sx + 48}} ${{sy}}, ${{laneStartX - 44}} ${{ey}}, ${{laneStartX}} ${{ey}} L ${{c.x}} ${{ey}}`;
        bgPaths += `<path d="${{d}}" fill="none" stroke="${{inHand ? "#bfd6fb" : "#e6e8ec"}}" stroke-width="${{inHand ? "1.8" : "1.2"}}" stroke-dasharray="${{inHand ? "4 4" : "none"}}" />`;
      }}

      // 2. Active HTTP request trajectories + straight-lane callout pills
      if (trace && trace.attempts && trace.attempts.length > 0) {{
        const inSx = clientX + clientW;
        const inEx = albX;
        activePaths += `
          <path d="M ${{inSx}} ${{midY}} L ${{inEx}} ${{midY}}" stroke="#1a73e8" stroke-width="2.8" class="anim-flow" />
          <polygon points="${{inEx}},${{midY}} ${{inEx - 7}},${{midY - 4.5}} ${{inEx - 7}},${{midY + 4.5}}" fill="#1a73e8" />
          <circle r="4.5" fill="#1a73e8">
            <animateMotion dur="0.75s" repeatCount="indefinite" path="M ${{inSx}} ${{midY}} L ${{inEx}} ${{midY}}" />
          </circle>
          <text x="${{Math.round((inSx + inEx) / 2)}}" y="${{midY - 10}}" text-anchor="middle" font-family="Inter, sans-serif" font-size="10" font-weight="700" fill="#174ea6">HTTPS POST</text>
        `;

        trace.attempts.forEach((att) => {{
          const target = cellCenters[att.cellId];
          if (!target) return;
          const sx = albX + albW;
          const sy = midY;
          const ex = target.x;
          const ey = target.cy;
          const d = `M ${{sx}} ${{sy}} C ${{sx + 48}} ${{sy}}, ${{laneStartX - 44}} ${{ey}}, ${{laneStartX}} ${{ey}} L ${{ex}} ${{ey}}`;

          const isOk = att.status === 200;
          const isFailover = isOk && att.attempt > 0;
          const stroke = !isOk ? "#d93025" : (isFailover ? "#e37400" : "#1e8e3e");
          const pillBg = !isOk ? "#fce8e6" : (isFailover ? "#fef7e0" : "#e6f4ea");
          const pillFg = !isOk ? "#c5221f" : (isFailover ? "#b06000" : "#137333");
          const pillTxt = !isOk
            ? `✕ #${{att.attempt}} · 503 Blocked`
            : (isFailover ? `↻ Retry #${{att.attempt}} · 200 OK` : `✓ #0 · 200 OK`);

          activePaths += `
            <path d="${{d}}" fill="none" stroke="${{stroke}}" stroke-width="3" class="anim-flow" />
            <polygon points="${{ex}},${{ey}} ${{ex - 8}},${{ey - 5}} ${{ex - 8}},${{ey + 5}}" fill="${{stroke}}" />
            <circle r="5" fill="${{stroke}}">
              <animateMotion dur="${{isOk ? "0.95s" : "0.65s"}}" repeatCount="indefinite" path="${{d}}" />
            </circle>
          `;

          // Pill sits wholly on the flat horizontal runway [x=692..796], well right of all S-curves and 12px left of cell border!
          badgesLayer += `
            <g transform="translate(692, ${{ey - 11}})">
              <rect width="104" height="22" rx="11" fill="#ffffff" />
              <rect width="104" height="22" rx="11" fill="${{pillBg}}" stroke="${{stroke}}" stroke-width="1.2" />
              <text x="52" y="14.5" text-anchor="middle" font-family="Inter, sans-serif" font-size="9.8" font-weight="700" fill="${{pillFg}}">${{pillTxt}}</text>
            </g>
          `;
        }});
      }}

      // 3. Isolated Cell Bulkhead Nodes (x=808..1144, width=336)
      let cellsMarkup = "";
      for (let i = 0; i < fleetN; i++) {{
        const cid = formatCellId(i);
        const c = cellCenters[cid];
        const st = cellsState[cid] || {{ healthy: true, region: i % 2 === 0 ? "us-central1" : "us-east4", spanner_instance: `${{cid}}-spanner` }};
        const healthy = st.healthy !== false;
        const fill = healthy ? "#ffffff" : "#fff7f7";
        const border = healthy ? (activeHand.includes(cid) ? "#1a73e8" : "#dadce0") : "#d93025";
        const accentFill = healthy ? "#1e8e3e" : "#d93025";
        const badgeBg = healthy ? "#e6f4ea" : "#fce8e6";
        const badgeFg = healthy ? "#137333" : "#c5221f";
        const badgeTxt = healthy ? "● SERVING" : "✕ 503 DOWN";

        const roleIdx = activeHand.indexOf(cid);
        const roleTxt = roleIdx === 0 ? "1st Card" : (roleIdx === 1 ? "2nd Card" : (roleIdx > 1 ? `Card #${{roleIdx + 1}}` : "Isolated"));
        const roleBg = roleIdx === 0 ? "#e8f0fe" : (roleIdx > 0 ? "#fef7e0" : "#f1f3f4");
        const roleFg = roleIdx === 0 ? "#174ea6" : (roleIdx > 0 ? "#b06000" : "#5f6368");

        if (fleetN <= 4) {{
          const clickHint = healthy ? "[Click to crash]" : "[Click to heal]";
          const hintColor = healthy ? "#5f6368" : "#137333";
          cellsMarkup += `
            <g class="svg-cell-node" onclick="toggleCellChaos('${{cid}}')" transform="translate(${{c.x}}, ${{c.y}})">
              <rect class="cell-frame" width="${{cellW}}" height="${{cellBoxH}}" rx="10" fill="${{fill}}" stroke="${{border}}" stroke-width="${{healthy ? "1.6" : "2.2"}}" />
              <rect x="0" y="10" width="4" height="${{cellBoxH - 20}}" rx="2" fill="${{accentFill}}" />

              <!-- Row 1: Cell ID + Region + Role Card + Status Pill (strictly non-overlapping X coordinates) -->
              <text x="16" y="27" font-family="JetBrains Mono, monospace" font-size="13.5" font-weight="700" fill="#202124">${{cid}}</text>
              <text x="84" y="26.5" font-family="Inter, sans-serif" font-size="11" fill="#5f6368">${{escSvg(st.region)}}</text>

              <rect x="170" y="12" width="64" height="20" rx="5" fill="${{roleBg}}" />
              <text x="202" y="25.5" text-anchor="middle" font-family="Inter, sans-serif" font-size="10" font-weight="700" fill="${{roleFg}}">${{roleTxt}}</text>

              <rect x="242" y="12" width="82" height="20" rx="10" fill="${{badgeBg}}" />
              <text x="283" y="25.5" text-anchor="middle" font-family="Inter, sans-serif" font-size="10" font-weight="700" fill="${{badgeFg}}">${{badgeTxt}}</text>

              <!-- Row 2: Official Cloud Run + Spanner icons + click action hint -->
              <use href="#gcp-run" x="15" y="43" width="16" height="16" />
              <text x="35" y="55" font-family="Inter, sans-serif" font-size="11" fill="#3c4043">Cloud Run</text>
              <use href="#gcp-spanner" x="98" y="43" width="16" height="16" />
              <text x="117" y="55" font-family="JetBrains Mono, monospace" font-size="10" fill="#5f6368">${{escSvg(st.spanner_instance)}}</text>
              <text x="324" y="55" text-anchor="end" font-family="Inter, sans-serif" font-size="10" font-weight="600" fill="${{hintColor}}">${{clickHint}}</text>
            </g>
          `;
        }} else {{
          cellsMarkup += `
            <g class="svg-cell-node" onclick="toggleCellChaos('${{cid}}')" transform="translate(${{c.x}}, ${{c.y}})">
              <rect class="cell-frame" width="${{cellW}}" height="${{cellBoxH}}" rx="8" fill="${{fill}}" stroke="${{border}}" stroke-width="1.6" />
              <rect x="0" y="8" width="4" height="${{cellBoxH - 16}}" rx="2" fill="${{accentFill}}" />
              <text x="16" y="30" font-family="JetBrains Mono, monospace" font-size="12.5" font-weight="700" fill="#202124">${{cid}}</text>
              <text x="82" y="29.5" font-family="Inter, sans-serif" font-size="11" fill="#5f6368">${{escSvg(st.region)}}</text>
              <rect x="168" y="15" width="64" height="20" rx="5" fill="${{roleBg}}" />
              <text x="200" y="28.5" text-anchor="middle" font-family="Inter, sans-serif" font-size="10" font-weight="700" fill="${{roleFg}}">${{roleTxt}}</text>
              <rect x="242" y="15" width="82" height="20" rx="10" fill="${{badgeBg}}" />
              <text x="283" y="28.5" text-anchor="middle" font-family="Inter, sans-serif" font-size="10" font-weight="700" fill="${{badgeFg}}">${{badgeTxt}}</text>
            </g>
          `;
        }}
      }}

      const outcomeStatus = trace ? trace.finalStatus : 200;
      const usedFailover = trace && trace.attempts && trace.attempts.length > 1 && outcomeStatus === 200;
      const outcomeBg = outcomeStatus !== 200 ? "#fce8e6" : (usedFailover ? "#fef7e0" : "#e6f4ea");
      const outcomeFg = outcomeStatus !== 200 ? "#c5221f" : (usedFailover ? "#b06000" : "#137333");
      const outcomeLabel = trace
        ? (outcomeStatus === 200
            ? (usedFailover ? "↻ HTTP 200 (Backup Rescued!)" : "✓ HTTP 200 (Primary OK)")
            : "✕ HTTP 503 (Both Cells Down)")
        : "Ready to Route";

      const displayTenant = activeTenant.length > 18 ? activeTenant.slice(0, 17) + "…" : activeTenant;

      svg.innerHTML = `
        <text x="${{clientX}}" y="20" font-family="Inter, sans-serif" font-size="11" font-weight="700" fill="#5f6368">1. MULTI-TENANT CUSTOMER</text>
        <text x="${{albX}}" y="20" font-family="Inter, sans-serif" font-size="11" font-weight="700" fill="#5f6368">2. ANYCAST FRONT DOOR (PROXY-WASM)</text>
        <text x="${{cellX}}" y="20" font-family="Inter, sans-serif" font-size="11" font-weight="700" fill="#5f6368">3. ISOLATED CELLS (CLICK ANY CELL TO CRASH / HEAL)</text>

        ${{bgPaths}}
        ${{activePaths}}

        <!-- Stage 1: Customer Request Node -->
        <g transform="translate(${{clientX}}, ${{clientY}})">
          <rect width="${{clientW}}" height="${{clientH}}" rx="10" fill="#ffffff" stroke="#dadce0" stroke-width="1.6" />
          <text x="16" y="27" font-family="Inter, sans-serif" font-size="12.5" font-weight="700" fill="#202124">SaaS Customer Request</text>
          <rect x="14" y="37" width="${{clientW - 28}}" height="26" rx="5" fill="#f1f3f4" />
          <text x="22" y="54" font-family="JetBrains Mono, monospace" font-size="11.5" font-weight="600" fill="#174ea6">Tenant: ${{escSvg(displayTenant)}}</text>
          <text x="16" y="83" font-family="Inter, sans-serif" font-size="11.5" fill="#5f6368">Dealt Hand: <tspan font-family="JetBrains Mono, monospace" font-weight="600" fill="#202124">${{activeHand.join(" → ")}}</tspan></text>
          <rect x="14" y="100" width="${{clientW - 28}}" height="34" rx="7" fill="${{outcomeBg}}" />
          <text x="${{Math.round(clientW / 2)}}" y="121" text-anchor="middle" font-family="Inter, sans-serif" font-size="11.5" font-weight="700" fill="${{outcomeFg}}">${{outcomeLabel}}</text>
        </g>

        <!-- Stage 2: Global External ALB + Cloud Armor + Proxy-Wasm -->
        <g transform="translate(${{albX}}, ${{albY}})">
          <rect width="${{albW}}" height="${{albH}}" rx="10" fill="#ffffff" stroke="#1a73e8" stroke-width="1.8" />
          <rect x="12" y="12" width="${{albW - 24}}" height="30" rx="6" fill="#e8f0fe" />
          <use href="#gcp-clb" x="18" y="17" width="20" height="20" />
          <text x="43" y="31.5" font-family="Inter, sans-serif" font-size="11.8" font-weight="700" fill="#174ea6">Global External ALB (Anycast)</text>

          <use href="#gcp-armor" x="16" y="52" width="16" height="16" />
          <text x="37" y="64" font-family="Inter, sans-serif" font-size="11.5" fill="#3c4043">Cloud Armor per-tenant throttle</text>

          <text x="16" y="88" font-family="Inter, sans-serif" font-size="12" font-weight="700" fill="#202124">Service Extensions (Rust Wasm)</text>
          <text x="16" y="107" font-family="JetBrains Mono, monospace" font-size="11" fill="#5f6368">Deals k=${{shardK}} of N=${{fleetN}} (${{combos}} unique shards)</text>

          <rect x="12" y="122" width="${{albW - 24}}" height="34" rx="6" fill="#f8f9fa" stroke="#e0e3e7" />
          <text x="${{Math.round(albW / 2)}}" y="143" text-anchor="middle" font-family="JetBrains Mono, monospace" font-size="11" font-weight="600" fill="#202124">Stamps x-cell-id: ${{escSvg(lastServingCell)}}</text>
        </g>

        ${{cellsMarkup}}
        ${{badgesLayer}}
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
      await refreshAllCells(false);

      // Run initial live probe right on load so Box 2, Box 4, and Box 5 immediately show real data!
      const initRes = await routeTenantWithShuffleFailover("alice-corp", {{ poison: false, animate: true, pauseBetweenRetriesMs: 300 }});
      await refreshAllCells(false);

      if (initRes.rescuedByFailover) {{
        const failedCell = initRes.attempts[0].cellId;
        const okCell = initRes.attempts[initRes.attempts.length - 1].cellId;
        setNarrative(
          `Active chaos detected: primary ${{failedCell}} is down (HTTP 503), so 'alice-corp' automatically failed over to backup ${{okCell}} → HTTP 200 OK!`,
          "ok",
          "FAILOVER OK"
        );
      }} else if (initRes.finalStatus !== 200) {{
        setNarrative(
          `Active chaos detected: all cells in 'alice-corp's hand (${{initRes.handIds.join(" & ")}}) are down (503). Click '✓ Heal All Cells' to restore.`,
          "err",
          "503 DOWN"
        );
      }} else {{
        setNarrative(
          "Live & connected! Click any cell box on the right to crash it with HTTP 503 and watch automatic backup failover, or click '▶ Run Guided Failover Demo'.",
          "ok",
          "READY"
        );
      }}

      setInterval(() => {{
        if (!document.hidden && !busyDemo) {{
          refreshAllCells(true);
        }}
      }}, 6000);
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

    def _send_png(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("X-Served-By-Cell", CELL_ID)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        clean_path = self.path.split("?", 1)[0]

        if clean_path.startswith("/static/"):
            filename = clean_path.removeprefix("/static/")
            png_bytes = _load_static_png(filename)
            if png_bytes is not None:
                self._send_png(png_bytes)
                return
            self._send_json(404, {"error": "STATIC_ASSET_NOT_FOUND"})
            return

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
