# ------------------------------------------------------------------------------
# Cell Router Ingress: Global External ALB (Anycast + Proxy-Wasm) vs Regional ALB
# ------------------------------------------------------------------------------

resource "google_compute_region_network_endpoint_group" "cell_serverless_neg" {
  for_each              = var.compute_runtime == "cloud_run" ? local.cells : {}
  project               = each.value.project_id
  name                  = "${each.key}-serverless-neg"
  network_endpoint_type = "SERVERLESS"
  region                = each.value.region

  cloud_run {
    service = google_cloud_run_v2_service.cell_api[each.key].name
  }
}

resource "google_compute_security_policy" "cell_edge_armor" {
  project     = var.platform_project_id
  name        = "cell-router-edge-armor"
  description = "Cloud Armor per-tenant rate limiting ahead of cell routing"

  dynamic "adaptive_protection_config" {
    for_each = var.enable_adaptive_protection ? [1] : []
    content {
      layer_7_ddos_defense_config {
        enable = true
      }
    }
  }

  rule {
    action   = "throttle"
    priority = 1000
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
    rate_limit_options {
      conform_action      = "allow"
      exceed_action       = "deny(429)"
      enforce_on_key      = "HTTP_HEADER"
      enforce_on_key_name = "x-tenant-id"
      rate_limit_threshold {
        count        = 2000
        interval_sec = 60
      }
    }
  }

  rule {
    action   = "allow"
    priority = 2147483647
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
  }
}

# One isolated Backend Service per cell with outlier detection circuit-breaking
resource "google_compute_backend_service" "cell_backend_global" {
  for_each              = var.ingress_mode == "global" ? local.cells : {}
  project               = var.platform_project_id
  name                  = "${each.key}-global-backend"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  protocol              = "HTTPS"
  security_policy       = google_compute_security_policy.cell_edge_armor.id

  backend {
    group = google_compute_region_network_endpoint_group.cell_serverless_neg[each.key].id
  }

  dynamic "outlier_detection" {
    for_each = var.compute_runtime == "gke" ? [1] : []
    content {
      consecutive_errors = 3
      interval {
        seconds = 5
      }
      base_ejection_time {
        seconds = 30
      }
      max_ejection_percent = 100
    }
  }
}

# Global URL Map steering requests via x-cell-id header stamped by Proxy-Wasm LbEdgeExtension
resource "google_compute_url_map" "cell_router_global" {
  count           = var.ingress_mode == "global" ? 1 : 0
  project         = var.platform_project_id
  name            = "global-cell-router-urlmap"
  default_service = google_compute_backend_service.cell_backend_global[local.sorted_cell_ids[0]].id

  host_rule {
    hosts        = ["*"]
    path_matcher = "cellular-routes"
  }

  path_matcher {
    name            = "cellular-routes"
    default_service = google_compute_backend_service.cell_backend_global[local.sorted_cell_ids[0]].id

    dynamic "route_rules" {
      for_each = local.cells
      content {
        priority = 100 + route_rules.value.index
        match_rules {
          prefix_match = "/"
          header_matches {
            header_name = "x-cell-id"
            exact_match = route_rules.key
          }
        }
        route_action {
          retry_policy {
            num_retries      = max(1, local.effective_shard_size - 1)
            retry_conditions = ["5xx", "connect-failure", "refused-stream"]
          }
        }
        service = google_compute_backend_service.cell_backend_global[route_rules.key].id
      }
    }
  }
}

resource "google_compute_global_address" "anycast_vip" {
  count   = var.ingress_mode == "global" ? 1 : 0
  project = var.platform_project_id
  name    = "cell-router-anycast-vip"
}

resource "google_compute_target_http_proxy" "cell_http_proxy" {
  count   = var.ingress_mode == "global" ? 1 : 0
  project = var.platform_project_id
  name    = "cell-router-global-proxy"
  url_map = google_compute_url_map.cell_router_global[0].id
}

resource "google_compute_global_forwarding_rule" "cell_anycast_fr" {
  count                 = var.ingress_mode == "global" ? 1 : 0
  project               = var.platform_project_id
  name                  = "cell-router-anycast-fr"
  ip_address            = google_compute_global_address.anycast_vip[0].address
  ip_protocol           = "TCP"
  port_range            = "80"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  network_tier          = "PREMIUM"
  target                = google_compute_target_http_proxy.cell_http_proxy[0].id
}

locals {
  wasm_plugin_config_b64 = base64encode(jsonencode({
    total_cells   = length(local.cells)
    shard_size    = local.effective_shard_size
    hash_salt     = "gcp-cell-v1-prod"
    drained_cells = []
  }))
  wasm_version_id = "v-${substr(md5("${var.wasm_plugin_image_uri}:${local.wasm_plugin_config_b64}"), 0, 8)}"
}

# Service Extensions Proxy-Wasm Plugin computing deterministic shuffle-shard cell assignment
resource "google_network_services_wasm_plugin" "shuffle_router" {
  provider        = google-beta
  count           = var.ingress_mode == "global" ? 1 : 0
  project         = var.platform_project_id
  name            = "cell-shuffle-router-wasm"
  description     = "Deterministic Fisher-Yates shuffle-sharding cell router"
  location        = "global"
  main_version_id = local.wasm_version_id

  log_config {
    enable        = true
    sample_rate   = 0.05
    min_log_level = "INFO"
  }

  versions {
    version_name       = local.wasm_version_id
    description        = "Shuffle-sharding router plugin (${local.wasm_version_id})"
    image_uri          = var.wasm_plugin_image_uri
    plugin_config_data = local.wasm_plugin_config_b64
  }
}

# LbEdgeExtension executes BEFORE URL map evaluation on Global External ALBs so the
# x-cell-id header injected by Proxy-Wasm steers each request to its isolated cell backend.
resource "google_network_services_lb_edge_extension" "cell_shuffle_extension" {
  provider              = google-beta
  count                 = var.ingress_mode == "global" ? 1 : 0
  project               = var.platform_project_id
  name                  = "cell-shuffle-edge-ext"
  description           = "Pre-routing edge extension stamping x-cell-id per tenant virtual shard"
  location              = "global"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  forwarding_rules      = [google_compute_global_forwarding_rule.cell_anycast_fr[0].self_link]

  extension_chains {
    name = "shuffle-shard-header-chain"
    match_condition {
      cel_expression = "true"
    }
    extensions {
      name             = "shuffle-wasm"
      service          = google_network_services_wasm_plugin.shuffle_router[0].id
      fail_open        = false
      supported_events = ["REQUEST_HEADERS"]
    }
  }
}

# ------------------------------------------------------------------------------
# Regional External ALB Alternative (Strict Regional Fault & Sovereignty Boundary)
# ------------------------------------------------------------------------------

resource "google_compute_region_backend_service" "cell_backend_regional" {
  for_each              = var.ingress_mode == "regional" ? local.cells : {}
  project               = var.platform_project_id
  name                  = "${each.key}-regional-backend"
  region                = each.value.region
  load_balancing_scheme = "EXTERNAL_MANAGED"
  protocol              = "HTTPS"

  backend {
    group = google_compute_region_network_endpoint_group.cell_serverless_neg[each.key].id
  }
}
