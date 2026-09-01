# --- MCP: catalog (Firestore + embeddings) ------------------------------------
resource "google_cloud_run_v2_service" "mcp_catalog" {
  project             = var.project_id
  name                = "${local.prefix}-mcp-catalog"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  deletion_protection = var.environment == "prod"
  labels              = local.common_labels

  template {
    service_account                  = google_service_account.mcp_catalog.email
    max_instance_request_concurrency = 40
    timeout                          = "120s"

    scaling {
      min_instance_count = 0
      max_instance_count = 20
    }

    containers {
      image = "${local.image_base}/mcp-catalog:${var.image_tag}"

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
        cpu_idle = true
      }

      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = var.vertex_region
      }
      env {
        name  = "EMBEDDING_MODEL"
        value = var.embedding_model
      }
      env {
        name  = "EMBEDDING_DIMENSIONS"
        value = tostring(var.embedding_dimensions)
      }
      env {
        name  = "RERANK_MODEL"
        value = var.gemini_model
      }
      env {
        name  = "RERANK_OVERFETCH"
        value = tostring(var.rerank_overfetch)
      }

      startup_probe {
        http_get {
          path = "/healthz"
        }
        initial_delay_seconds = 5
        period_seconds        = 5
        failure_threshold     = 6
      }
    }
  }

  depends_on = [
    google_project_service.services,
    google_firestore_database.default,
  ]
}

# --- MCP: media (ffmpeg) ------------------------------------------------------
resource "google_cloud_run_v2_service" "mcp_media" {
  project             = var.project_id
  name                = "${local.prefix}-mcp-media"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  deletion_protection = var.environment == "prod"
  labels              = local.common_labels

  template {
    service_account = google_service_account.mcp_media.email
    # ffmpeg work is CPU-bound; one heavy job per instance keeps latency sane.
    max_instance_request_concurrency = 4
    timeout                          = "3600s"

    scaling {
      min_instance_count = 0
      max_instance_count = 30
    }

    containers {
      image = "${local.image_base}/mcp-media:${var.image_tag}"

      resources {
        limits = {
          cpu    = var.media_worker_cpu
          memory = var.media_worker_memory
        }
        # ffmpeg must keep the CPU between requests while transcoding.
        cpu_idle          = false
        startup_cpu_boost = true
      }

      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "UPLOADS_BUCKET"
        value = google_storage_bucket.uploads.name
      }
      env {
        name  = "MEDIA_BUCKET"
        value = google_storage_bucket.media.name
      }
      env {
        name  = "HLS_BUCKET"
        value = google_storage_bucket.hls.name
      }
      env {
        name  = "CDN_BASE_URL"
        value = local.cdn_base_url
      }

      startup_probe {
        http_get {
          path = "/healthz"
        }
        initial_delay_seconds = 10
        period_seconds        = 5
        # 1s (the default) is tight for a cold gen2 instance still mounting its
        # GCS volume; a slow first response is not a dead container.
        timeout_seconds   = 5
        failure_threshold = 12
      }
    }

  }

  depends_on = [google_project_service.services]
}

# --- API (behind IAP) ---------------------------------------------------------
resource "google_cloud_run_v2_service" "api" {
  project             = var.project_id
  name                = "${local.prefix}-api"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = var.environment == "prod"
  labels              = local.common_labels

  # Cloud Run's built-in IAP integration. Requests are rejected at the edge
  # before they reach the container.
  iap_enabled = true

  template {
    service_account                  = google_service_account.api.email
    max_instance_request_concurrency = 80
    timeout                          = "600s"

    scaling {
      min_instance_count = var.environment == "prod" ? 1 : 0
      max_instance_count = 20
    }

    containers {
      image = "${local.image_base}/api:${var.image_tag}"

      resources {
        limits = {
          cpu    = "2"
          memory = "2Gi"
        }
        cpu_idle = true
      }

      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT_NUMBER"
        value = data.google_project.this.number
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = var.vertex_region
      }
      env {
        name  = "UPLOADS_BUCKET"
        value = google_storage_bucket.uploads.name
      }
      env {
        name  = "MEDIA_BUCKET"
        value = google_storage_bucket.media.name
      }
      env {
        name  = "SIGNER_SERVICE_ACCOUNT"
        value = google_service_account.api.email
      }
      env {
        name  = "MCP_CATALOG_URL"
        value = google_cloud_run_v2_service.mcp_catalog.uri
      }
      env {
        name  = "MCP_MEDIA_URL"
        value = google_cloud_run_v2_service.mcp_media.uri
      }
      env {
        name  = "AGENT_ENGINE_RESOURCE"
        value = google_vertex_ai_reasoning_engine.producer.name
      }
      env {
        name  = "IAP_AUDIENCE"
        value = "/projects/${data.google_project.this.number}/locations/${var.region}/services/${local.prefix}-api"
      }
      env {
        name  = "IDENTITY_PLATFORM_API_KEY"
        value = google_apikeys_key.identity_platform.key_string
      }
      env {
        name  = "HLS_BUCKET"
        value = google_storage_bucket.hls.name
      }
      env {
        name  = "CDN_BASE_URL"
        value = local.cdn_base_url
      }
      env {
        name  = "CDN_SIGNING_KEY_NAME"
        value = google_compute_backend_bucket_signed_url_key.hls.name
      }
      env {
        name  = "CDN_SIGNED_URL_TTL"
        value = tostring(var.hls_signed_url_ttl_seconds)
      }
      env {
        name = "CDN_SIGNING_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.cdn_signing_key.secret_id
            version = "latest"
          }
        }
      }
      env {
        name  = "ENVIRONMENT"
        value = var.environment
      }

      startup_probe {
        http_get {
          path = "/healthz"
        }
        initial_delay_seconds = 5
        period_seconds        = 5
        failure_threshold     = 12
      }
    }
  }

  depends_on = [google_project_service.services]
}

# --- Web SPA (behind IAP) -----------------------------------------------------
resource "google_cloud_run_v2_service" "web" {
  project             = var.project_id
  name                = "${local.prefix}-web"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = var.environment == "prod"
  labels              = local.common_labels

  iap_enabled = true

  template {
    service_account                  = google_service_account.web.email
    max_instance_request_concurrency = 200

    scaling {
      min_instance_count = var.environment == "prod" ? 1 : 0
      max_instance_count = 10
    }

    containers {
      image = "${local.image_base}/web:${var.image_tag}"

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle = true
      }

      # Runtime config is injected into /config.js by the container entrypoint so
      # the same image can be promoted between environments unchanged.
      env {
        name  = "API_BASE_URL"
        value = google_cloud_run_v2_service.api.uri
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "FIREBASE_API_KEY"
        value = google_apikeys_key.identity_platform.key_string
      }
      env {
        name  = "FIREBASE_AUTH_DOMAIN"
        value = "${var.project_id}.firebaseapp.com"
      }
      env {
        name  = "ENVIRONMENT"
        value = var.environment
      }

      startup_probe {
        http_get {
          path = "/healthz"
        }
        initial_delay_seconds = 3
        period_seconds        = 3
        failure_threshold     = 10
      }
    }
  }

  depends_on = [google_project_service.services]
}
