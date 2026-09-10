# Batch work that is the wrong shape for a request-driven service.
#
# A Cloud Run *service* request has a one-hour ceiling and its filesystem is
# memory. Two things here need neither: the HLS download, which streams a whole
# VOD playlist into one object and then transcodes a 1 fps proxy of it, and the
# live capture, which follows a live playlist for as long as the event lasts.
# Both run as Cloud Run *Jobs* — run-to-completion, deadline of a day — started
# by the media service with per-execution environment overrides and polled by
# it, the same way it treats a Transcoder job.

# --- HLS → MP4/TS download (jobs/hls2mp4) ------------------------------------
resource "google_cloud_run_v2_job" "hls2mp4" {
  project             = var.project_id
  name                = "${local.prefix}-hls2mp4"
  location            = var.region
  deletion_protection = var.environment == "prod"
  labels              = local.common_labels

  template {
    task_count = 1

    template {
      service_account = google_service_account.mcp_media.email
      # The download is I/O-bound and quick; the 1 fps proxy is a decode of
      # the whole recording and is what takes the time on a long event.
      timeout     = "${var.hls_download_timeout_seconds}s"
      max_retries = 0

      containers {
        image = "${local.image_base}/hls2mp4:${var.image_tag}"

        resources {
          limits = {
            cpu    = "2"
            memory = "2Gi"
          }
        }

        env {
          name  = "MAX_PARALLEL"
          value = "8"
        }
        # Everything else — source URL, destinations, whether to make the
        # proxy — is set per execution by mcp/media_server/server.py.
      }
    }
  }

  depends_on = [google_project_service.services]
}

# --- Live capture (media_server.live_capture, same image as mcp-media) --------
resource "google_cloud_run_v2_job" "live_capture" {
  project             = var.project_id
  name                = "${local.prefix}-live-capture"
  location            = var.region
  deletion_protection = var.environment == "prod"
  labels              = local.common_labels

  template {
    task_count = 1

    template {
      service_account = google_service_account.mcp_media.email
      timeout         = "${var.live_capture_timeout_seconds}s"
      max_retries     = 0

      containers {
        image   = "${local.image_base}/mcp-media:${var.image_tag}"
        command = ["python", "-m", "media_server.live_capture"]

        resources {
          limits = {
            cpu    = "1"
            memory = "1Gi"
          }
        }

        env {
          name  = "GOOGLE_CLOUD_PROJECT"
          value = var.project_id
        }
        env {
          name  = "MEDIA_BUCKET"
          value = google_storage_bucket.media.name
        }
        env {
          name  = "LIVE_CHUNK_SECONDS"
          value = tostring(var.live_chunk_seconds)
        }
      }
    }
  }

  depends_on = [google_project_service.services]
}

# The media service starts these and reads their executions. run.developer on
# the job resource rather than on the project: it is the narrowest role that
# carries run.jobs.runWithOverrides, and it reaches nothing else.
resource "google_cloud_run_v2_job_iam_member" "media_runs_hls2mp4" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.hls2mp4.name
  role     = "roles/run.developer"
  member   = "serviceAccount:${google_service_account.mcp_media.email}"
}

resource "google_cloud_run_v2_job_iam_member" "media_runs_live_capture" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.live_capture.name
  role     = "roles/run.developer"
  member   = "serviceAccount:${google_service_account.mcp_media.email}"
}

# --- The live tick --------------------------------------------------------------
# Once a minute, Cloud Scheduler asks the API whether any live event needs
# attention: a scheduled one whose start is five minutes away, or a running one
# with captured chunks nobody has analysed. The API hands each due job to the
# agent's live_event_agent. Nothing here is per event — one clock, and each
# tick finds its own work in Firestore — so scheduling an event is a document
# write rather than a resource.
resource "google_service_account" "scheduler" {
  project      = var.project_id
  account_id   = "${local.prefix}-scheduler"
  display_name = "Sportscut live-event scheduler"
}

resource "google_cloud_scheduler_job" "live_tick" {
  project = var.project_id
  # Scheduler serves fewer regions than Cloud Run. The job itself can target
  # any URL, so where it lives is only a question of where it is offered.
  region           = var.scheduler_region
  name             = "${local.prefix}-live-tick"
  description      = "Drives live-event capture and per-chunk analysis"
  schedule         = "* * * * *"
  time_zone        = "Etc/UTC"
  attempt_deadline = "1800s"

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${local.app_url}/api/live/tick"
    body        = base64encode("{}")
    headers = {
      "Content-Type" = "application/json"
    }

    # A Google-signed ID token the API verifies against this exact audience
    # and this service account's email — the only caller that route accepts.
    oidc_token {
      service_account_email = google_service_account.scheduler.email
      audience              = "${local.app_url}/api/live/tick"
    }
  }

  depends_on = [google_project_service.services]
}
