locals {
  prefix = "${var.app_name}-${var.environment}"

  # Bootstrap resource, created by deploy/scripts/bootstrap.sh rather than by
  # Terraform — see cloudbuild.tf. The name must be derivable without reading
  # state, so it carries no random suffix.
  tf_state_bucket = "${var.project_id}-${var.app_name}-${var.environment}-tfstate"

  # Every API the stack needs. Terraform enables these before anything else, so a
  # brand new project only needs billing linked.
  services = [
    "aiplatform.googleapis.com",
    "apikeys.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "cloudtrace.googleapis.com",
    "compute.googleapis.com",
    "eventarc.googleapis.com",
    "firestore.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "iap.googleapis.com",
    "identitytoolkit.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "pubsub.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "serviceusage.googleapis.com",
    "speech.googleapis.com",
    "storage.googleapis.com",
    "videointelligence.googleapis.com",
  ]

  # The Agent Runtime engine is created by the SDK, not Terraform — see
  # agents/deployment/deploy.py. Terraform publishes the name both sides agree
  # on so neither has to discover a resource id the other invented.
  agent_display_name = "${var.app_name}-${var.environment}-producer"

  web_service_name = var.web_service_name != "" ? var.web_service_name : "${local.prefix}-web"

  # Origins allowed to talk to the buckets from a browser. The app's own origin
  # must be here or the direct-to-GCS upload fails its CORS preflight — the
  # signed URL is valid, the browser simply refuses to send it.
  browser_origins = distinct(concat([local.app_url], var.upload_cors_origins))

  common_labels = {
    app         = var.app_name
    environment = var.environment
    managed-by  = "terraform"
  }
}
