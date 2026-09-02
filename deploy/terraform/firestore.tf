resource "google_firestore_database" "default" {
  project                           = var.project_id
  name                              = "(default)"
  location_id                       = var.firestore_location
  type                              = "FIRESTORE_NATIVE"
  concurrency_mode                  = "OPTIMISTIC"
  app_engine_integration_mode       = "DISABLED"
  point_in_time_recovery_enablement = var.environment == "prod" ? "POINT_IN_TIME_RECOVERY_ENABLED" : "POINT_IN_TIME_RECOVERY_DISABLED"
  delete_protection_state           = var.environment == "prod" ? "DELETE_PROTECTION_ENABLED" : "DELETE_PROTECTION_DISABLED"

  depends_on = [google_project_service.services]
}

# --- Vector (KNN) indexes -----------------------------------------------------
# find_nearest over moment embeddings. COSINE matches the normalised vectors the
# embedding model returns. Dimension must equal var.embedding_dimensions exactly
# or the query fails at runtime rather than at index build time.
resource "google_firestore_index" "moments_knn" {
  project     = var.project_id
  database    = google_firestore_database.default.name
  collection  = "moments"
  query_scope = "COLLECTION_GROUP"

  fields {
    field_path = "ownerUid"
    order      = "ASCENDING"
  }

  fields {
    field_path = "embedding"
    vector_config {
      dimension = var.embedding_dimensions
      flat {}
    }
  }

  # Firestore appends __name__ to the index it actually creates, so the remote
  # object is [ownerUid, __name__, embedding] while this config declares
  # [ownerUid, embedding]. The provider reads that as a field change, which
  # forces replacement — and the replacement's create fails with 409 because
  # the equivalent index already exists, so every subsequent apply retries the
  # same doomed replace. The definition below is what created the index; it is
  # the normalisation that differs, not the intent.
  #
  # Change the vector definition by deleting the index and re-applying, not by
  # editing in place.
  lifecycle {
    ignore_changes = [fields]
  }
}

# Games are indexed separately from the moments inside them. "Find the Sweden
# Denmark match" and "find the double save" are different questions over
# different units, and one index holding both would answer each with the other:
# a match summary and its moments share most of their vocabulary.
resource "google_firestore_index" "games_knn" {
  project     = var.project_id
  database    = google_firestore_database.default.name
  collection  = "games"
  query_scope = "COLLECTION"

  fields {
    field_path = "ownerUid"
    order      = "ASCENDING"
  }

  fields {
    field_path = "embedding"
    vector_config {
      dimension = var.embedding_dimensions
      flat {}
    }
  }

  # Same normalisation trap as moments_knn above: Firestore appends __name__ to
  # what it creates, the provider reads that as a change, and the forced
  # replacement fails 409 for ever after.
  lifecycle {
    ignore_changes = [fields]
  }
}

# "More clips like this" across a user's whole library.
resource "google_firestore_index" "clips_knn" {
  project     = var.project_id
  database    = google_firestore_database.default.name
  collection  = "clips"
  query_scope = "COLLECTION_GROUP"

  fields {
    field_path = "ownerUid"
    order      = "ASCENDING"
  }

  fields {
    field_path = "embedding"
    vector_config {
      dimension = var.embedding_dimensions
      flat {}
    }
  }

  # Firestore appends __name__ to the index it actually creates, so the remote
  # object is [ownerUid, __name__, embedding] while this config declares
  # [ownerUid, embedding]. The provider reads that as a field change, which
  # forces replacement — and the replacement's create fails with 409 because
  # the equivalent index already exists, so every subsequent apply retries the
  # same doomed replace. The definition below is what created the index; it is
  # the normalisation that differs, not the intent.
  #
  # Change the vector definition by deleting the index and re-applying, not by
  # editing in place.
  lifecycle {
    ignore_changes = [fields]
  }
}

# --- Composite indexes for the UI's realtime queries --------------------------
resource "google_firestore_index" "jobs_by_owner_recent" {
  project    = var.project_id
  database   = google_firestore_database.default.name
  collection = "jobs"

  fields {
    field_path = "ownerUid"
    order      = "ASCENDING"
  }

  fields {
    field_path = "createdAt"
    order      = "DESCENDING"
  }
}

resource "google_firestore_index" "moments_by_score" {
  project     = var.project_id
  database    = google_firestore_database.default.name
  collection  = "moments"
  query_scope = "COLLECTION_GROUP"

  fields {
    field_path = "jobId"
    order      = "ASCENDING"
  }

  fields {
    field_path = "highlightScore"
    order      = "DESCENDING"
  }
}

resource "google_firestore_index" "clips_by_score" {
  project     = var.project_id
  database    = google_firestore_database.default.name
  collection  = "clips"
  query_scope = "COLLECTION_GROUP"

  fields {
    field_path = "jobId"
    order      = "ASCENDING"
  }

  fields {
    field_path = "score"
    order      = "DESCENDING"
  }
}

resource "google_firestore_index" "events_by_ts" {
  project     = var.project_id
  database    = google_firestore_database.default.name
  collection  = "events"
  query_scope = "COLLECTION_GROUP"

  fields {
    field_path = "jobId"
    order      = "ASCENDING"
  }

  fields {
    field_path = "ts"
    order      = "ASCENDING"
  }
}

# Security rules are the only thing standing between a signed-in browser and
# another tenant's video, since the SPA talks to Firestore directly.
resource "google_firebaserules_ruleset" "firestore" {
  provider = google-beta
  project  = var.project_id

  source {
    files {
      name    = "firestore.rules"
      content = file("${path.module}/../../firestore.rules")
    }
  }

  depends_on = [google_firestore_database.default]
}

resource "google_firebaserules_release" "firestore" {
  provider     = google-beta
  project      = var.project_id
  name         = "cloud.firestore"
  ruleset_name = google_firebaserules_ruleset.firestore.name

  lifecycle {
    replace_triggered_by = [google_firebaserules_ruleset.firestore]
  }
}
