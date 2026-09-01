# Agent Runtime needs a source archive at create time. Cloud Build replaces it
# with the real package on every deploy, so Terraform seeds a minimal valid
# archive once and then ignores source changes forever.
resource "google_vertex_ai_reasoning_engine" "producer" {
  provider = google-beta

  project      = var.project_id
  region       = var.vertex_region
  display_name = "${local.prefix}-producer"
  description  = "Sprtz AI sports video analysis agent (ADK)."

  spec {
    agent_framework = "google-adk"
    service_account = google_service_account.agent.email

    # GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION are deliberately absent:
    # Agent Runtime reserves both and rejects the resource outright if either is
    # supplied. It injects them itself, and sprtz_agents.config reads them from
    # the environment either way.
    deployment_spec {
      min_instances         = var.agent_min_instances
      max_instances         = var.agent_max_instances
      container_concurrency = 9

      resource_limits = {
        cpu    = "4"
        memory = "8Gi"
      }

      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = "True"
      }
      env {
        name  = "SPRTZ_MODEL"
        value = var.gemini_model
      }
      env {
        name  = "SPRTZ_EMBEDDING_MODEL"
        value = var.embedding_model
      }
      env {
        name  = "SPRTZ_EMBEDDING_DIMENSIONS"
        value = tostring(var.embedding_dimensions)
      }
      env {
        name  = "SPRTZ_SEGMENT_MINUTES"
        value = tostring(var.segment_minutes)
      }
      env {
        name  = "SPRTZ_SEGMENT_OVERLAP_SECONDS"
        value = tostring(var.segment_overlap_seconds)
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
        name  = "LOGS_BUCKET_NAME"
        value = google_storage_bucket.agent_logs.name
      }
      env {
        name  = "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY"
        value = "true"
      }
      env {
        name  = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
        value = "true"
      }
    }

    source_code_spec {
      inline_source {
        source_archive = trimspace(file("${path.module}/bootstrap_source.b64"))
      }

      python_spec {
        entrypoint_module = "sprtz_agents.agent_runtime_app"
        entrypoint_object = "agent_runtime"
        requirements_file = "requirements.txt"
        version           = "3.12"
      }
    }
  }

  # Cloud Build owns the source after the first apply.
  lifecycle {
    ignore_changes = [spec[0].source_code_spec]
  }

  depends_on = [
    google_project_service.services,
    google_project_service_identity.vertex,
  ]
}
