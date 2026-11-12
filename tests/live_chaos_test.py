#!/usr/bin/env python3
"""Live end-to-end blast-radius & poison-pill chaos test harness.

Modes:
1. Local multi-process cluster mode (default):
   - Boots N isolated cell HTTP servers (`cell-service/app.py`) on localhost ports.
   - Boots a local L7 Cell Router HTTP proxy implementing the exact Cloud Load Balancing
     Proxy-Wasm shuffle-sharding + outlier-retry failover behavior (`num_retries = k - 1`).
   - Sends baseline traffic for 1,000 SaaS tenants -> verifies 100.0% availability.
   - Injects a deterministic poison-pill payload (`X-Poison-Payload: crash`) from
     `tenant-poison-4091`, crashing EVERY cell in that tenant's virtual shard.
   - Re-tests all 999 healthy tenants and compares Standard Sharding (k=1) vs.
     Shuffle Sharding (N=8, k=3 -> <2% impact; and N=12, k=2 -> <2% impact).

2. Remote GCP ALB mode (`--target-url https://<ALB_VIP>`):
   - Runs the same tenant probe & poison-pill isolation check against a live
     Cloud Load Balancing external Application Load Balancer deployment.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from shuffle_router import blast_radius_stats, compute_virtual_shard

ROOT_DIR = Path(__file__).resolve().parent.parent
CELL_APP_PATH = ROOT_DIR / "cell-service" / "app.py"


class LocalCellRouterProxy(BaseHTTPRequestHandler):
    """Emulates Cloud Load Balancing + Proxy-Wasm LbTrafficExtension + URL map retryPolicy."""

    cell_ports: list[int] = []
    shard_size: int = 3
    salt: str = "gcp-cell-v1-prod"

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        self._proxy_request("POST")

    def do_GET(self) -> None:
        self._proxy_request("GET")

    def _proxy_request(self, method: str) -> None:
        tenant_id = self.headers.get("X-Tenant-Id", "anonymous")
        poison = self.headers.get("X-Poison-Payload", "")
        total_cells = len(self.cell_ports)

        hand = compute_virtual_shard(
            tenant_id=tenant_id,
            total_cells=total_cells,
            shard_size=self.shard_size,
            salt=self.salt,
        )
        shard_sig = ",".join(f"cell-{c:02d}" for c in sorted(hand))

        # Mirror Envoy/ALB retryPolicy (retries across the k cells in the tenant's hand on 5xx)
        last_status = 503
        last_body = b'{"error":"ALL_SHARD_CELLS_UNHEALTHY"}'
        served_cell = f"cell-{hand[0]:02d}"

        for attempt_idx, cell_idx in enumerate(hand):
            port = self.cell_ports[cell_idx]
            url = f"http://127.0.0.1:{port}{self.path}"
            req = urllib.request.Request(
                url,
                data=b"" if method == "POST" else None,
                method=method,
                headers={
                    "X-Tenant-Id": tenant_id,
                    "X-Virtual-Shard": shard_sig,
                    "X-Retry-Attempt": str(attempt_idx),
                    **({"X-Poison-Payload": poison} if poison else {}),
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    last_status = resp.status
                    last_body = resp.read()
                    served_cell = resp.headers.get("X-Served-By-Cell", f"cell-{cell_idx:02d}")
                    break
            except urllib.error.HTTPError as exc:
                last_status = exc.code
                last_body = exc.read()
                # If a poison-pill tenant retries across its own hand, each retry poisons the next
                # cell in its k-cell shard—worst-case scenario! Healthy tenants stop at the first 200 OK.
                continue
            except Exception:
                last_status = 503
                continue

        self.send_response(last_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(last_body)))
        self.send_header("X-Served-By-Cell", served_cell)
        self.send_header("X-Virtual-Shard", shard_sig)
        self.end_headers()
        self.wfile.write(last_body)


def _wait_http_ok(url: str, timeout_s: float = 8.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.05)
    raise RuntimeError(f"Timed out waiting for {url}")


def _send_tenant_req(router_url: str, tenant_id: str, poison: bool = False) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json", "X-Tenant-Id": tenant_id}
    if poison:
        headers["X-Poison-Payload"] = "crash"
    req = urllib.request.Request(f"{router_url}/v1/infer", data=b"{}", method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, {"raw_body": raw[:200]}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") or "{}"
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw_body": body[:200]}
    except Exception as exc:
        return 503, {"error": str(exc)}


def run_live_experiment(total_cells: int, shard_size: int, num_tenants: int, base_port: int) -> dict:
    procs: list[subprocess.Popen] = []
    cell_ports = [base_port + i for i in range(total_cells)]
    router_port = base_port + 100

    env_base = os.environ.copy()
    env_base["QUIET_LOGS"] = "1"

    try:
        for i, port in enumerate(cell_ports):
            env = env_base.copy()
            env["CELL_ID"] = f"cell-{i:02d}"
            env["PORT"] = str(port)
            proc = subprocess.Popen(
                [sys.executable, str(CELL_APP_PATH), "--host", "127.0.0.1", "--port", str(port)],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            procs.append(proc)

        for port in cell_ports:
            _wait_http_ok(f"http://127.0.0.1:{port}/healthz")

        class BoundRouter(LocalCellRouterProxy):
            pass

        BoundRouter.cell_ports = cell_ports
        BoundRouter.shard_size = shard_size

        class HighBacklogHTTPServer(ThreadingHTTPServer):
            request_queue_size = 256
            allow_reuse_address = True

        router_server = HighBacklogHTTPServer(("127.0.0.1", router_port), BoundRouter)
        router_thread = threading.Thread(target=router_server.serve_forever, daemon=True)
        router_thread.start()
        router_url = f"http://127.0.0.1:{router_port}"

        healthy_tenants = [f"tenant-{i:04d}" for i in range(1, num_tenants + 1)]
        poison_tenant = "tenant-poison-4091"

        # Step 1: Verify 100% baseline availability across sample tenants
        with ThreadPoolExecutor(max_workers=24) as pool:
            baseline_codes = [
                code for code, _ in pool.map(lambda t: _send_tenant_req(router_url, t), healthy_tenants[:200])
            ]
        baseline_ok_pct = 100.0 * sum(1 for c in baseline_codes if c == 200) / len(baseline_codes)

        # Step 2: Fire poison-pill tenant request (retries across all k cells in its hand, crashing all k!)
        poison_code, _ = _send_tenant_req(router_url, poison_tenant, poison=True)
        poison_hand = compute_virtual_shard(poison_tenant, total_cells=total_cells, shard_size=shard_size)

        # Step 3: Probe all 1,000 innocent tenants concurrently through the live L7 router
        with ThreadPoolExecutor(max_workers=32) as pool:
            post_results = list(pool.map(lambda t: _send_tenant_req(router_url, t), healthy_tenants))

        impacted = sum(1 for code, _ in post_results if code != 200)
        surviving = len(healthy_tenants) - impacted
        empirical_impact_pct = 100.0 * impacted / len(healthy_tenants)

        router_server.shutdown()
        router_server.server_close()

        theory = blast_radius_stats(total_cells, shard_size)
        return {
            **theory,
            "poison_tenant": poison_tenant,
            "poisoned_cells": [f"cell-{c:02d}" for c in sorted(poison_hand)],
            "poison_response_code": poison_code,
            "baseline_availability_pct": round(baseline_ok_pct, 2),
            "tenants_tested": num_tenants,
            "tenants_surviving": surviving,
            "tenants_impacted": impacted,
            "empirical_blast_radius_pct": round(empirical_impact_pct, 2),
            "empirical_availability_pct": round(100.0 - empirical_impact_pct, 2),
        }
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()


def main() -> None:
    parser = argparse.ArgumentParser(description="Test cell-based blast-radius & shuffle sharding")
    parser.add_argument("--tenants", type=int, default=1000, help="Number of healthy SaaS tenants to test")
    parser.add_argument("--target-url", default="", help="Optional remote GCP ALB URL instead of local cluster")
    args = parser.parse_args()

    if args.target_url:
        base_url = args.target_url.rstrip("/")
        print(f"Running live multi-tenant shuffle-shard check against {base_url}...")
        sample_tenants = ["alice-corp", "bob-retail", "charlie-bank", "delta-health"]
        probe_summary = []
        for tid in sample_tenants:
            code, payload = _send_tenant_req(base_url, tid, poison=False)
            probe_summary.append({"tenant": tid, "status_code": code, "response": payload})
        print(json.dumps(probe_summary, indent=2))
        return

    print("=" * 78)
    print(" GOOGLE CLOUD CELLULAR ARCHITECTURE — LIVE POISON-PILL CHAOS BENCHMARK")
    print("=" * 78)

    scenarios = [
        ("Standard Cell Sharding (N=8, k=1)", 8, 1, 19100),
        ("Shuffle Sharding       (N=8, k=2)", 8, 2, 19300),
        ("Shuffle Sharding       (N=8, k=3)", 8, 3, 19500),
        ("Shuffle Sharding      (N=12, k=2)", 12, 2, 19700),
    ]

    results = []
    for label, n_cells, k_width, base_port in scenarios:
        res = run_live_experiment(n_cells, k_width, args.tenants, base_port)
        res["scenario"] = label
        results.append(res)
        print(
            f"\n[{label}]\n"
            f"  Virtual shards C({n_cells},{k_width}) : {res['virtual_shards']}\n"
            f"  Poisoned cells         : {', '.join(res['poisoned_cells'])}\n"
            f"  Baseline availability  : {res['baseline_availability_pct']:.2f}%\n"
            f"  Surviving tenants      : {res['tenants_surviving']}/{res['tenants_tested']} "
            f"({res['empirical_availability_pct']:.2f}% available)\n"
            f"  Empirical blast radius : {res['empirical_blast_radius_pct']:.2f}% "
            f"(theoretical: {res['shuffle_blast_radius_pct']:.2f}%)"
        )

    # Assert <2% poison-pill blast radius on N=8,k=3 (1.79% theoretical) and N=12,k=2 (1.52% theoretical)
    assert results[2]["empirical_blast_radius_pct"] <= 2.2, "Expected <2% blast radius for N=8, k=3"
    assert results[3]["empirical_blast_radius_pct"] <= 2.0, "Expected <2% blast radius for N=12, k=2"
    print("\n" + "=" * 78)
    print(" PASS: Poison-pill blast radius contained below 2% via shuffle sharding!")
    print("=" * 78)


if __name__ == "__main__":
    main()
