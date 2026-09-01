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

  common_labels = {
    app         = var.app_name
    environment = var.environment
    managed-by  = "terraform"
  }
}
