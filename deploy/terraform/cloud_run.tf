# --- MCP: catalog (Firestore + embeddings) ------------------------------------
resource "google_cloud_run_v2_service" "mcp_catalog" {
  project  = var.project_id
  name     = "${local.prefix}-mcp-catalog"
  location = var.region
  # Not INTERNAL_ONLY: Cloud Run services calling each other without a VPC
  # connector egress over the public internet, so internal-only rejects the very
  # callers this exists for — the API and the agent both got 404s from it. The
  # service stays private through IAM instead: only the agent and API service
  # accounts hold run.invoker, and every call carries an OIDC identity token, so
  # an unauthenticated request is refused with 403.
  ingress             = "INGRESS_TRAFFIC_ALL"
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
        cpu_idle          = true
        startup_cpu_boost = true
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
        # ~5 minutes. Importing fastmcp and the Google client libraries costs
        # tens of seconds on Cloud Run's startup CPU — measured at ~100s on a
        # live revision against 6s on a developer machine. A tight window here
        # kills the container mid-import, and because its stdout is still
        # buffered the logs show nothing at all, which reads as a container that
        # never ran rather than one that was not given time.
        initial_delay_seconds = 10
        period_seconds        = 10
        timeout_seconds       = 5
        failure_threshold     = 30
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
  project  = var.project_id
  name     = "${local.prefix}-mcp-media"
  location = var.region
  # Not INTERNAL_ONLY: Cloud Run services calling each other without a VPC
  # connector egress over the public internet, so internal-only rejects the very
  # callers this exists for — the API and the agent both got 404s from it. The
  # service stays private through IAM instead: only the agent and API service
  # accounts hold run.invoker, and every call carries an OIDC identity token, so
  # an unauthenticated request is refused with 403.
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = var.environment == "prod"
  labels              = local.common_labels

  template {
    service_account = google_service_account.mcp_media.email
    # One heavy job per instance. Packaging a match now runs on Transcoder API
    # and no video passes through here, so this no longer guards against the
    # OOM that set it — but probes, clip cuts and reframes are still ffmpeg,
    # and their working set is a whole container's business.
    max_instance_request_concurrency = 1
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
      env {
        # Transcoder runs where the buckets and this service are, not where
        # Vertex is: it reads the source and writes the package itself, and
        # crossing regions to do that is egress for no benefit.
        name  = "TRANSCODER_LOCATION"
        value = var.region
      }

      startup_probe {
        http_get {
          path = "/healthz"
        }
        # ~5 minutes. Importing fastmcp and the Google client libraries costs
        # tens of seconds on Cloud Run's startup CPU — measured at ~100s on a
        # live revision against 6s on a developer machine. A tight window here
        # kills the container mid-import, and because its stdout is still
        # buffered the logs show nothing at all, which reads as a container that
        # never ran rather than one that was not given time.
        initial_delay_seconds = 10
        period_seconds        = 10
        timeout_seconds       = 5
        failure_threshold     = 30
      }
    }

  }

  depends_on = [google_project_service.services]
}

# --- API (behind IAP) ---------------------------------------------------------
resource "google_cloud_run_v2_service" "api" {
  project  = var.project_id
  name     = "${local.prefix}-api"
  location = var.region
  # Only the load balancer may reach this; the run.app URL is a dead end.
  ingress             = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
  deletion_protection = var.environment == "prod"
  labels              = local.common_labels

  template {
    service_account                  = google_service_account.api.email
    max_instance_request_concurrency = 80
    # The agent's SSE stream stays open for the whole of an analysis, which on a
    # three-hour match runs well past ten minutes. This is the deadline that
    # governs it — a backend service fronting a serverless NEG cannot set one.
    timeout = "3600s"

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
        cpu_idle          = true
        startup_cpu_boost = true
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
        name  = "AGENT_ENGINE_DISPLAY_NAME"
        value = local.agent_display_name
      }
      # Empty because IAP is not in front of this service. It cannot be derived
      # here either: the value would be the LB backend service's id, and that
      # backend points at this service through a NEG, so referencing it creates
      # a dependency cycle.
      #
      # If IAP is ever reinstated, set this from the backend service id — the
      # audience is "/projects/<num>/global/backendServices/<id>", not the Cloud
      # Run form, which would silently 401 every request IAP had already
      # admitted. auth.py ignores an IAP assertion when this is unset.
      env {
        name  = "IAP_AUDIENCE"
        value = ""
      }
      env {
        name  = "IDENTITY_PLATFORM_API_KEY"
        value = google_apikeys_key.identity_platform.key_string
      }
      env {
        name  = "HLS_BUCKET"
        value = google_storage_bucket.hls.name
      }
      # Same hostname as the app, so the playback cookie is same-origin.
      env {
        name  = "CDN_BASE_URL"
        value = local.app_url
      }
      env {
        name  = "CDN_SIGNING_KEY_NAME"
        value = google_compute_backend_bucket_signed_url_key.hls.name
      }
      env {
        name  = "CDN_SIGNED_URL_TTL"
        value = tostring(var.hls_signed_url_ttl_seconds)
      }
      # The CDN is served from the app's own hostname, so the cookie needs no
      # parent-domain trick — and none was possible on *.run.app, which is on
      # the Public Suffix List.
      env {
        # Empty by default, which makes the playback cookie host-only. The load
        # balancer serves the CDN from the app's own hostname, so a Domain
        # attribute widens the cookie for no benefit — and a Domain a browser
        # declines is the whole cookie lost, which surfaces as an opaque 403
        # inside the player rather than as anything mentioning cookies. Set the
        # variable only for a deployment that genuinely serves the CDN from a
        # sibling host.
        name  = "CDN_COOKIE_DOMAIN"
        value = var.cdn_cookie_domain
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
      # Only advertise a federated provider the tenant actually has configured,
      # so the editor never shows a sign-in button that can only fail with
      # auth/operation-not-allowed.
      env {
        name  = "FEDERATED_PROVIDERS"
        value = var.google_oauth_client_id != "" ? "google.com" : ""
      }
      # The sports the upload panel offers. The agent's registry is the real
      # source of truth and this service cannot import it, so the list is set
      # here — where a deployment's configuration lives — rather than being a
      # second copy in the code. A test in the agents suite fails if the API's
      # default and the registry ever disagree.
      env {
        name  = "SUPPORTED_SPORTS"
        value = join(",", var.supported_sports)
      }

      startup_probe {
        http_get {
          path = "/healthz"
        }
        # ~5 minutes. Importing fastmcp and the Google client libraries costs
        # tens of seconds on Cloud Run's startup CPU — measured at ~100s on a
        # live revision against 6s on a developer machine. A tight window here
        # kills the container mid-import, and because its stdout is still
        # buffered the logs show nothing at all, which reads as a container that
        # never ran rather than one that was not given time.
        initial_delay_seconds = 10
        period_seconds        = 10
        timeout_seconds       = 5
        failure_threshold     = 30
      }
    }
  }

  depends_on = [google_project_service.services]
}

# --- Web SPA (behind IAP) -----------------------------------------------------
resource "google_cloud_run_v2_service" "web" {
  project  = var.project_id
  name     = local.web_service_name
  location = var.region
  # Only the load balancer may reach these; the run.app URLs are dead ends.
  ingress             = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
  deletion_protection = var.environment == "prod"
  labels              = local.common_labels

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
        cpu_idle          = true
        startup_cpu_boost = true
      }

      # Runtime config is injected into /config.js by the container entrypoint so
      # the same image can be promoted between environments unchanged.
      # Empty on purpose: the load balancer serves the SPA and the API on one
      # hostname, so the browser calls /api/* same-origin. Pointing this at the
      # API's own run.app URL would reintroduce CORS and, since ingress is
      # restricted to the load balancer, would not be reachable anyway.
      env {
        name  = "API_BASE_URL"
        value = ""
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
        # ~5 minutes. Importing fastmcp and the Google client libraries costs
        # tens of seconds on Cloud Run's startup CPU — measured at ~100s on a
        # live revision against 6s on a developer machine. A tight window here
        # kills the container mid-import, and because its stdout is still
        # buffered the logs show nothing at all, which reads as a container that
        # never ran rather than one that was not given time.
        initial_delay_seconds = 10
        period_seconds        = 10
        timeout_seconds       = 5
        failure_threshold     = 30
      }
    }
  }

  depends_on = [google_project_service.services]
}
