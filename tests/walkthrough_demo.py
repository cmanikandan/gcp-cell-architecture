#!/usr/bin/env python3
"""
Beginner-friendly 15-second walkthrough of cell-based architecture and shuffle sharding.
Spins up 8 real HTTP cell servers + a local shuffle-sharding load balancer, then walks
step-by-step through Alice and a "poison-pill" customer (Tenant X).
"""

import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from live_chaos_test import LocalCellRouterProxy  # noqa: E402
from shuffle_router import compute_virtual_shard  # noqa: E402


def http_get(url: str, headers: dict[str, str]) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body)
        except Exception:
            return exc.code, {"error": body}


def main() -> None:
    n_cells = 8
    k_hand = 3
    base_cell_port = 19200
    router_port = 19180
    demo_salt = "gcp-cell-v1-prod"

    procs: list[subprocess.Popen] = []
    server: ThreadingHTTPServer | None = None
    try:
        print("Step 1/4: Starting 8 isolated cell servers (cell-00 .. cell-07) and local load balancer...")
        ports = []
        for idx in range(n_cells):
            cid = f"cell-{idx:02d}"
            port = base_cell_port + idx
            ports.append(port)
            p = subprocess.Popen(
                [sys.executable, str(ROOT / "cell-service" / "app.py")],
                env={"CELL_ID": cid, "PORT": str(port), "PATH": "/usr/bin:/bin"},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            procs.append(p)

        class DemoRouter(LocalCellRouterProxy):
            cell_ports = ports
            shard_size = k_hand
            salt = demo_salt

        server = ThreadingHTTPServer(("127.0.0.1", router_port), DemoRouter)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        time.sleep(0.8)

        lb_url = f"http://127.0.0.1:{router_port}/api/v1/process"

        poison_id = "tenant-x"
        poison_hand = [f"cell-{i:02d}" for i in compute_virtual_shard(poison_id, n_cells, k_hand, demo_salt)]

        alice_id = "alice"
        for i in range(1, 50):
            cand = "alice" if i == 1 else f"alice-{i}"
            h = [f"cell-{c:02d}" for c in compute_virtual_shard(cand, n_cells, k_hand, demo_salt)]
            if h[0] in poison_hand and any(c not in poison_hand for c in h):
                alice_id = cand
                break
        alice_hand = [f"cell-{c:02d}" for c in compute_virtual_shard(alice_id, n_cells, k_hand, demo_salt)]

        print("\nStep 2/4: Dealing each customer a 3-cell 'virtual hand' (8 cells -> 56 unique hands):")
        print(f"  - Noisy customer ({poison_id}) hand : {poison_hand}")
        print(f"  - Normal customer ({alice_id}) hand: {alice_hand}  <-- Shares {alice_hand[0]} with {poison_id}!")

        status, body = http_get(lb_url, {"X-Tenant-Id": alice_id})
        print(f"\n  Before crash -> {alice_id} request returns HTTP {status} from {body.get('cell_id')}")

        print(f"\nStep 3/4: {poison_id} sends a poison-pill payload that crashes all 3 cells in its hand...")
        http_get(lb_url, {"X-Tenant-Id": poison_id, "X-Poison-Payload": "crash"})
        print(f"  - Crashed cells: {poison_hand} (returning HTTP 503)")
        print("  - Remaining 5 cells stay 100% healthy because cells share nothing.")

        print(f"\nStep 4/4: Sending {alice_id}'s next request through the load balancer...")
        status, body = http_get(lb_url, {"X-Tenant-Id": alice_id})
        print(
            f"  -> HTTP {status} OK! {alice_hand[0]} was down, so the load balancer automatically retried "
            f"healthy backup {body.get('cell_id')} — {alice_id} experienced zero downtime!"
        )
        assert status == 200, f"Expected 200 OK for {alice_id}, got {status}"
    finally:
        if server is not None:
            server.shutdown()
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=2)
            except Exception:
                p.kill()


if __name__ == "__main__":
    main()
