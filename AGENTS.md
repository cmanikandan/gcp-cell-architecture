# AGENTS.md

Guidance for AI coding agents working in this repository.

## Project overview

This repository provides a runnable reference implementation and local chaos-testing suite for **cell-based architectures and shuffle sharding on Google Cloud**. It pairs a thin, stateless edge routing layer (`Cloud Load Balancing` + `Service Extensions` Proxy-Wasm) with isolated per-project workload cells (`Cloud Run`, `GKE`, `Spanner`, `Cloud WAN` `STAR` spokes), wave-gated rollouts (`Cloud Deploy`), and multi-project governance (`App Hub` and `Cloud Hub`).

## Directory layout

- `router-wasm/` — Rust `proxy-wasm` (`LbTrafficExtension`) plugin computing deterministic `k`-of-`N` virtual cell hands (`fnv1a_64` + `fmix64` + partial Fisher-Yates) with zero external I/O.
- `cell-service/` — Multi-tenant cell HTTP service (`app.py`, `Dockerfile`, and `k8s/cell-deployment.yaml`) supporting Spanner (`google-cloud-spanner` with local fallback), `/healthz` readiness probes, and deterministic poison-pill crash simulation (`X-Poison-Payload: crash`).
- `terraform/` — Modular Terraform configurations for global Anycast or sovereign regional external Application Load Balancers (`ingress_alb.tf`), Cloud WAN `STAR` spoke isolation (`cloud_wan_ncc.tf`), isolated compute and Spanner instances per cell (`cells_compute_spanner.tf`), progressive wave delivery (`cloud_deploy.tf`), and App Hub registration (`apphub.tf`).
- `clouddeploy/` — Skaffold v4 manifest (`skaffold.yaml`) and post-rollout SLO verification script (`verify-cell-slo.sh`).
- `tests/` — Step-by-step failover demo (`walkthrough_demo.py`), 1,000-customer live HTTP chaos benchmark (`live_chaos_test.py`), reference Python shuffle router (`shuffle_router.py`), and end-to-end runner (`deploy_and_test.sh`).
- `diagrams/` — Official Google Cloud Architecture Center (`GCP2`) style `.png` diagrams, official SVG icons (`icons/`), and generator script (`generate_diagrams.py`).
- `DISCUSS_GOOGLE_DEV_POST.md` — Companion technical blog post formatted for `https://discuss.google.dev/`.

## Architectural invariants (do not break)

1. **Zero shared state on the routing hot path:** Never add database, Redis, or remote RPC lookups inside `router-wasm/src/lib.rs` or `tests/shuffle_router.py`. Routing must remain purely deterministic and stateless.
2. **Hash parity between Rust and Python:** Keep `fnv1a_64` and `compute_virtual_shard` identical between `router-wasm/src/lib.rs` and `tests/shuffle_router.py` so local chaos tests match production Wasm routing.
3. **Hard bulkhead boundaries between cells:** Cells must never communicate east-west. Keep `preset_topology = "STAR"` on the Network Connectivity Center hub (`terraform/cloud_wan_ncc.tf`) with cell VPCs placed exclusively in the `edge` spoke group.
4. **Progressive wave rollouts:** Never deploy simultaneously to all cells. Keep `cell-00` as the `Wave 0` canary stage with `verify = true` and `repair_rollout_rule` enabled in `terraform/cloud_deploy.tf`.

## Verification commands

Always run the local chaos and verification suite after modifying routing, cell service, or deployment scripts:

```bash
# Run both the step-by-step failover demo and the 1,000-customer chaos benchmark
./tests/deploy_and_test.sh --local-only

# Run only the quick 4-step Alice vs. Tenant X failover walkthrough
python3 tests/walkthrough_demo.py

# Validate Terraform formatting and syntax (if terraform is installed)
terraform -chdir=terraform fmt -check
terraform -chdir=terraform validate
```

 Expected benchmark assertion: `N=8, k=3` and `N=12, k=2` shuffle sharding must contain poison-pill blast radius **below 2%** across 1,000 simulated tenants (`PASS: Poison-pill blast radius contained below 2% via shuffle sharding!`).
