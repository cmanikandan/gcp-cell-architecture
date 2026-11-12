#!/usr/bin/env bash
# End-to-end build, deploy, and blast-radius test script for Google Cloud.
# Usage:
#   ./tests/deploy_and_test.sh --local-only
#   PLATFORM_PROJECT_ID=my-platform-proj ./tests/deploy_and_test.sh --deploy-gcp
#   PLATFORM_PROJECT_ID=my-platform-proj ./tests/deploy_and_test.sh --destroy-gcp
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:---local-only}"

if [[ "${MODE}" == "--destroy-gcp" ]]; then
  shift || true
  exec "${ROOT_DIR}/tests/uninstall_gcp.sh" "$@"
fi

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
SECONDARY_REGION="${SECONDARY_REGION:-us-east4}"
AR_REPO="${AR_REPO:-cells}"
POC_CELL_COUNT="${POC_CELL_COUNT:-4}"

echo "==> [0/6] Enabling required Google Cloud APIs, Terraform CLI & Artifact Registry in ${PLATFORM_PROJECT_ID}..."
# shellcheck source=tests/ensure_terraform.sh
source "${ROOT_DIR}/tests/ensure_terraform.sh"
ensure_terraform

gcloud services enable \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  cloudresourcemanager.googleapis.com \
  compute.googleapis.com \
  run.googleapis.com \
  spanner.googleapis.com \
  networkconnectivity.googleapis.com \
  networkservices.googleapis.com \
  clouddeploy.googleapis.com \
  apphub.googleapis.com \
  monitoring.googleapis.com \
  logging.googleapis.com \
  iam.googleapis.com \
  --project="${PLATFORM_PROJECT_ID}"
gcloud services enable networkactions.googleapis.com --project="${PLATFORM_PROJECT_ID}" >/dev/null 2>&1 || true

if ! gcloud artifacts repositories describe "${AR_REPO}" \
  --project="${PLATFORM_PROJECT_ID}" \
  --location="${REGION}" >/dev/null 2>&1; then
  echo "    Creating Artifact Registry Docker repository '${AR_REPO}' in ${REGION}..."
  gcloud artifacts repositories create "${AR_REPO}" \
    --project="${PLATFORM_PROJECT_ID}" \
    --location="${REGION}" \
    --repository-format=docker \
    --description="Cell-based architecture POC images and Service Extensions Wasm plugins"
fi

# Ensure default Compute Engine / Cloud Build service account can push images & logs on new projects
PROJECT_NUMBER="$(gcloud projects describe "${PLATFORM_PROJECT_ID}" --format='value(projectNumber)' 2>/dev/null || true)"
if [[ -n "${PROJECT_NUMBER}" ]]; then
  for ROLE in roles/artifactregistry.writer roles/logging.logWriter roles/storage.objectAdmin; do
    gcloud projects add-iam-policy-binding "${PLATFORM_PROJECT_ID}" \
      --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
      --role="${ROLE}" \
      --condition=None \
      --quiet >/dev/null 2>&1 || true
  done
fi

# Configure single-project App Hub boundary (safe no-op if already configured or managed via folder)
gcloud apphub boundary update \
  --crm-node="projects/${PLATFORM_PROJECT_ID}" \
  --project="${PLATFORM_PROJECT_ID}" \
  --location=global \
  --quiet >/dev/null 2>&1 || true

echo "==> [1/6] Building & packaging Proxy-Wasm shuffle-sharding Service Extension in Cloud Build..."
WASM_URI="${REGION}-docker.pkg.dev/${PLATFORM_PROJECT_ID}/${AR_REPO}/shuffle-router:v0.1.0"
if [[ "${FORCE_REBUILD:-0}" != "1" ]] && gcloud artifacts docker images describe "${WASM_URI}" --quiet >/dev/null 2>&1; then
  echo "    Reusing existing Wasm image ${WASM_URI} (set FORCE_REBUILD=1 to rebuild)."
else
  gcloud builds submit "${ROOT_DIR}/router-wasm" \
    --project="${PLATFORM_PROJECT_ID}" \
    --config="${ROOT_DIR}/router-wasm/cloudbuild.yaml" \
    --substitutions="_WASM_URI=${WASM_URI}"
fi

echo "==> [2/6] Building cell workload container image..."
CELL_IMAGE_URI="${REGION}-docker.pkg.dev/${PLATFORM_PROJECT_ID}/${AR_REPO}/cell-api:v1.4.0"
if [[ "${FORCE_REBUILD:-0}" != "1" ]] && gcloud artifacts docker images describe "${CELL_IMAGE_URI}" --quiet >/dev/null 2>&1; then
  echo "    Reusing existing cell image ${CELL_IMAGE_URI} (set FORCE_REBUILD=1 to rebuild)."
else
  gcloud builds submit "${ROOT_DIR}/cell-service" \
    --project="${PLATFORM_PROJECT_ID}" \
    --tag="${CELL_IMAGE_URI}"
fi

echo "==> [3/6] Provisioning cellular topology via Terraform (ALB, Cloud WAN, Cloud Run, Spanner, Cloud Deploy, App Hub)..."
EXISTING_NON_CELL_VPCS="$(
  gcloud compute networks list --project="${PLATFORM_PROJECT_ID}" --format="value(name)" 2>/dev/null \
    | { grep -Ev '^(cell-[0-9]+-vpc|cellular-poc-vpc)$' || true; } \
    | wc -l | tr -d ' '
)"
DEDICATED_VPC_PER_CELL="true"
if (( 5 - EXISTING_NON_CELL_VPCS < POC_CELL_COUNT )); then
  echo "    Note: Project ${PLATFORM_PROJECT_ID} already has ${EXISTING_NON_CELL_VPCS} non-cell VPC(s)."
  echo "          Setting dedicated_vpc_per_cell=false so single-project POC stays within the default 5-VPC quota."
  DEDICATED_VPC_PER_CELL="false"
fi

cat > "${ROOT_DIR}/terraform/poc.auto.tfvars" <<EOF
platform_project_id    = "${PLATFORM_PROJECT_ID}"
primary_region         = "${REGION}"
secondary_region       = "${SECONDARY_REGION}"
poc_cell_count         = ${POC_CELL_COUNT}
dedicated_vpc_per_cell = ${DEDICATED_VPC_PER_CELL}
cell_container_image   = "${CELL_IMAGE_URI}"
wasm_plugin_image_uri  = "${WASM_URI}"
EOF

terraform -chdir="${ROOT_DIR}/terraform" init
terraform -chdir="${ROOT_DIR}/terraform" apply -auto-approve

echo "==> [4/6] Creating progressive wave release in Cloud Deploy (starts at cell-00 canary)..."
RELEASE_NAME="rel-$(date +%Y%m%d-%H%M%S)"
gcloud deploy releases create "${RELEASE_NAME}" \
  --project="${PLATFORM_PROJECT_ID}" \
  --region="${REGION}" \
  --delivery-pipeline="cellular-saas-wave-pipeline" \
  --source="${ROOT_DIR}/clouddeploy" \
  --images="cell-api=${CELL_IMAGE_URI}"

ALB_VIP="$(terraform -chdir="${ROOT_DIR}/terraform" output -raw anycast_cell_router_ip)"
echo "==> [5/6] Waiting for Global External ALB Anycast VIP (${ALB_VIP}) edge propagation..."
READY=0
for attempt in {1..24}; do
  HTTP_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -H 'X-Tenant-Id: warmup-probe' "http://${ALB_VIP}/healthz" || echo '000')"
  if [[ "${HTTP_CODE}" == "200" ]]; then
    echo "    Anycast VIP ${ALB_VIP} is live (HTTP 200 on attempt ${attempt}/24)!"
    READY=1
    break
  fi
  echo "    [attempt ${attempt}/24] GFE edge programming in progress (HTTP ${HTTP_CODE}); retrying in 10s..."
  sleep 10
done

echo "==> [6/6] Probing Anycast Cell Router VIP (${ALB_VIP}) across sample tenants..."
if [[ "${READY}" -eq 1 ]]; then
  python3 "${ROOT_DIR}/tests/live_chaos_test.py" --target-url "http://${ALB_VIP}"
else
  echo "    Note: Global ALB edge propagation can occasionally take 3-5 minutes on brand-new VIPs."
  echo "    Re-run the live tenant probe in ~60 seconds with:"
  echo "      python3 ./tests/live_chaos_test.py --target-url http://${ALB_VIP}"
fi

echo ""
echo "========================================================================"
echo " POC Deployment Complete!"
echo " Anycast Cell Router URL : http://${ALB_VIP}"
echo " When finished testing, stop all hourly billing with:"
echo "   PLATFORM_PROJECT_ID=${PLATFORM_PROJECT_ID} ./tests/uninstall_gcp.sh --yes"
echo "========================================================================"
