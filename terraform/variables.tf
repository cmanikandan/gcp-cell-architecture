variable "platform_project_id" {
  description = "Shared routing, Cloud Deploy, and App Hub management host project ID (also hosts cells in single-project POC mode)."
  type        = string
}

variable "cell_project_ids" {
  description = "Optional map of cell ID (cell-00 .. cell-07) to dedicated Google Cloud project IDs for multi-project hard isolation. Leave empty ({}) for single-project sandbox/Cloud Shell POC mode."
  type        = map(string)
  default     = {}
}

variable "poc_cell_count" {
  description = "Number of isolated cells to provision when cell_project_ids is empty (defaults to 4 in single-project POC mode so 4 cell VPCs + default VPC fit within Google Cloud's default 5-VPC per-project quota)."
  type        = number
  default     = 4
  validation {
    condition     = var.poc_cell_count >= 2 && var.poc_cell_count <= 16
    error_message = "poc_cell_count must be between 2 and 16."
  }
}

variable "shard_size" {
  description = "Shuffle-sharding virtual shard width k (clamped automatically when total cells < 8)."
  type        = number
  default     = 3
}

variable "dedicated_vpc_per_cell" {
  description = "Create a dedicated VPC per cell (true by default; set false if a single-project sandbox already has existing custom VPCs near the 5-VPC quota)."
  type        = bool
  default     = true
}

variable "primary_region" {
  description = "Default Google Cloud region for management plane and even-numbered cell placement."
  type        = string
  default     = "us-central1"
}

variable "secondary_region" {
  description = "Secondary Google Cloud region for odd-numbered cell placement."
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
  default     = "us-central1-docker.pkg.dev/saas-platform-prod/cells/cell-api:v1.4.3"
}

variable "wasm_plugin_image_uri" {
  description = "Artifact Registry Docker/OCI image URI of compiled Proxy-Wasm shuffle-sharding router."
  type        = string
  default     = "us-central1-docker.pkg.dev/saas-platform-prod/cells/shuffle-router:v0.1.1"
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

variable "allow_unauthenticated_invoker" {
  description = "Grant roles/run.invoker to allUsers on cell Cloud Run services so External ALB probes work out of the box in POC mode (direct *.run.app access remains blocked by INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER)."
  type        = bool
  default     = true
}

variable "bind_apphub_discovered_services" {
  description = "Bind asynchronously discovered Cloud Run services into App Hub applications (false on initial apply because GCP App Hub service discovery lags Cloud Run creation by ~2 minutes; set true on a subsequent apply if desired)."
  type        = bool
  default     = false
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
  resolved_cell_projects = length(var.cell_project_ids) > 0 ? var.cell_project_ids : {
    for idx in range(var.poc_cell_count) : format("cell-%02d", idx) => var.platform_project_id
  }

  sorted_cell_ids = sort(keys(local.resolved_cell_projects))

  cells = {
    for idx, cell_id in local.sorted_cell_ids : cell_id => {
      index      = idx
      project_id = local.resolved_cell_projects[cell_id]
      region     = idx % 2 == 0 ? var.primary_region : var.secondary_region
      wave       = idx == 0 ? "canary" : (idx == 1 ? "wave-1" : (idx < 4 ? "wave-2" : "wave-3"))
    }
  }

  unique_project_ids = distinct(concat(
    [var.platform_project_id],
    [for c in values(local.cells) : c.project_id]
  ))

  effective_shard_size = length(local.cells) >= 8 ? min(var.shard_size, length(local.cells) - 1) : min(2, length(local.cells) - 1)
}
