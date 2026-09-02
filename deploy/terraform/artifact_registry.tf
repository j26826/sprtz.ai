resource "google_artifact_registry_repository" "containers" {
  project       = var.project_id
  location      = var.region
  repository_id = "${local.prefix}-containers"
  description   = "Sportscut container images."
  format        = "DOCKER"
  labels        = local.common_labels

  docker_config {
    immutable_tags = false
  }

  cleanup_policies {
    id     = "keep-recent-releases"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }

  cleanup_policies {
    id     = "delete-old-untagged"
    action = "DELETE"
    condition {
      tag_state  = "UNTAGGED"
      older_than = "604800s" # 7 days
    }
  }

  depends_on = [google_project_service.services]
}

locals {
  image_base = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.containers.repository_id}"
}
