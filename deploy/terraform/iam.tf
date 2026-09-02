locals {
  # project-level role -> service account email
  project_roles = merge(
    { for r in [
      "roles/aiplatform.user",
      "roles/datastore.user",
      "roles/logging.logWriter",
      "roles/cloudtrace.agent",
      "roles/serviceusage.serviceUsageConsumer",
      ] : "agent:${r}" => { role = r, member = "serviceAccount:${google_service_account.agent.email}" }
    },
    { for r in [
      "roles/aiplatform.user",
      "roles/datastore.user",
      "roles/logging.logWriter",
      "roles/cloudtrace.agent",
      "roles/iam.serviceAccountTokenCreator",
      ] : "api:${r}" => { role = r, member = "serviceAccount:${google_service_account.api.email}" }
    },
    { for r in [
      "roles/logging.logWriter",
      ] : "web:${r}" => { role = r, member = "serviceAccount:${google_service_account.web.email}" }
    },
    { for r in [
      "roles/logging.logWriter",
      "roles/cloudtrace.agent",
      # Packaging for playback is a Transcoder job now, so this service creates
      # them and polls them. It never touches the video itself.
      "roles/transcoder.admin",
      ] : "mcp_media:${r}" => { role = r, member = "serviceAccount:${google_service_account.mcp_media.email}" }
    },
    { for r in [
      "roles/datastore.user",
      "roles/aiplatform.user",
      "roles/logging.logWriter",
      "roles/cloudtrace.agent",
      ] : "mcp_catalog:${r}" => { role = r, member = "serviceAccount:${google_service_account.mcp_catalog.email}" }
    },
    { for r in [
      "roles/artifactregistry.writer",
      "roles/run.admin",
      "roles/aiplatform.admin",
      "roles/datastore.owner",
      "roles/storage.admin",
      "roles/iam.serviceAccountUser",
      "roles/iam.serviceAccountAdmin",
      "roles/logging.logWriter",
      "roles/serviceusage.serviceUsageAdmin",
      "roles/iap.admin",
      "roles/firebaserules.admin",
      ] : "cloudbuild:${r}" => { role = r, member = "serviceAccount:${google_service_account.cloudbuild.email}" }
    },
  )
}

resource "google_project_iam_member" "workload" {
  for_each = local.project_roles

  project = var.project_id
  role    = each.value.role
  member  = each.value.member
}

# --- Bucket-scoped access -----------------------------------------------------
# The API only ever needs to hand out signed URLs, so it gets object-level access
# to uploads and read on media; only the media worker may write derived output.

resource "google_storage_bucket_iam_member" "api_uploads" {
  bucket = google_storage_bucket.uploads.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.api.email}"
}

# The API reads playlists out of the HLS bucket to rewrite them; it never
# touches segments, which go straight from the CDN to the player.
resource "google_storage_bucket_iam_member" "api_hls_read" {
  bucket = google_storage_bucket.hls.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.api.email}"
}

resource "google_storage_bucket_iam_member" "api_media_read" {
  bucket = google_storage_bucket.media.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.api.email}"
}

resource "google_storage_bucket_iam_member" "media_worker_uploads_read" {
  bucket = google_storage_bucket.uploads.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.mcp_media.email}"
}

# The Transcoder service agent, not the media service, is what actually opens
# the source and writes the HLS package. Missing either of these fails minutes
# into an encode rather than when the job is created.
resource "google_storage_bucket_iam_member" "transcoder_uploads_read" {
  bucket = google_storage_bucket.uploads.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_project_service_identity.transcoder.email}"
}

resource "google_storage_bucket_iam_member" "transcoder_hls_write" {
  bucket = google_storage_bucket.hls.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_project_service_identity.transcoder.email}"
}

resource "google_storage_bucket_iam_member" "media_worker_media_write" {
  bucket = google_storage_bucket.media.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.mcp_media.email}"
}

resource "google_storage_bucket_iam_member" "agent_logs" {
  bucket = google_storage_bucket.agent_logs.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.agent.email}"
}

# Gemini reads the video straight from GCS during the multimodal pass.
resource "google_storage_bucket_iam_member" "vertex_uploads_read" {
  bucket = google_storage_bucket.uploads.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_project_service_identity.vertex.email}"
}

resource "google_storage_bucket_iam_member" "vertex_media_read" {
  bucket = google_storage_bucket.media.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_project_service_identity.vertex.email}"
}

# --- Service-to-service invocation --------------------------------------------
# MCP servers have no public ingress; only the agent (and the API, for health
# checks and manual re-runs) may invoke them.

resource "google_cloud_run_v2_service_iam_member" "agent_invokes_media" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.mcp_media.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.agent.email}"
}

resource "google_cloud_run_v2_service_iam_member" "agent_invokes_catalog" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.mcp_catalog.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.agent.email}"
}

resource "google_cloud_run_v2_service_iam_member" "api_invokes_catalog" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.mcp_catalog.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.api.email}"
}

resource "google_cloud_run_v2_service_iam_member" "api_invokes_media" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.mcp_media.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.api.email}"
}

# The API mints V4 signed URLs by signing as itself via the IAM Credentials API,
# which requires it to be a token creator on its own identity.
resource "google_service_account_iam_member" "api_self_sign" {
  service_account_id = google_service_account.api.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.api.email}"
}

# Cloud Build deploys as each workload identity.
resource "google_service_account_iam_member" "cloudbuild_act_as" {
  for_each = {
    agent       = google_service_account.agent.name
    api         = google_service_account.api.name
    web         = google_service_account.web.name
    mcp_media   = google_service_account.mcp_media.name
    mcp_catalog = google_service_account.mcp_catalog.name
  }

  service_account_id = each.value
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.cloudbuild.email}"
}
