# AGENTS.md

Guidance for AI coding agents working in this repository.

## Project overview

This repository provides a runnable **proof-of-concept (POC)** reference implementation, interactive browser chaos studio, and local chaos-testing suite for **cell-based architectures and shuffle sharding on Google Cloud**. It pairs a thin, stateless edge routing layer (`Cloud Load Balancing` + `Service Extensions` Proxy-Wasm `LbEdgeExtension`) with isolated workload cells (`Cloud Run`, `GKE`, `Spanner`, `Cloud WAN` `STAR` spokes), wave-gated rollouts (`Cloud Deploy`), and multi-project governance (`App Hub` and `Cloud Hub`).

## Directory layout

- `router-wasm/` — Rust `proxy-wasm` (`LbEdgeExtension`) plugin (`src/lib.rs`) + remote Cloud Build pipeline (`cloudbuild.yaml`) computing deterministic `k`-of-`N` virtual cell hands (`fnv1a_64` + `fmix64` + partial Fisher-Yates) with zero external I/O, stamping `x-cell-id`, `x-virtual-shard`, `x-ordered-hand`, and `x-fleet-config` (plus honoring operational `x-target-cell` / `x-cell-override` headers).
- `cell-service/` — Multi-tenant cell HTTP service (`app.py`, `Dockerfile`, and `k8s/cell-deployment.yaml`) featuring:
  - Server-side GCP Project ID obfuscation (`_mask_project_id` exposes only the first 3 characters + `************`),
  - Spanner-backed per-cell chaos state (`__cell_health__` row in `Tenants` with a 180s auto-heal TTL so poison-pill crashes apply across autoscaled Cloud Run instances without crossing cell Spanner boundaries),
  - Interactive SVG topology visualizer & browser-triggered chaos studio served at `GET /` (`Accept: text/html`).
- `terraform/` — Modular Terraform configurations for global Anycast or sovereign regional external Application Load Balancers (`ingress_alb.tf`), Cloud WAN `STAR` spoke isolation (`cloud_wan_ncc.tf`), isolated compute and Spanner instances per cell (`cells_compute_spanner.tf`), progressive wave delivery (`cloud_deploy.tf`), and App Hub registration (`apphub.tf`). Supports both multi-project (`cell_project_ids`) and single-project Cloud Shell (`poc_cell_count = 4`) modes, and uses `invoker_iam_disabled` on Cloud Run for Domain Restricted Sharing org-policy compatibility.
- `clouddeploy/` — Skaffold v4 manifest (`skaffold.yaml`), parameterized Cloud Run service manifest (`cloudrun-service.yaml`), and post-rollout SLO verification script (`verify-cell-slo.sh`).
- `tests/` — Step-by-step failover demo (`walkthrough_demo.py`), 1,000-customer live HTTP chaos benchmark (`live_chaos_test.py`), reference Python shuffle router (`shuffle_router.py`), Cloud Shell Terraform bootstrap helper (`ensure_terraform.sh`), end-to-end runner (`deploy_and_test.sh`), and cloud teardown script (`uninstall_gcp.sh`).
- `diagrams/` — Official Google Cloud Architecture Center (`GCP2`) style `.png` diagrams, official SVG icons (`icons/`), and generator script (`generate_diagrams.py`).
- `DISCUSS_GOOGLE_DEV_POST.md` — Companion technical blog post formatted for `https://discuss.google.dev/`.

## Architectural & security invariants (do not break)

1. **Zero shared state on the routing hot path:** Never add database, Redis, or remote RPC lookups inside `router-wasm/src/lib.rs` or `tests/shuffle_router.py`. Routing must remain purely deterministic and stateless.
2. **Bit-for-bit hash parity across Rust, Python, and Browser JS:** Keep 64-bit `fnv1a_64` and `compute_virtual_shard` (`(h % (remaining as u64)) as usize`) identical across `router-wasm/src/lib.rs`, `tests/shuffle_router.py`, and the `BigInt` implementation in `cell-service/app.py`.
3. **Never leak full GCP Project IDs in HTTP responses:** Keep `_RAW_PROJECT_ID` strictly private in `cell-service/app.py` (used only for `spanner.Client`), and always return `MASKED_PROJECT_ID` (`first 3 chars + ************`) across HTML and JSON endpoints. Never commit real user GCP project IDs to the repository.
4. **Hard bulkhead boundaries between cells:** Cells must never communicate east-west. Keep `preset_topology = "STAR"` on the Network Connectivity Center hub (`terraform/cloud_wan_ncc.tf`) with cell VPCs placed exclusively in the `edge` spoke group, and keep chaos state scoped to each cell's own Spanner database.
5. **Keep `/healthz` HTTP 200 for Cloud Run `startupProbe`:** During a poison-pill chaos test, tenant data-plane routes (`/v1/infer`, `/api/...`) return HTTP `503`, whereas `/healthz`, `/chaos/status`, `/chaos/reset`, and browser `GET /` (`Accept: text/html`) return HTTP `200` so autoscaled Cloud Run instances can start up and accept heal commands.
6. **Atomic Wasm version updates & progressive wave rollouts:** Keep `main_version_id` content-hashed in `terraform/ingress_alb.tf` so Wasm image updates promote cleanly without digest caching, and keep `cell-00` as the `Wave 0` canary stage in `terraform/cloud_deploy.tf`.
7. **Clean POC teardown:** Any new Google Cloud resource added to `terraform/` or `tests/deploy_and_test.sh` must be cleanly removable by `tests/uninstall_gcp.sh` without manual deletion blockers.

## Verification commands

Always run the local chaos, security masking, and verification suite after modifying routing, cell service, or deployment scripts:

```bash
# Run walkthrough demo, 1,000-customer chaos benchmark, SLO gate, and project-ID masking check ($0 local mode)
./tests/deploy_and_test.sh --local-only

# Run only the quick 4-step Alice vs. Tenant X failover walkthrough
python3 tests/walkthrough_demo.py

# Syntax-check shell scripts
bash -n tests/deploy_and_test.sh tests/uninstall_gcp.sh tests/ensure_terraform.sh clouddeploy/verify-cell-slo.sh

# Validate Terraform formatting and syntax (if terraform is installed)
terraform -chdir=terraform fmt -check
terraform -chdir=terraform validate
```

Expected benchmark assertion: `N=8, k=3` and `N=12, k=2` shuffle sharding must contain poison-pill blast radius **below 2%** across 1,000 simulated tenants (`PASS: Poison-pill blast radius contained below 2% via shuffle sharding!`), and the project ID masking check must pass (`[security] PASS: GCP Project ID obfuscated`).
