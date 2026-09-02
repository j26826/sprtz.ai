# Cloud Build needs somewhere to keep build logs it owns.
resource "google_storage_bucket" "build_logs" {
  name                        = "${local.prefix}-build-logs-${random_id.bucket_suffix.hex}"
  project                     = var.project_id
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true
  labels                      = local.common_labels

  lifecycle_rule {
    condition {
      age = 30
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.services]
}

resource "google_storage_bucket_iam_member" "cloudbuild_logs" {
  bucket = google_storage_bucket.build_logs.name
  role   = "roles/storage.admin"
  member = "serviceAccount:${google_service_account.cloudbuild.email}"
}

# Requires the GitHub repo to be connected to Cloud Build once, by hand or with
# `gcloud builds connections`. Guarded so a first apply works without it.
resource "google_cloudbuild_trigger" "main" {
  count = var.create_build_trigger ? 1 : 0

  project     = var.project_id
  location    = var.region
  name        = "${local.prefix}-main"
  description = "Build, test and deploy Sportscut on push to main."

  service_account = google_service_account.cloudbuild.id
  filename        = "deploy/cloudbuild.yaml"

  github {
    owner = var.github_owner
    name  = var.github_repo
    push {
      branch = "^main$"
    }
  }

  substitutions = {
    _REGION          = var.region
    _ENVIRONMENT     = var.environment
    _APP_NAME        = var.app_name
    _AR_REPO         = google_artifact_registry_repository.containers.repository_id
    _TF_STATE_BUCKET = local.tf_state_bucket
    _AGENT_SA        = google_service_account.agent.email
  }
}
