resource "google_project_service" "services" {
  for_each = toset(local.services)

  project = var.project_id
  service = each.value

  # Never tear an API down on destroy — other workloads in the project may rely on it.
  disable_on_destroy         = false
  disable_dependent_services = false
}

# Vertex AI's service agent must exist before it can be granted access to GCS.
resource "google_project_service_identity" "vertex" {
  provider = google-beta
  project  = var.project_id
  service  = "aiplatform.googleapis.com"

  depends_on = [google_project_service.services]
}

# Transcoder reads the source and writes the package itself, under its own
# service agent rather than the caller's identity. The agent has to exist
# before it can be granted anything, and it is only created on request.
resource "google_project_service_identity" "transcoder" {
  provider = google-beta
  project  = var.project_id
  service  = "transcoder.googleapis.com"

  depends_on = [google_project_service.services]
}

data "google_project" "this" {
  project_id = var.project_id

  depends_on = [google_project_service.services]
}
