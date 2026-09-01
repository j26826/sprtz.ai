# HLS delivery: a GCS bucket of playlists and segments, fronted by Cloud CDN
# through an external Application Load Balancer.
#
# Access is controlled with Cloud CDN signed URLs rather than by making the
# bucket public. Signing a URL *prefix* is what makes this workable for HLS: one
# signature covers a job's master playlist, its variant playlists and all of its
# segments, so the player never has to re-sign mid-stream.

resource "google_storage_bucket" "hls" {
  name                        = "${local.prefix}-hls-${random_id.bucket_suffix.hex}"
  project                     = var.project_id
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  force_destroy               = var.environment != "prod"
  labels                      = local.common_labels

  # The player fetches segments from this bucket cross-origin and, with signed
  # cookies, does so with credentials — so the allowed origins must be listed
  # explicitly rather than wildcarded.
  cors {
    origin          = var.upload_cors_origins
    method          = ["GET", "HEAD", "OPTIONS"]
    response_header = ["Content-Type", "Range", "Accept-Ranges", "Content-Length"]
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

  depends_on = [google_project_service.services]
}

resource "google_storage_bucket_iam_member" "media_worker_hls_write" {
  bucket = google_storage_bucket.hls.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.mcp_media.email}"
}

# The load balancer reads the bucket as the cloud-cdn-fill service agent. That
# agent does not exist until the project's first CDN-enabled backend is created,
# and its creation is asynchronous — granting it access in the same apply that
# creates the backend fails with "service account does not exist" unless the
# grant waits.
resource "time_sleep" "cdn_fill_agent" {
  depends_on      = [google_compute_backend_bucket.hls]
  create_duration = "120s"
}

resource "google_storage_bucket_iam_member" "cdn_read" {
  bucket = google_storage_bucket.hls.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:service-${data.google_project.this.number}@cloud-cdn-fill.iam.gserviceaccount.com"

  depends_on = [time_sleep.cdn_fill_agent]
}

resource "google_compute_backend_bucket" "hls" {
  project     = var.project_id
  name        = "${local.prefix}-hls-backend"
  bucket_name = google_storage_bucket.hls.name
  enable_cdn  = true

  cdn_policy {
    cache_mode = "USE_ORIGIN_HEADERS"

    # Segments are immutable and playlists carry a short max-age, so origin
    # headers are the right authority. See gcs.upload_directory.
    negative_caching = true
    negative_caching_policy {
      code = 404
      ttl  = 30
    }

    # How long a validated signature stays cacheable at the edge.
    signed_url_cache_max_age_sec = 3600

    cache_key_policy {
      include_http_headers   = []
      query_string_whitelist = []
    }
  }

  depends_on = [google_project_service.services]
}

# Signing key. The API reads it from Secret Manager to mint playback URLs.
resource "random_id" "cdn_signing_key" {
  byte_length = 16
}

resource "google_compute_backend_bucket_signed_url_key" "hls" {
  project        = var.project_id
  name           = "${local.prefix}-hls-key"
  backend_bucket = google_compute_backend_bucket.hls.name
  key_value      = random_id.cdn_signing_key.b64_url

  lifecycle {
    create_before_destroy = true
  }
}

resource "google_secret_manager_secret" "cdn_signing_key" {
  project   = var.project_id
  secret_id = "${local.prefix}-cdn-signing-key"

  replication {
    auto {}
  }

  depends_on = [google_project_service.services]
}

resource "google_secret_manager_secret_version" "cdn_signing_key" {
  secret      = google_secret_manager_secret.cdn_signing_key.id
  secret_data = random_id.cdn_signing_key.b64_url
}

resource "google_secret_manager_secret_iam_member" "api_reads_cdn_key" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.cdn_signing_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}

# --- Load balancer ------------------------------------------------------------

resource "google_compute_global_address" "cdn" {
  project = var.project_id
  name    = "${local.prefix}-cdn-ip"

  depends_on = [google_project_service.services]
}

resource "google_compute_url_map" "cdn" {
  project         = var.project_id
  name            = "${local.prefix}-cdn-urlmap"
  default_service = google_compute_backend_bucket.hls.id

  # A freshly created backend bucket can report resourceNotReady for a short
  # window; the first apply hit exactly that. The wait absorbs it.
  depends_on = [time_sleep.cdn_fill_agent]
}

# HTTPS is not optional in practice: the editor is served over HTTPS, so a
# plain-HTTP HLS URL would be blocked as mixed content. A managed certificate
# needs a domain, so when cdn_domain is empty the HTTP listener is created alone
# and playback works only for local development.
resource "google_compute_managed_ssl_certificate" "cdn" {
  count   = var.cdn_domain == "" ? 0 : 1
  project = var.project_id
  name    = "${local.prefix}-cdn-cert"

  managed {
    domains = [var.cdn_domain]
  }
}

resource "google_compute_target_https_proxy" "cdn" {
  count            = var.cdn_domain == "" ? 0 : 1
  project          = var.project_id
  name             = "${local.prefix}-cdn-https-proxy"
  url_map          = google_compute_url_map.cdn.id
  ssl_certificates = [google_compute_managed_ssl_certificate.cdn[0].id]
}

resource "google_compute_global_forwarding_rule" "cdn_https" {
  count                 = var.cdn_domain == "" ? 0 : 1
  project               = var.project_id
  name                  = "${local.prefix}-cdn-https"
  target                = google_compute_target_https_proxy.cdn[0].id
  ip_address            = google_compute_global_address.cdn.id
  port_range            = "443"
  load_balancing_scheme = "EXTERNAL_MANAGED"
}

resource "google_compute_target_http_proxy" "cdn" {
  project = var.project_id
  name    = "${local.prefix}-cdn-http-proxy"
  url_map = google_compute_url_map.cdn.id
}

resource "google_compute_global_forwarding_rule" "cdn_http" {
  project               = var.project_id
  name                  = "${local.prefix}-cdn-http"
  target                = google_compute_target_http_proxy.cdn.id
  ip_address            = google_compute_global_address.cdn.id
  port_range            = "80"
  load_balancing_scheme = "EXTERNAL_MANAGED"
}

locals {
  cdn_base_url = var.cdn_domain != "" ? "https://${var.cdn_domain}" : "http://${google_compute_global_address.cdn.address}"
}
