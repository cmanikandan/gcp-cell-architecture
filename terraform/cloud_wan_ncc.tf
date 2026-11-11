# ------------------------------------------------------------------------------
# Cloud WAN (Network Connectivity Center) Hub & Spoke Isolation
# Connects hybrid enterprise branches north-south while blocking east-west cross-cell traffic
# ------------------------------------------------------------------------------

resource "google_network_connectivity_hub" "cloud_wan_hub" {
  project        = var.platform_project_id
  name           = "enterprise-cellular-wan-hub"
  description    = "Cloud WAN hub isolating cellular VPC spokes while enabling hybrid north-south ingress"
  preset_topology = "STAR"
}

# Center group: Shared hybrid ingress VPC (Cloud Interconnect / HA VPN / Regional ALB proxies)
resource "google_network_connectivity_group" "center_ingress_group" {
  project     = var.platform_project_id
  hub         = google_network_connectivity_hub.cloud_wan_hub.id
  name        = "center"
  description = "North-south enterprise WAN ingress spokes (Interconnect / SD-WAN)"
  auto_accept {
    auto_accept_projects = [var.platform_project_id]
  }
}

# Edge group: Isolated Cell VPCs — STAR topology ensures edge spokes NEVER exchange routes
# with each other (zero east-west cross-cell blast radius), only with center ingress spokes.
resource "google_network_connectivity_group" "isolated_cell_spokes" {
  project     = var.platform_project_id
  hub         = google_network_connectivity_hub.cloud_wan_hub.id
  name        = "edge"
  description = "Isolated cell VPC spokes — east-west cell-to-cell routing is strictly prohibited"
  auto_accept {
    auto_accept_projects = values(var.cell_project_ids)
  }
}

resource "google_network_connectivity_spoke" "cell_vpc_spoke" {
  for_each    = local.cells
  project     = var.platform_project_id
  name        = "${each.key}-vpc-spoke"
  location    = "global"
  hub         = google_network_connectivity_hub.cloud_wan_hub.id
  group       = google_network_connectivity_group.isolated_cell_spokes.id
  description = "Isolated VPC spoke for ${each.key}"

  linked_vpc_network {
    uri = google_compute_network.cell_vpc[each.key].self_link
  }
}
