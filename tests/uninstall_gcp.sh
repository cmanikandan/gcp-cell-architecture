#!/usr/bin/env bash
# Uninstall / teardown script for the Google Cloud cell-based architecture POC.
# Destroys all Terraform-provisioned infrastructure (Spanner instances, Cloud WAN
# spokes, Cloud Load Balancing forwarding rules, Cloud Armor policies, Cloud Run
# services, Cloud Deploy targets, and App Hub registrations) and removes POC
# container images from Artifact Registry so ongoing cloud billing stops.
#
# Usage:
#   PLATFORM_PROJECT_ID=my-platform-proj ./tests/uninstall_gcp.sh
#   PLATFORM_PROJECT_ID=my-platform-proj ./tests/uninstall_gcp.sh --yes
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AUTO_APPROVE="${1:-}"

: "${PLATFORM_PROJECT_ID:?Set PLATFORM_PROJECT_ID before running uninstall_gcp.sh}"
REGION="${PRIMARY_REGION:-us-central1}"
AR_REPO="${AR_REPO:-cells}"

WASM_REPO_IMAGE="${REGION}-docker.pkg.dev/${PLATFORM_PROJECT_ID}/${AR_REPO}/shuffle-router"
CELL_REPO_IMAGE="${REGION}-docker.pkg.dev/${PLATFORM_PROJECT_ID}/${AR_REPO}/cell-api"
WASM_URI="${WASM_REPO_IMAGE}:v0.1.0"
CELL_IMAGE_URI="${CELL_REPO_IMAGE}:v1.4.0"

echo "========================================================================"
echo " Google Cloud Cell-Based Architecture POC — Teardown & Uninstall"
echo "========================================================================"
echo "  Platform project : ${PLATFORM_PROJECT_ID}"
echo "  Primary region   : ${REGION}"
echo "  Artifact Registry: ${REGION}-docker.pkg.dev/${PLATFORM_PROJECT_ID}/${AR_REPO}"
echo "========================================================================"

if [[ "${AUTO_APPROVE}" != "--yes" && "${AUTO_APPROVE}" != "-y" ]]; then
  read -r -p "Destroy all POC Google Cloud resources in ${PLATFORM_PROJECT_ID} and cell projects? Type 'yes' to continue: " CONFIRM
  if [[ "${CONFIRM}" != "yes" ]]; then
    echo "Aborted. No resources were deleted."
    exit 1
  fi
fi

echo "==> [1/3] Removing Cloud Deploy delivery pipeline & child releases (if present)..."
if command -v gcloud >/dev/null 2>&1; then
  gcloud deploy delivery-pipelines delete "cellular-saas-wave-pipeline" \
    --project="${PLATFORM_PROJECT_ID}" \
    --region="${REGION}" \
    --force \
    --quiet 2>/dev/null || echo "    (No active Cloud Deploy pipeline found or already removed)"
fi

echo "==> [2/3] Running Terraform destroy across all cells and shared routing resources..."
terraform -chdir="${ROOT_DIR}/terraform" init -input=false
terraform -chdir="${ROOT_DIR}/terraform" destroy -auto-approve \
  -var="platform_project_id=${PLATFORM_PROJECT_ID}" \
  -var="primary_region=${REGION}" \
  -var="cell_container_image=${CELL_IMAGE_URI}" \
  -var="wasm_plugin_image_uri=${WASM_URI}" \
  -var="enable_deletion_protection=false"

echo "==> [3/3] Cleaning up POC container & Wasm images from Artifact Registry..."
if command -v gcloud >/dev/null 2>&1; then
  gcloud artifacts docker images delete "${WASM_REPO_IMAGE}" \
    --delete-tags --quiet 2>/dev/null || echo "    (${WASM_REPO_IMAGE} already absent)"
  gcloud artifacts docker images delete "${CELL_REPO_IMAGE}" \
    --delete-tags --quiet 2>/dev/null || echo "    (${CELL_REPO_IMAGE} already absent)"
fi

echo ""
echo "Teardown complete! All POC Terraform resources and container images have been removed."
echo "Tip: Verify in Google Cloud Console -> Billing -> Reports that no hourly resources remain active."
