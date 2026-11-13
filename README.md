# Cell-Based Architecture & Shuffle Sharding on Google Cloud

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Deployable reference architecture, local chaos-testing suite, and diagrams for **isolating multi-tenant workloads on Google Cloud using cell-based architecture and shuffle sharding**.

> **Proof-of-concept (POC) & billing notice:** This repository is an educational **proof of concept (POC)**, not a turn-key production deployment. Running the local demo (`--local-only`) is **100% free**, whereas deploying the Terraform stack to Google Cloud (`--deploy-gcp`) provisions real cloud resources and **will incur Google Cloud billing charges**. Prices, free-tier quotas, and service features change over time—always refer to the [official Google Cloud documentation](https://docs.cloud.google.com/) and the [Google Cloud Pricing Calculator](https://cloud.google.com/products/calculator) for the latest information before deploying.

![Cell-based reference architecture on Google Cloud](diagrams/01_cell_reference_architecture.png)

## What you will find in this repo

Instead of running every customer inside one shared cluster and database—where a single noisy neighbor or bad config push can cause a 100% outage—this POC shows how to:

1. **Divide your stack into independent cells** (`1 Google Cloud project = 1 cell`) across **Cloud Run**, **Google Kubernetes Engine (GKE)**, **Spanner**, and **Cloud WAN** (`STAR` spoke isolation).
2. **Contain "poison-pill" customers below a 2% blast radius** using stateless **shuffle sharding** (`Service Extensions` Proxy-Wasm on **Cloud Load Balancing**).
3. **Roll out updates safely wave by wave** in **Cloud Deploy** with automated SLO verification and rollback, while viewing all cells from one dashboard in **App Hub** and **Cloud Hub**.

---

## Quickstart: run the 60-second local demo (\$0 cloud cost)

You can test cell isolation and shuffle-sharding failover right on your laptop using Python 3.10+ with **zero cloud cost**. The test runner spins up 8 isolated cell HTTP servers, starts a local shuffle-sharding load balancer, crashes **Tenant X**'s assigned cells, and verifies that **Alice** and **98%+ of 1,000 customers** stay online:

```bash
git clone https://github.com/cmanikandan/gcp-cell-architecture.git
cd gcp-cell-architecture

./tests/deploy_and_test.sh --local-only
```

Want to run just the 4-step beginner walkthrough (**Alice** vs. **Tenant X**)?

```bash
python3 tests/walkthrough_demo.py
```

![Shuffle sharding vs standard cell sharding](diagrams/02_shuffle_sharding_combinatorics.png)

---

## Deploy the POC to Google Cloud

### Approximate POC cost estimate

By default, `terraform/variables.tf` uses POC-friendly sizing (`100` Spanner processing units on `STANDARD` edition per cell, scale-to-zero Cloud Run with `min_instance_count = 0`, and Cloud Armor Standard). Approximate US list-price estimates (`us-central1` / `us-east4`) for all **8 cells**:

| Google Cloud service | POC configuration (8 cells) | Approx. hourly rate | Approx. 2-hour POC test | Approx. 24/7 monthly rate | Official pricing docs |
| :--- | :--- | :---: | :---: | :---: | :--- |
| **Spanner** | `8` regional `STANDARD` instances × `100` PU (1-hr minimum) | ~\$0.76/hr | ~\$1.52 | ~\$555/mo | [Spanner pricing](https://cloud.google.com/spanner/pricing) |
| **Cloud WAN (NCC)** | `8` VPC spokes on `STAR` hub (+ data transfer) | ~\$0.80/hr | ~\$1.60 | ~\$584/mo | [Network Connectivity pricing](https://cloud.google.com/network-connectivity/pricing) |
| **Cloud Load Balancing & Service Extensions** | `1` global forwarding rule + Proxy-Wasm plugin | ~\$0.025/hr | ~\$0.05 | ~\$18/mo | [Load Balancing pricing](https://cloud.google.com/load-balancing/pricing) |
| **Cloud Armor Standard** | `1` security policy + `2` rules (prorated) | ~\$0.01/hr | <\$0.05 | ~\$7/mo | [Cloud Armor pricing](https://cloud.google.com/armor/pricing) |
| **Cloud Run, Cloud Deploy, Cloud Build & App Hub** | Scale-to-zero (`min_instances = 0`), 1st pipeline, App Hub | ~\$0.00/hr idle | <\$0.25 | Usage-based | [Pricing Calculator](https://cloud.google.com/products/calculator) |
| **Estimated POC total** | **Full 8-cell POC stack** | **~\$1.60/hr** | **~\$3–\$5 total** | **~\$1,165/mo** | *Destroy after testing!* |

*Estimates exclude network egress/inter-region data transfer and free-tier credits, and may vary by region or currency. Always check the official Google Cloud pricing documentation linked above for current rates.*

### Prerequisites

* [Google Cloud SDK (`gcloud`)](https://docs.cloud.google.com/sdk/docs/install) authenticated to a sandbox organization/project with billing enabled
* [Terraform](https://developer.hashicorp.com/terraform/downloads) `>= 1.6` (automatically installed into `~/.local/bin/terraform` if Google Cloud Shell's stub is detected)
* *(Optional for local Wasm development only)* [Rust toolchain](https://rustup.rs/) with target `wasm32-wasip1` — **not required** for `--deploy-gcp`, because **Cloud Build** compiles and packages both the Rust Proxy-Wasm plugin (`router-wasm/cloudbuild.yaml`) and the Python cell service remotely

### One-command POC build & deploy (Cloud Shell or local workstation)

```bash
export PLATFORM_PROJECT_ID="your-platform-host-project"
export PRIMARY_REGION="us-central1"

./tests/deploy_and_test.sh --deploy-gcp
```

> **Single-project Cloud Shell mode vs. multi-project fleet:** When `cell_project_ids` is omitted, `--deploy-gcp` automatically enables required APIs, compiles the `wasm32-wasip1` Service Extension remotely in Cloud Build, and deploys **4 isolated cells** (`POC_CELL_COUNT=4`, ~`\$0.80/hr`) into `PLATFORM_PROJECT_ID` so 4 cell VPCs + `default` VPC fit cleanly inside Google Cloud's default 5-VPC per-project quota. To deploy the full **8-cell** multi-project topology (~`\$1.60/hr`), pass `cell_project_ids` in Terraform or set `POC_CELL_COUNT=8`.

### Interactive browser topology & chaos studio

Once `--deploy-gcp` prints `Anycast Cell Router URL : http://<ANYCAST_VIP>`, open that URL in any browser to launch the **5-box guided Cell Architecture & Shuffle-Sharding Live Explorer**:

* **Embedded Architecture Blueprint tabs (Box 1):** Browse all 4 Google Cloud Architecture Center diagrams (end-to-end cell topology, shuffle-sharding math, global vs. regional ingress, and Cloud Deploy wave rollouts) directly inside the app with plain-English explanations and step-by-step "What to do here" guides in every box.
* **Live animated SVG routing topology (Box 2):** Watch real HTTP requests traverse the Global External ALB + Proxy-Wasm `LbEdgeExtension` into isolated Cloud Run + Spanner cells, automatically re-probing whenever a cell crashes or heals so arrows always match live reality.
* **Browser-triggered chaos controls & 24-customer blast matrix (Boxes 3–5):** Crash or heal individual cells (Spanner-backed with 180s auto-heal), run the **4-step Guided Failover Demo**, click any of 24 live customer tiles to trace its hand, and inspect raw HTTP retries and Spanner timestamps in the live request log.
* **Server-side project ID obfuscation:** All HTML and JSON endpoints mask `GOOGLE_CLOUD_PROJECT` server-side to only its first 3 characters (`abc************`) so full project IDs are never exposed to external clients.

![Live Anycast Application Load Balancer and Rust Proxy-Wasm routing view](diagrams/06_live_anycast_wasm_routing_diagram.png)

![Per-cell chaos controls and 24-customer blast matrix](diagrams/07_live_chaos_controls_and_blast_matrix.png)

![Cross-region Spanner failover audit log](diagrams/08_live_cross_region_spanner_failover_log.png)

To switch between a **global Anycast external Application Load Balancer** (default) and **sovereign regional external Application Load Balancers** (for strict data residency), set `ingress_mode = "regional"` in `terraform/variables.tf` or pass `-var="ingress_mode=regional"`.

### Uninstall & tear down Google Cloud resources

To avoid ongoing hourly Spanner and Cloud WAN spoke charges (~`\$1.60/hr`), tear down the POC as soon as you finish testing. The uninstall script removes active Cloud Deploy releases, runs `terraform destroy` across all cells, and deletes POC container images from Artifact Registry:

```bash
export PLATFORM_PROJECT_ID="your-platform-host-project"
export PRIMARY_REGION="us-central1"

./tests/uninstall_gcp.sh
# Or non-interactively: ./tests/uninstall_gcp.sh --yes
```

![Global vs regional external Application Load Balancers](diagrams/03_global_vs_regional_alb_ingress.png)

---

## Repository structure

```text
.
├── router-wasm/              # Rust Proxy-Wasm LbEdgeExtension + Cloud Build Wasm pipeline
├── cell-service/             # Cell HTTP service, Spanner chaos state & live SVG Chaos Studio UI
├── terraform/                # ALB, Cloud WAN (NCC STAR), Cloud Run/GKE, Spanner, Cloud Deploy, App Hub
├── clouddeploy/              # Skaffold v4 profiles, Cloud Run template & SLO verification gate
├── tests/                    # Local failover demo, 1,000-tenant chaos test, deploy & uninstall scripts
├── diagrams/                 # Google Cloud Architecture Center style PNG diagrams & generator
├── DISCUSS_GOOGLE_DEV_POST.md# Companion technical blog article for discuss.google.dev
├── AGENTS.md                 # Instructions, security rules & architectural invariants for AI agents
└── LICENSE                   # MIT License
```

![Progressive wave-by-wave rollout in Cloud Deploy](diagrams/04_cloud_deploy_wave_pipeline.png)

## License

Released under the [MIT License](LICENSE).
