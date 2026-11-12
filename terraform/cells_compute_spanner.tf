# ------------------------------------------------------------------------------
# Cell Compute (Cloud Run v2 / GKE) & Dedicated Spanner Isolation per Cell
# ------------------------------------------------------------------------------

resource "google_compute_network" "cell_vpc" {
  for_each                = var.dedicated_vpc_per_cell ? local.cells : {}
  project                 = each.value.project_id
  name                    = "${each.key}-vpc"
  auto_create_subnetworks = false
}

# Fallback shared sandbox VPC used only when a single-project POC already has
# custom VPCs consuming Google Cloud's default 5-VPC per-project quota.
resource "google_compute_network" "shared_poc_vpc" {
  count                   = var.dedicated_vpc_per_cell ? 0 : 1
  project                 = var.platform_project_id
  name                    = "cellular-poc-vpc"
  auto_create_subnetworks = false
}

locals {
  cell_network_id = {
    for k, v in local.cells :
    k => (var.dedicated_vpc_per_cell ? google_compute_network.cell_vpc[k].id : google_compute_network.shared_poc_vpc[0].id)
  }
}

resource "google_compute_subnetwork" "cell_subnet" {
  for_each                 = local.cells
  project                  = each.value.project_id
  name                     = "${each.key}-subnet"
  region                   = each.value.region
  network                  = local.cell_network_id[each.key]
  ip_cidr_range            = cidrsubnet("10.64.0.0/12", 8, each.value.index)
  private_ip_google_access = true
}

# Dedicated Spanner instance per cell prevents noisy-neighbor CPU saturation,
# bad Partitioned DML locks, or schema rollouts from crossing cell boundaries.
resource "google_spanner_instance" "cell_db" {
  for_each         = local.cells
  project          = each.value.project_id
  name             = "${each.key}-spanner"
  config           = "regional-${each.value.region}"
  display_name     = "Isolated Spanner (${each.key})"
  processing_units = var.spanner_processing_units
  edition          = var.spanner_edition

  labels = {
    cell_id = each.key
    wave    = each.value.wave
  }
}

resource "google_spanner_database" "cell_ledger" {
  for_each = local.cells
  project  = each.value.project_id
  instance = google_spanner_instance.cell_db[each.key].name
  name     = "cell_ledger"

  ddl = [
    <<-EOT
    CREATE TABLE Tenants (
      TenantId   STRING(64) NOT NULL,
      Tier       STRING(32) NOT NULL,
      CreatedAt  TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true)
    ) PRIMARY KEY (TenantId)
    EOT
    ,
    <<-EOT
    CREATE TABLE TenantEvents (
      TenantId   STRING(64) NOT NULL,
      EventId    STRING(64) NOT NULL,
      Payload    JSON,
      RecordedAt TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true)
    ) PRIMARY KEY (TenantId, EventId),
      INTERLEAVE IN PARENT Tenants ON DELETE CASCADE
    EOT
  ]

  deletion_protection = var.enable_deletion_protection
}

resource "google_service_account" "cell_workload_sa" {
  for_each     = local.cells
  project      = each.value.project_id
  account_id   = "${each.key}-workload-sa"
  display_name = "Workload Identity SA for ${each.key}"
}

resource "google_spanner_database_iam_member" "cell_db_user" {
  for_each = local.cells
  project  = each.value.project_id
  instance = google_spanner_instance.cell_db[each.key].name
  database = google_spanner_database.cell_ledger[each.key].name
  role     = "roles/spanner.databaseUser"
  member   = "serviceAccount:${google_service_account.cell_workload_sa[each.key].email}"
}

# Cell-scoped Cloud Run service with Direct VPC Egress into the cell's isolated subnet.
# Uses `invoker_iam_disabled` instead of an `allUsers` IAM binding so External ALB
# Serverless NEG probes work cleanly under Domain Restricted Sharing org policies
# while `INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER` blocks direct *.run.app access.
resource "google_cloud_run_v2_service" "cell_api" {
  for_each             = var.compute_runtime == "cloud_run" ? local.cells : {}
  project              = each.value.project_id
  name                 = "${each.key}-api"
  location             = each.value.region
  ingress              = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
  invoker_iam_disabled = var.allow_unauthenticated_invoker
  deletion_protection  = var.enable_deletion_protection

  template {
    service_account = google_service_account.cell_workload_sa[each.key].email

    scaling {
      min_instance_count = var.cloud_run_min_instances
      max_instance_count = 50
    }

    vpc_access {
      network_interfaces {
        network    = local.cell_network_id[each.key]
        subnetwork = google_compute_subnetwork.cell_subnet[each.key].id
      }
      egress = "PRIVATE_RANGES_ONLY"
    }

    containers {
      image = var.cell_container_image
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = each.value.project_id
      }
      env {
        name  = "CELL_ID"
        value = each.key
      }
      env {
        name  = "CELL_REGION"
        value = each.value.region
      }
      env {
        name  = "SPANNER_INSTANCE"
        value = google_spanner_instance.cell_db[each.key].name
      }
      env {
        name  = "SPANNER_DATABASE"
        value = google_spanner_database.cell_ledger[each.key].name
      }
      startup_probe {
        http_get {
          path = "/healthz"
          port = 8080
        }
      }
    }
  }
}

# Optional GKE Autopilot cluster per cell when stateful sidecars or custom GPUs are required
resource "google_container_cluster" "cell_gke" {
  for_each            = var.compute_runtime == "gke" ? local.cells : {}
  project             = each.value.project_id
  name                = "${each.key}-autopilot"
  location            = each.value.region
  enable_autopilot    = true
  deletion_protection = var.enable_deletion_protection
  network             = local.cell_network_id[each.key]
  subnetwork          = google_compute_subnetwork.cell_subnet[each.key].id

  release_channel {
    channel = "REGULAR"
  }
}
