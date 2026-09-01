output "web_url" {
  description = "SPRTZ AI Editor URL (IAP-protected)."
  value       = google_cloud_run_v2_service.web.uri
}

output "api_url" {
  description = "API base URL (IAP-protected)."
  value       = google_cloud_run_v2_service.api.uri
}

output "mcp_catalog_url" {
  description = "Internal URL of the catalog MCP server."
  value       = google_cloud_run_v2_service.mcp_catalog.uri
}

output "mcp_media_url" {
  description = "Internal URL of the media MCP server."
  value       = google_cloud_run_v2_service.mcp_media.uri
}

output "vertex_region" {
  description = "Region hosting Agent Runtime and Gemini."
  value       = var.vertex_region
}

output "agent_engine_resource" {
  description = "Fully-qualified Agent Runtime resource name for the producer agent."
  value       = google_vertex_ai_reasoning_engine.producer.name
}

output "uploads_bucket" {
  description = "Bucket receiving raw video uploads."
  value       = google_storage_bucket.uploads.name
}

output "media_bucket" {
  description = "Bucket holding proxies, segments and rendered clips."
  value       = google_storage_bucket.media.name
}

output "tf_state_bucket" {
  description = "Bucket holding Terraform state for CI applies."
  value       = google_storage_bucket.tf_state.name
}

output "artifact_registry" {
  description = "Docker image path prefix."
  value       = local.image_base
}

output "agent_logs_bucket" {
  description = "Bucket holding agent artifacts and telemetry."
  value       = google_storage_bucket.agent_logs.name
}

output "agent_service_account" {
  value = google_service_account.agent.email
}

output "cloudbuild_service_account" {
  value = google_service_account.cloudbuild.email
}

output "firebase_config" {
  description = "Client config for the Firebase JS SDK (Identity Platform + Firestore)."
  sensitive   = true
  value = {
    apiKey     = google_apikeys_key.identity_platform.key_string
    authDomain = "${var.project_id}.firebaseapp.com"
    projectId  = var.project_id
  }
}

output "cdn_ip" {
  description = "Global IP of the HLS load balancer. Point cdn_domain's A record here."
  value       = google_compute_global_address.cdn.address
}

output "cdn_base_url" {
  description = "Base URL for HLS playback."
  value       = local.cdn_base_url
}

output "hls_bucket" {
  description = "Bucket holding the HLS packages."
  value       = google_storage_bucket.hls.name
}
