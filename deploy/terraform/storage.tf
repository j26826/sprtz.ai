resource "random_id" "bucket_suffix" {
  byte_length = 3
}

# Raw uploads. Written directly by the browser with a V4 signed URL.
resource "google_storage_bucket" "uploads" {
  name                        = "${local.prefix}-uploads-${random_id.bucket_suffix.hex}"
  project                     = var.project_id
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  force_destroy               = var.environment != "prod"
  labels                      = local.common_labels

  cors {
    origin          = var.upload_cors_origins
    method          = ["GET", "HEAD", "PUT", "OPTIONS"]
    response_header = ["Content-Type", "Content-Range", "x-goog-resumable"]
    max_age_seconds = 3600
  }

  lifecycle_rule {
    condition {
      age = var.video_retention_days
    }
    action {
      type = "Delete"
    }
  }

  versioning {
    enabled = false
  }

  depends_on = [google_project_service.services]
}

# Derived media: proxies, sampled frames, extracted audio, rendered clips.
resource "google_storage_bucket" "media" {
  name                        = "${local.prefix}-media-${random_id.bucket_suffix.hex}"
  project                     = var.project_id
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  force_destroy               = var.environment != "prod"
  labels                      = local.common_labels

  cors {
    origin          = var.upload_cors_origins
    method          = ["GET", "HEAD"]
    response_header = ["Content-Type", "Range"]
    max_age_seconds = 3600
  }

  depends_on = [google_project_service.services]
}

# Agent artifacts and build/telemetry logs.
resource "google_storage_bucket" "agent_logs" {
  name                        = "${local.prefix}-agent-logs-${random_id.bucket_suffix.hex}"
  project                     = var.project_id
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  force_destroy               = var.environment != "prod"
  labels                      = local.common_labels

  lifecycle_rule {
    condition {
      age = 90
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.services]
}
