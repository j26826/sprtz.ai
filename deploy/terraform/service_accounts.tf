# One service account per workload so a compromised container cannot reach
# further than the job it exists to do.

resource "google_service_account" "agent" {
  project      = var.project_id
  account_id   = "${local.prefix}-agent"
  display_name = "Sportscut — Vertex Agent Runtime"
  description  = "Runs the sprtz_producer ADK agent on Agent Runtime."
}

resource "google_service_account" "api" {
  project      = var.project_id
  account_id   = "${local.prefix}-api"
  display_name = "Sportscut — API service"
  description  = "FastAPI backend behind IAP. Signs upload URLs and proxies agent sessions."
}

resource "google_service_account" "web" {
  project      = var.project_id
  account_id   = "${local.prefix}-web"
  display_name = "Sportscut — Web service"
  description  = "Serves the Sportscut editor SPA. Holds no data permissions."
}

resource "google_service_account" "mcp_media" {
  project      = var.project_id
  account_id   = "${local.prefix}-mcp-media"
  display_name = "Sportscut — MCP media server"
  description  = "ffmpeg tool server. Reads uploads, writes derived media."
}

resource "google_service_account" "mcp_catalog" {
  project      = var.project_id
  account_id   = "${local.prefix}-mcp-catalog"
  display_name = "Sportscut — MCP catalog server"
  description  = "Firestore + embedding tool server."
}

resource "google_service_account" "cloudbuild" {
  project      = var.project_id
  account_id   = "${local.prefix}-cloudbuild"
  display_name = "Sportscut — Cloud Build"
  description  = "Builds images, applies Terraform, deploys the agent."
}
