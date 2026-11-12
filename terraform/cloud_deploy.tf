# ------------------------------------------------------------------------------
# Cloud Deploy Cellular Wave Pipeline, Multi-Targets, Automations & Deploy Policy
# ------------------------------------------------------------------------------

resource "google_service_account" "clouddeploy_runner_sa" {
  project      = var.platform_project_id
  account_id   = "cell-clouddeploy-runner"
  display_name = "Cloud Deploy Wave Execution Service Account"
}

locals {
  runner_platform_roles = [
    "roles/clouddeploy.jobRunner",
    "roles/clouddeploy.operator",
    "roles/logging.logWriter",
    "roles/storage.admin",
  ]
  runner_cell_project_roles = [
    "roles/run.developer",
    "roles/iam.serviceAccountUser",
  ]
  runner_cell_role_bindings = {
    for pair in setproduct(local.unique_project_ids, local.runner_cell_project_roles) :
    "${pair[0]}::${pair[1]}" => {
      project_id = pair[0]
      role       = pair[1]
    }
  }

  canary_cell_id = local.sorted_cell_ids[0]
  wave_1_cell_id = length(local.sorted_cell_ids) > 1 ? local.sorted_cell_ids[1] : null
  wave_2_cell_ids = slice(
    local.sorted_cell_ids,
    min(2, length(local.sorted_cell_ids)),
    min(4, length(local.sorted_cell_ids))
  )
  wave_3_cell_ids = slice(
    local.sorted_cell_ids,
    min(4, length(local.sorted_cell_ids)),
    length(local.sorted_cell_ids)
  )
}

resource "google_project_iam_member" "clouddeploy_runner_platform_iam" {
  for_each = toset(local.runner_platform_roles)
  project  = var.platform_project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.clouddeploy_runner_sa.email}"
}

resource "google_project_iam_member" "clouddeploy_runner_cell_iam" {
  for_each = local.runner_cell_role_bindings
  project  = each.value.project_id
  role     = each.value.role
  member   = "serviceAccount:${google_service_account.clouddeploy_runner_sa.email}"
}

# Individual cell targets (4 cells in single-project POC; 8+ cells in multi-project fleet)
resource "google_clouddeploy_target" "cell_target" {
  for_each = local.cells
  project  = var.platform_project_id
  location = var.primary_region
  name     = each.key

  description = "Isolated deployment target for ${each.key} (${each.value.region})"
  execution_configs {
    usages          = ["RENDER", "DEPLOY", "VERIFY"]
    service_account = google_service_account.clouddeploy_runner_sa.email
  }

  run {
    location = "projects/${each.value.project_id}/locations/${each.value.region}"
  }

  deploy_parameters = {
    service_name             = "${each.key}-api"
    cell_id                  = each.key
    cell_region              = each.value.region
    cell_project_id          = each.value.project_id
    spanner_instance         = "${each.key}-spanner"
    workload_service_account = google_service_account.cell_workload_sa[each.key].email
  }

  depends_on = [
    google_project_iam_member.clouddeploy_runner_platform_iam,
    google_project_iam_member.clouddeploy_runner_cell_iam,
  ]
}

# Multi-target Wave 2 (cell-02, cell-03) with mandatory approval gate
resource "google_clouddeploy_target" "wave_2_multitarget" {
  count            = length(local.wave_2_cell_ids) > 0 ? 1 : 0
  project          = var.platform_project_id
  location         = var.primary_region
  name             = "wave-2-cells"
  description      = "Parallel rollout across Wave 2 cells after canary & wave-1 bake"
  require_approval = true

  multi_target {
    target_ids = [for cid in local.wave_2_cell_ids : google_clouddeploy_target.cell_target[cid].name]
  }
}

# Multi-target Wave 3 (cell-04 .. cell-07 when 5+ cells are configured)
resource "google_clouddeploy_target" "wave_3_multitarget" {
  count            = length(local.wave_3_cell_ids) > 0 ? 1 : 0
  project          = var.platform_project_id
  location         = var.primary_region
  name             = "wave-3-cells"
  description      = "Final wave across remaining fleet cells"
  require_approval = true

  multi_target {
    target_ids = [for cid in local.wave_3_cell_ids : google_clouddeploy_target.cell_target[cid].name]
  }
}

# Progressive Wave Delivery Pipeline: cell-00 (Canary) -> cell-01 -> wave-2 (-> wave-3)
resource "google_clouddeploy_delivery_pipeline" "cellular_pipeline" {
  project     = var.platform_project_id
  location    = var.primary_region
  name        = "cellular-saas-wave-pipeline"
  description = "Blast-radius-gated progressive delivery across isolated cells"

  serial_pipeline {
    stages {
      target_id = google_clouddeploy_target.cell_target[local.canary_cell_id].name
      profiles  = ["canary"]
      strategy {
        standard {
          verify = true
        }
      }
    }

    dynamic "stages" {
      for_each = local.wave_1_cell_id != null ? [local.wave_1_cell_id] : []
      content {
        target_id = google_clouddeploy_target.cell_target[stages.value].name
        profiles  = ["prod"]
        strategy {
          standard {
            verify = true
          }
        }
      }
    }

    dynamic "stages" {
      for_each = length(local.wave_2_cell_ids) > 0 ? [1] : []
      content {
        target_id = google_clouddeploy_target.wave_2_multitarget[0].name
        profiles  = ["prod"]
        strategy {
          standard {
            verify = true
          }
        }
      }
    }

    dynamic "stages" {
      for_each = length(local.wave_3_cell_ids) > 0 ? [1] : []
      content {
        target_id = google_clouddeploy_target.wave_3_multitarget[0].name
        profiles  = ["prod"]
        strategy {
          standard {
            verify = true
          }
        }
      }
    }
  }
}

# Automated Rollback & Bake-Timer Promotion Rules
resource "google_clouddeploy_automation" "canary_auto_guard" {
  project           = var.platform_project_id
  location          = var.primary_region
  delivery_pipeline = google_clouddeploy_delivery_pipeline.cellular_pipeline.name
  name              = "cell-bake-and-autorepair"
  service_account   = google_service_account.clouddeploy_runner_sa.email

  selector {
    targets {
      id = "*"
    }
  }

  # Automatically roll back a cell if deployment or SLO verification fails
  rules {
    repair_rollout_rule {
      id     = "rollback-failed-cell"
      phases = ["stable", "verify"]
      repair_phases {
        rollback {}
      }
    }
  }

  # Enforce a 15-minute bake window after cell-00 verification before promoting to cell-01
  rules {
    promote_release_rule {
      id                    = "promote-after-canary-bake"
      wait                  = "900s"
      destination_target_id = "@next"
    }
  }
}

# Deploy Policy: Block multi-cell Wave 2 rollouts during Friday/weekend peak windows
resource "google_clouddeploy_deploy_policy" "peak_freeze_gate" {
  count       = length(local.wave_2_cell_ids) > 0 ? 1 : 0
  project     = var.platform_project_id
  location    = var.primary_region
  name        = "block-weekend-multicell-waves"
  description = "Prevents multi-cell wave rollouts outside Monday-Thursday change windows"

  selectors {
    delivery_pipeline {
      id = google_clouddeploy_delivery_pipeline.cellular_pipeline.name
    }
    target {
      id = google_clouddeploy_target.wave_2_multitarget[0].name
    }
  }

  rules {
    rollout_restriction {
      id       = "friday-weekend-freeze"
      invokers = ["DEPLOY_AUTOMATION"]
      actions  = ["CREATE"]
      time_windows {
        time_zone = "America/New_York"
        weekly_windows {
          days_of_week = ["FRIDAY", "SATURDAY", "SUNDAY"]
          start_time {
            hours   = 0
            minutes = 0
          }
          end_time {
            hours   = 23
            minutes = 59
          }
        }
      }
    }
  }
}
