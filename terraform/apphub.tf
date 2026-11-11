# ------------------------------------------------------------------------------
# App Hub & Cloud Hub Multi-Project Cellular Governance
# Models each cell as an explicit App Hub Application so Cloud Hub surfaces
# per-cell SLOs, alerts, incidents, and rollout states without shared blast radius.
# ------------------------------------------------------------------------------

resource "google_apphub_service_project_attachment" "cell_attachment" {
  for_each                     = local.cells
  project                      = var.platform_project_id
  service_project_attachment_id = each.value.project_id
}

resource "google_apphub_application" "cell_application" {
  for_each       = local.cells
  project        = var.platform_project_id
  location       = each.value.region
  application_id = "saas-${each.key}"
  display_name   = "SaaS Isolation Unit (${each.key})"
  description    = "Autonomous fault-isolated cell ${each.key} monitored in Cloud Hub"

  scope {
    type = "REGIONAL"
  }

  attributes {
    criticality {
      type = "MISSION_CRITICAL"
    }
    environment {
      type = "PRODUCTION"
    }
    operator_owners {
      display_name = "Platform SRE Cell Operations"
      email        = "sre-cells@example.com"
    }
  }

  depends_on = [google_apphub_service_project_attachment.cell_attachment]
}

# Discover and bind each cell's Cloud Run service and Spanner database to its App Hub boundary
data "google_apphub_discovered_service" "cell_run_discovered" {
  for_each    = var.compute_runtime == "cloud_run" ? local.cells : {}
  project     = var.platform_project_id
  location    = each.value.region
  service_uri = "//run.googleapis.com/${google_cloud_run_v2_service.cell_api[each.key].id}"

  depends_on = [google_apphub_service_project_attachment.cell_attachment]
}

resource "google_apphub_service" "cell_run_registered" {
  for_each           = var.compute_runtime == "cloud_run" ? local.cells : {}
  project            = var.platform_project_id
  location           = each.value.region
  application_id     = google_apphub_application.cell_application[each.key].application_id
  service_id         = "${each.key}-api-svc"
  display_name       = "${each.key} API Service"
  discovered_service = data.google_apphub_discovered_service.cell_run_discovered[each.key].name

  attributes {
    criticality {
      type = "MISSION_CRITICAL"
    }
    environment {
      type = "PRODUCTION"
    }
  }
}
