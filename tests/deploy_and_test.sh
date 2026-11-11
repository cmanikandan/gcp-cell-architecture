#!/usr/bin/env bash
# End-to-end build, deploy, and blast-radius test script for Google Cloud.
# Usage:
#   ./tests/deploy_and_test.sh --local-only
#   PLATFORM_PROJECT_ID=my-platform-proj ./tests/deploy_and_test.sh --deploy-gcp
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:---local-only}"

if [[ "${MODE}" == "--local-only" ]]; then
  echo "==> [Part 1] Step-by-step customer failover walkthrough (Alice vs. Tenant X)..."
  python3 "${ROOT_DIR}/tests/walkthrough_demo.py"
  echo ""
  echo "==> [Part 2] Running 1,000-customer live HTTP shuffle-sharding benchmark..."
  python3 "${ROOT_DIR}/tests/live_chaos_test.py" --tenants 1000
  echo "==> Verifying Cloud Deploy SLO verification gate script..."
  CELL_PORT=19990
  QUIET_LOGS=1 CELL_ID=cell-00 PORT="${CELL_PORT}" python3 "${ROOT_DIR}/cell-service/app.py" --host 127.0.0.1 --port "${CELL_PORT}" &
  CELL_PID=$!
  trap 'kill "${CELL_PID}" 2>/dev/null || true' EXIT
  sleep 0.5
  SKIP_GCP_MQL=1 CLOUD_DEPLOY_TARGET=cell-00 CELL_ROUTER_URL="http://127.0.0.1:${CELL_PORT}" \
    bash "${ROOT_DIR}/clouddeploy/verify-cell-slo.sh"
  exit 0
fi

: "${PLATFORM_PROJECT_ID:?Set PLATFORM_PROJECT_ID before running --deploy-gcp}"
REGION="${PRIMARY_REGION:-us-central1}"
AR_REPO="${AR_REPO:-cells}"

echo "==> [1/5] Building Proxy-Wasm shuffle-sharding Service Extension..."
(
  cd "${ROOT_DIR}/router-wasm"
  cargo build --target wasm32-wasip1 --release
  mkdir -p package
  cp target/wasm32-wasip1/release/cell_shuffle_router_wasm.wasm package/plugin.wasm
  cat > package/Dockerfile <<'EOF'
FROM scratch
COPY plugin.wasm plugin.wasm
EOF
)

WASM_URI="${REGION}-docker.pkg.dev/${PLATFORM_PROJECT_ID}/${AR_REPO}/shuffle-router:v0.1.0"
gcloud builds submit "${ROOT_DIR}/router-wasm/package" \
  --project="${PLATFORM_PROJECT_ID}" \
  --tag="${WASM_URI}"

echo "==> [2/5] Building cell workload container image..."
CELL_IMAGE_URI="${REGION}-docker.pkg.dev/${PLATFORM_PROJECT_ID}/${AR_REPO}/cell-api:v1.4.0"
gcloud builds submit "${ROOT_DIR}/cell-service" \
  --project="${PLATFORM_PROJECT_ID}" \
  --tag="${CELL_IMAGE_URI}"

echo "==> [3/5] Provisioning cellular topology via Terraform (ALB, Cloud WAN, Cloud Run, Spanner, Cloud Deploy, App Hub)..."
terraform -chdir="${ROOT_DIR}/terraform" init
terraform -chdir="${ROOT_DIR}/terraform" apply -auto-approve \
  -var="platform_project_id=${PLATFORM_PROJECT_ID}" \
  -var="cell_container_image=${CELL_IMAGE_URI}" \
  -var="wasm_plugin_image_uri=${WASM_URI}"

echo "==> [4/5] Creating progressive wave release in Cloud Deploy (starts at cell-00 canary)..."
gcloud deploy releases create "rel-$(date +%Y%m%d-%H%M)" \
  --project="${PLATFORM_PROJECT_ID}" \
  --region="${REGION}" \
  --delivery-pipeline="cellular-saas-wave-pipeline" \
  --source="${ROOT_DIR}/clouddeploy" \
  --images="cell-api=${CELL_IMAGE_URI}"

ALB_VIP="$(terraform -chdir="${ROOT_DIR}/terraform" output -raw anycast_cell_router_ip)"
echo "==> [5/5] Probing Anycast Cell Router VIP (${ALB_VIP})..."
python3 "${ROOT_DIR}/tests/live_chaos_test.py" --target-url "http://${ALB_VIP}"
