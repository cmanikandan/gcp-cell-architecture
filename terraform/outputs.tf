output "anycast_cell_router_ip" {
  description = "Global Anycast VIP for the external Application Load Balancer cell router."
  value       = var.ingress_mode == "global" ? google_compute_global_address.anycast_vip[0].address : null
}

output "cloud_wan_hub_id" {
  description = "Network Connectivity Center Hub ID enforcing STAR isolation across cell VPCs."
  value       = google_network_connectivity_hub.cloud_wan_hub.id
}

output "cloud_deploy_pipeline" {
  description = "Cloud Deploy progressive wave pipeline name."
  value       = google_clouddeploy_delivery_pipeline.cellular_pipeline.name
}

output "apphub_cell_applications" {
  description = "Registered App Hub application IDs visible in Cloud Hub."
  value       = { for k, v in google_apphub_application.cell_application : k => v.id }
}
