#!/usr/bin/env python3
"""Cell-isolated multi-tenant workload service for Cloud Run and GKE.

Supports:
- Cell-scoped Spanner reads/writes when SPANNER_INSTANCE and SPANNER_DATABASE are set
  (falls back cleanly to an in-memory cell store for local blast-radius chaos testing).
- Deterministic poison-pill fault simulation (`X-Poison-Payload: crash`) to model an
  unhandled OOM / deadlock / thread-pool exhaustion bug triggered by a single tenant.
- Health readiness endpoint (`/healthz`) polled by Cloud Load Balancing & GKE/Cloud Run.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

CELL_ID = os.environ.get("CELL_ID", "cell-00")
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", f"saas-prod-{CELL_ID}")
REGION = os.environ.get("CELL_REGION", "us-central1")
SPANNER_INSTANCE = os.environ.get("SPANNER_INSTANCE", "")
SPANNER_DATABASE = os.environ.get("SPANNER_DATABASE", "cell_ledger")
APP_VERSION = os.environ.get("APP_VERSION", "v1.4.0")

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
    """Writes or reads tenant transaction inside this cell's isolated Spanner instance."""
    if _spanner_db is not None:
        with _spanner_db.snapshot() as snapshot:
            results = list(snapshot.execute_sql("SELECT CURRENT_TIMESTAMP()"))
            return str(results[0][0]) if results else "spanner-ok"
    return f"local-spanner-sim:{CELL_ID}"


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
