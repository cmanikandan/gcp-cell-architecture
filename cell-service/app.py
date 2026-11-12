#!/usr/bin/env python3
"""Cell-isolated multi-tenant workload service for Cloud Run and GKE.

Supports:
- Cell-scoped Spanner reads/writes when SPANNER_INSTANCE and SPANNER_DATABASE are set
  (auto-discovers GOOGLE_CLOUD_PROJECT via environment or Cloud Run metadata server, and
  falls back cleanly to an in-memory cell store for local blast-radius chaos testing).
- Deterministic poison-pill fault simulation (`X-Poison-Payload: crash`) to model an
  unhandled OOM / deadlock / thread-pool exhaustion bug triggered by a single tenant.
- Health readiness endpoint (`/healthz`) polled by Cloud Load Balancing & GKE/Cloud Run.
- Interactive browser status & live shuffle-shard explorer when opened in a web browser.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

CELL_ID = os.environ.get("CELL_ID", "cell-00")
REGION = os.environ.get("CELL_REGION", "us-central1")
SPANNER_INSTANCE = os.environ.get("SPANNER_INSTANCE", "")
SPANNER_DATABASE = os.environ.get("SPANNER_DATABASE", "cell_ledger")
APP_VERSION = os.environ.get("APP_VERSION", "v1.4.1")


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


PROJECT_ID = _detect_project_id()

_state_lock = threading.Lock()
_cell_state: dict[str, Any] = {
    "healthy": True,
    "poisoned_by_tenant": None,
    "poisoned_at": None,
    "requests_served": 0,
    "tenant_hits": {},
}

_spanner_db = None
if SPANNER_INSTANCE:
    try:
        from google.cloud import spanner  # type: ignore

        _spanner_client = spanner.Client(project=PROJECT_ID)
        _spanner_db = _spanner_client.instance(SPANNER_INSTANCE).database(SPANNER_DATABASE)
    except Exception as exc:  # pragma: no cover - optional runtime dependency
        print(f"[{CELL_ID}] Spanner client unavailable ({exc}); using local cell memory store.")


def _record_tenant_op(tenant_id: str) -> str:
    """Reads transaction timestamp from this cell's isolated Spanner database."""
    if _spanner_db is not None:
        try:
            with _spanner_db.snapshot() as snapshot:
                results = list(snapshot.execute_sql("SELECT CURRENT_TIMESTAMP()", timeout=4.0))
                ts = str(results[0][0]) if results else "ok"
                return f"spanner:{SPANNER_INSTANCE}:{ts}"
        except Exception as exc:
            print(f"[{CELL_ID}] Spanner read warning for tenant={tenant_id}: {exc}")
            return f"spanner-degraded:{SPANNER_INSTANCE}:{type(exc).__name__}"
    return f"local-spanner-sim:{CELL_ID}"


def _render_browser_dashboard(
    tenant_id: str,
    virtual_shard: str,
    routed_cell_header: str,
    storage_token: str,
) -> bytes:
    esc = html.escape
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Google Cloud Cell Router — {esc(CELL_ID)}</title>
  <style>
    :root {{
      --bg: #f8fafc; --card: #ffffff; --border: #dadce0;
      --text: #202124; --muted: #5f6368; --blue: #1a73e8;
      --green-bg: #e6f4ea; --green-fg: #137333;
      --blue-bg: #e8f0fe; --blue-fg: #1967d2;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; padding: 32px 20px; background: var(--bg); color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      line-height: 1.5;
    }}
    .wrap {{ max-width: 980px; margin: 0 auto; }}
    .card {{
      background: var(--card); border: 1px solid var(--border);
      border-radius: 12px; padding: 24px; margin-bottom: 20px;
      box-shadow: 0 1px 2px rgba(60,64,67,0.06);
    }}
    .badge {{
      display: inline-block; padding: 4px 10px; border-radius: 999px;
      font-size: 12px; font-weight: 600;
    }}
    .badge-ok {{ background: var(--green-bg); color: var(--green-fg); }}
    .badge-cell {{ background: var(--blue-bg); color: var(--blue-fg); font-family: monospace; }}
    h1 {{ margin: 8px 0 6px; font-size: 22px; }}
    h2 {{ margin: 0 0 12px; font-size: 16px; }}
    p.sub {{ margin: 0; color: var(--muted); font-size: 14px; }}
    .grid {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 12px; margin-top: 18px;
    }}
    .kv {{
      border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px;
      background: #fbfcfe;
    }}
    .kv .k {{ font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }}
    .kv .v {{ font-size: 14px; font-weight: 600; font-family: monospace; margin-top: 4px; word-break: break-all; }}
    .controls {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 14px; }}
    input[type="text"] {{
      padding: 8px 12px; border: 1px solid var(--border); border-radius: 6px;
      font-size: 14px; min-width: 220px;
    }}
    button {{
      background: var(--blue); color: #fff; border: none; border-radius: 6px;
      padding: 9px 16px; font-size: 14px; font-weight: 600; cursor: pointer;
    }}
    button.secondary {{ background: #fff; color: var(--blue); border: 1px solid var(--border); }}
    button:hover {{ opacity: 0.93; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ text-align: left; padding: 10px 8px; border-bottom: 1px solid #eef0f2; }}
    th {{ color: var(--muted); font-weight: 600; font-size: 12px; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <span class="badge badge-ok">● LIVE ANYCAST CELL ROUTER</span>
      <h1>Cell-Based Architecture &amp; Shuffle Sharding POC</h1>
      <p class="sub">
        Request routed at the Google Cloud Load Balancing edge via Proxy-Wasm Service Extensions to an isolated Cloud Run + Spanner cell.
      </p>
      <div class="grid">
        <div class="kv"><div class="k">Serving Cell</div><div class="v">{esc(CELL_ID)}</div></div>
        <div class="kv"><div class="k">Cell Region</div><div class="v">{esc(REGION)}</div></div>
        <div class="kv"><div class="k">GCP Project</div><div class="v">{esc(PROJECT_ID)}</div></div>
        <div class="kv"><div class="k">Virtual Shard (Proxy-Wasm)</div><div class="v">{esc(virtual_shard)}</div></div>
        <div class="kv"><div class="k">Edge Header (x-cell-id)</div><div class="v">{esc(routed_cell_header)}</div></div>
        <div class="kv"><div class="k">Cell Spanner Read</div><div class="v">{esc(storage_token)}</div></div>
      </div>
    </div>

    <div class="card">
      <h2>Live Multi-Tenant Shuffle-Sharding Probe</h2>
      <p class="sub" style="margin-bottom:14px;">
        Send live HTTP requests with different <code>X-Tenant-Id</code> headers through this Anycast VIP to observe deterministic cell &amp; Spanner isolation.
      </p>
      <div class="controls">
        <button onclick="probeSampleTenants()">Probe 6 Sample Tenants</button>
        <input id="customTenant" type="text" placeholder="Enter custom X-Tenant-Id (e.g. acme-ai)" value="acme-financial" />
        <button class="secondary" onclick="probeSingleTenant()">Route Custom Tenant</button>
      </div>
      <table>
        <thead>
          <tr>
            <th>Tenant ID</th>
            <th>HTTP</th>
            <th>Serving Cell</th>
            <th>Region</th>
            <th>Virtual Shard Hand (Proxy-Wasm)</th>
            <th>Isolated Spanner Ledger</th>
          </tr>
        </thead>
        <tbody id="resultsBody">
          <tr><td colspan="6" style="color:#5f6368;">Probing live cells...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <script>
    const SAMPLE_TENANTS = [
      "alice-corp", "bob-retail", "charlie-bank",
      "delta-health", "echo-logistics", "foxtrot-ai"
    ];

    async function callTenant(tid) {{
      const res = await fetch("/v1/infer", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json", "X-Tenant-Id": tid }},
        body: "{{}}"
      }});
      const data = await res.json();
      return {{ status: res.status, data }};
    }}

    function renderRow(tid, status, d) {{
      const tr = document.createElement("tr");
      const cells = [
        tid,
        status === 200 ? "200 OK" : String(status),
        d.cell_id || "-",
        d.region || "-",
        d.virtual_shard || "-",
        d.storage || "-"
      ];
      cells.forEach((val, idx) => {{
        const td = document.createElement("td");
        if (idx === 2) {{
          const span = document.createElement("span");
          span.className = "badge badge-cell";
          span.textContent = val;
          td.appendChild(span);
        }} else {{
          td.style.fontFamily = idx >= 4 || idx === 0 ? "monospace" : "inherit";
          td.textContent = val;
        }}
        tr.appendChild(td);
      }});
      return tr;
    }}

    async function probeSampleTenants() {{
      const tbody = document.getElementById("resultsBody");
      tbody.innerHTML = "";
      for (const tid of SAMPLE_TENANTS) {{
        try {{
          const {{ status, data }} = await callTenant(tid);
          tbody.appendChild(renderRow(tid, status, data));
        }} catch (e) {{
          tbody.appendChild(renderRow(tid, "ERR", {{ storage: String(e) }}));
        }}
      }}
    }}

    async function probeSingleTenant() {{
      const input = document.getElementById("customTenant");
      const tid = (input.value || "anonymous").trim();
      const tbody = document.getElementById("resultsBody");
      const {{ status, data }} = await callTenant(tid);
      tbody.prepend(renderRow(tid, status, data));
    }}

    probeSampleTenants();
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
        self.send_header("X-Served-By-Cell", CELL_ID)
        self.send_header("X-App-Version", APP_VERSION)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Served-By-Cell", CELL_ID)
        self.send_header("X-App-Version", APP_VERSION)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            with _state_lock:
                healthy = _cell_state["healthy"]
                poisoned_by = _cell_state["poisoned_by_tenant"]
            if not healthy:
                self._send_json(
                    503,
                    {
                        "status": "UNHEALTHY",
                        "cell_id": CELL_ID,
                        "reason": f"Cell crashed by poison-pill payload from {poisoned_by}",
                    },
                )
                return
            self._send_json(
                200,
                {
                    "status": "SERVING",
                    "cell_id": CELL_ID,
                    "project_id": PROJECT_ID,
                    "region": REGION,
                    "version": APP_VERSION,
                },
            )
            return

        if self.path == "/metrics":
            with _state_lock:
                snapshot = dict(_cell_state)
            self._send_json(200, {"cell_id": CELL_ID, **snapshot})
            return

        # Render interactive HTML dashboard when opened directly in a web browser
        accept = self.headers.get("Accept", "")
        if self.path in ("/", "/index.html") and "text/html" in accept:
            tenant_id = self.headers.get("X-Tenant-Id", "browser-visitor")
            virtual_shard = self.headers.get("X-Virtual-Shard", "unassigned")
            routed_cell = self.headers.get("X-Cell-Id", CELL_ID)
            storage_token = _record_tenant_op(tenant_id)
            self._send_html(
                200,
                _render_browser_dashboard(tenant_id, virtual_shard, routed_cell, storage_token),
            )
            return

        self._handle_tenant_request()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 0:
            _ = self.rfile.read(length)

        if self.path == "/chaos/reset":
            with _state_lock:
                _cell_state["healthy"] = True
                _cell_state["poisoned_by_tenant"] = None
                _cell_state["poisoned_at"] = None
            self._send_json(200, {"status": "RESET", "cell_id": CELL_ID})
            return

        self._handle_tenant_request()

    def _handle_tenant_request(self) -> None:
        tenant_id = self.headers.get("X-Tenant-Id", "anonymous")
        poison_header = self.headers.get("X-Poison-Payload", "").lower()

        with _state_lock:
            if not _cell_state["healthy"]:
                poisoned_by = _cell_state["poisoned_by_tenant"]
                self._send_json(
                    503,
                    {
                        "error": "CELL_UNAVAILABLE",
                        "cell_id": CELL_ID,
                        "poisoned_by": poisoned_by,
                    },
                )
                return

            if poison_header == "crash":
                _cell_state["healthy"] = False
                _cell_state["poisoned_by_tenant"] = tenant_id
                _cell_state["poisoned_at"] = time.time()
                self._send_json(
                    503,
                    {
                        "error": "POISON_PILL_TRIGGERED",
                        "cell_id": CELL_ID,
                        "tenant_id": tenant_id,
                    },
                )
                return

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
                "project_id": PROJECT_ID,
                "region": REGION,
                "version": APP_VERSION,
                "host": socket.gethostname(),
                "tenant_id": tenant_id,
                "virtual_shard": self.headers.get("X-Virtual-Shard", "unassigned"),
                "storage": storage_token,
            },
        )


class CellHTTPServer(ThreadingHTTPServer):
    request_queue_size = 256
    allow_reuse_address = True


def serve(host: str, port: int) -> None:
    server = CellHTTPServer((host, port), CellRequestHandler)
    print(f"[{CELL_ID}] Listening on http://{host}:{port} (project={PROJECT_ID}, version={APP_VERSION})")
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run isolated SaaS cell backend")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = parser.parse_args()
    serve(args.host, args.port)
