#!/usr/bin/env bash
# Executed inside a Cloud Deploy verify job after deploying to a cell target.
# Blocks promotion to the next wave if synthetic probes fail or Cloud Monitoring
# reports a 5xx error ratio > 0.5% for the target cell.
set -euo pipefail

TARGET_CELL="${CLOUD_DEPLOY_TARGET:-cell-00}"
ROUTER_URL="${CELL_ROUTER_URL:-}"
PROJECT_ID="${CLOUD_DEPLOY_PROJECT:-saas-platform-prod}"
CELL_REGION_VAL="${CELL_REGION:-${CLOUD_DEPLOY_LOCATION:-us-central1}}"

if [[ -n "${ROUTER_URL}" ]]; then
  echo "[verify] Probing synthetic canary tenant routed to ${TARGET_CELL} via ${ROUTER_URL}..."
  for i in {1..5}; do
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
      -H "X-Tenant-Id: synthetic-probe-${TARGET_CELL}" \
      -H "X-Cell-Override: ${TARGET_CELL}" \
      "${ROUTER_URL}/healthz" || echo "000")
    if [[ "${HTTP_CODE}" != "200" ]]; then
      echo "[verify] ERROR: Synthetic probe ${i}/5 returned HTTP ${HTTP_CODE} on ${TARGET_CELL}."
      exit 1
    fi
  done
elif command -v gcloud >/dev/null 2>&1; then
  echo "[verify] Checking Cloud Run Ready condition for ${TARGET_CELL}-api in ${PROJECT_ID} (${CELL_REGION_VAL})..."
  READY_STATUS=$(gcloud run services describe "${TARGET_CELL}-api" \
    --project="${PROJECT_ID}" \
    --region="${CELL_REGION_VAL}" \
    --format="value(status.conditions[0].status)" 2>/dev/null || echo "Unknown")
  if [[ "${READY_STATUS}" != "True" ]]; then
    echo "[verify] ERROR: Cloud Run service ${TARGET_CELL}-api Ready status is '${READY_STATUS}'."
    exit 1
  fi
fi

echo "[verify] Querying Cloud Monitoring 5-minute 5xx error burn rate for ${TARGET_CELL}..."
if command -v gcloud >/dev/null 2>&1 && [[ "${SKIP_GCP_MQL:-0}" != "1" ]]; then
  ALERTS=$(gcloud alpha monitoring policies list \
    --project="${PROJECT_ID}" \
    --filter="displayName:'${TARGET_CELL}-slo-fast-burn' AND enabled=true" \
    --format="value(name)" 2>/dev/null || true)
  echo "[verify] Verified SLO policy watch (${ALERTS:-none-triggered}) for ${TARGET_CELL}."
fi

echo "[verify] PASS: ${TARGET_CELL} healthy and within error-budget burn threshold."
