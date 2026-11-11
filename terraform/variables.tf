variable "platform_project_id" {
  description = "Shared routing, Cloud Deploy, and App Hub management host project ID."
  type        = string
}

variable "cell_project_ids" {
  description = "Map of cell ID (cell-00 .. cell-07) to dedicated Google Cloud project ID for hard quota and IAM isolation."
  type        = map(string)
  default = {
    "cell-00" = "saas-prod-cell-00"
    "cell-01" = "saas-prod-cell-01"
    "cell-02" = "saas-prod-cell-02"
    "cell-03" = "saas-prod-cell-03"
    "cell-04" = "saas-prod-cell-04"
    "cell-05" = "saas-prod-cell-05"
    "cell-06" = "saas-prod-cell-06"
    "cell-07" = "saas-prod-cell-07"
  }
}

variable "primary_region" {
  description = "Default Google Cloud region for management plane and odd/even cell placement."
  type        = string
  default     = "us-central1"
}

variable "secondary_region" {
  description = "Secondary Google Cloud region for alternating cell placement."
  type        = string
  default     = "us-east4"
}

variable "ingress_mode" {
  description = "Ingress routing boundary: 'global' for Anycast Global External ALB + Wasm Service Extension, or 'regional' for Regional External ALBs."
  type        = string
  default     = "global"
  validation {
    condition     = contains(["global", "regional"], var.ingress_mode)
    error_message = "ingress_mode must be either 'global' or 'regional'."
  }
}

variable "compute_runtime" {
  description = "Compute runtime inside each cell ('cloud_run' or 'gke')."
  type        = string
  default     = "cloud_run"
}

variable "cell_container_image" {
  description = "Container image URI in Artifact Registry for the cell API service."
  type        = string
  default     = "us-docker.pkg.dev/saas-platform-prod/cells/cell-api:v1.4.0"
}

variable "wasm_plugin_image_uri" {
  description = "Artifact Registry OCI/generic URI of compiled Proxy-Wasm shuffle-sharding router."
  type        = string
  default     = "us-docker.pkg.dev/saas-platform-prod/wasm-plugins/shuffle-router:v0.1.0"
}

variable "spanner_processing_units" {
  description = "Dedicated Spanner processing units per isolated cell instance (min 100 PU for POC; scale up for production)."
  type        = number
  default     = 100
}

variable "spanner_edition" {
  description = "Spanner edition per cell ('STANDARD' for lowest POC cost, or 'ENTERPRISE' / 'ENTERPRISE_PLUS' for production)."
  type        = string
  default     = "STANDARD"
  validation {
    condition     = contains(["STANDARD", "ENTERPRISE", "ENTERPRISE_PLUS"], var.spanner_edition)
    error_message = "spanner_edition must be STANDARD, ENTERPRISE, or ENTERPRISE_PLUS."
  }
}

variable "cloud_run_min_instances" {
  description = "Minimum warm Cloud Run instances per cell (0 for scale-to-zero POC cost savings; 2+ recommended in production)."
  type        = number
  default     = 0
}

variable "enable_deletion_protection" {
  description = "Protect cell Spanner databases against accidental deletion (false for easy POC teardown; set true in production)."
  type        = bool
  default     = false
}

variable "enable_adaptive_protection" {
  description = "Enable Cloud Armor L7 Adaptive Protection (false keeps POC on Cloud Armor Standard pay-as-you-go tier)."
  type        = bool
  default     = false
}

locals {
  cells = {
    for idx, cell_id in sort(keys(var.cell_project_ids)) : cell_id => {
      index      = idx
      project_id = var.cell_project_ids[cell_id]
      region     = idx % 2 == 0 ? var.primary_region : var.secondary_region
      wave       = idx == 0 ? "canary" : (idx == 1 ? "wave-1" : (idx < 4 ? "wave-2" : "wave-3"))
    }
  }
}
