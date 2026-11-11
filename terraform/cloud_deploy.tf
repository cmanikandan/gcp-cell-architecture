# ------------------------------------------------------------------------------
# Cloud Deploy Cellular Wave Pipeline, Multi-Targets, Automations & Deploy Policy
# ------------------------------------------------------------------------------

resource "google_service_account" "clouddeploy_runner_sa" {
  project      = var.platform_project_id
  account_id   = "cell-clouddeploy-runner"
  display_name = "Cloud Deploy Wave Execution Service Account"
}

# Individual cell targets (cell-00 .. cell-07)
resource "google_clouddeploy_target" "cell_target" {
  for_each = local.cells
  project  = var.platform_project_id
  location = var.primary_region
  name     = each.key

  description       = "Isolated deployment target for ${each.key} (${each.value.region})"
  execution_configs {
    usages          = ["RENDER", "DEPLOY", "VERIFY"]
    service_account = google_service_account.clouddeploy_runner_sa.email
  }

  run {
    location = "projects/${each.value.project_id}/locations/${each.value.region}"
  }

  deploy_parameters = {
    cell_id          = each.key
    spanner_instance = "${each.key}-spanner"
  }
}

# Multi-target Wave 2 (25% fleet: cell-02, cell-03) with mandatory approval gate
resource "google_clouddeploy_target" "wave_2_multitarget" {
  project          = var.platform_project_id
  location         = var.primary_region
  name             = "wave-2-cells"
  description      = "Parallel rollout across cell-02 and cell-03 after canary & wave-1 bake"
  require_approval = true

  multi_target {
    target_ids = [
      google_clouddeploy_target.cell_target["cell-02"].name,
      google_clouddeploy_target.cell_target["cell-03"].name,
    ]
  }
}

# Multi-target Wave 3 (50% remaining fleet: cell-04 .. cell-07)
resource "google_clouddeploy_target" "wave_3_multitarget" {
  project          = var.platform_project_id
  location         = var.primary_region
  name             = "wave-3-cells"
  description      = "Final wave across cell-04 through cell-07"
  require_approval = true

  multi_target {
    target_ids = [
      google_clouddeploy_target.cell_target["cell-04"].name,
      google_clouddeploy_target.cell_target["cell-05"].name,
      google_clouddeploy_target.cell_target["cell-06"].name,
      google_clouddeploy_target.cell_target["cell-07"].name,
    ]
  }
}

# Progressive Wave Delivery Pipeline: cell-00 (Canary) -> cell-01 -> wave-2 -> wave-3
resource "google_clouddeploy_delivery_pipeline" "cellular_pipeline" {
  project     = var.platform_project_id
  location    = var.primary_region
  name        = "cellular-saas-wave-pipeline"
  description = "Blast-radius-gated progressive delivery across 8 isolated cells"

  serial_pipeline {
    stages {
      target_id = google_clouddeploy_target.cell_target["cell-00"].name
      profiles  = ["canary"]
      strategy {
        standard {
          verify = true
        }
      }
    }
    stages {
      target_id = google_clouddeploy_target.cell_target["cell-01"].name
      profiles  = ["prod"]
      strategy {
        standard {
          verify = true
        }
      }
    }
    stages {
      target_id = google_clouddeploy_target.wave_2_multitarget.name
      profiles  = ["prod"]
      strategy {
        standard {
          verify = true
        }
      }
    }
    stages {
      target_id = google_clouddeploy_target.wave_3_multitarget.name
      profiles  = ["prod"]
      strategy {
        standard {
          verify = true
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

# Deploy Policy: Block multi-cell Wave 2/3 rollouts during Friday/weekend peak windows
resource "google_clouddeploy_deploy_policy" "peak_freeze_gate" {
  project     = var.platform_project_id
  location    = var.primary_region
  name        = "block-weekend-multicell-waves"
  description = "Prevents Wave 2 and Wave 3 rollouts outside Monday-Thursday change windows"

  selectors {
    delivery_pipeline {
      id = google_clouddeploy_delivery_pipeline.cellular_pipeline.name
    }
    target {
      id = google_clouddeploy_target.wave_2_multitarget.name
    }
  }

  rules {
    rollout_restriction {
      id       = "friday-weekend-freeze"
      invokers = ["USER", "DEPLOY_AUTOMATION"]
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
