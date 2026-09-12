# Publishing a moment to YouTube.
#
# Two of the three things this needs are automated here: the API is enabled
# (see locals.tf) and the client secret is kept in Secret Manager and handed to
# the two services that use it. The third is not automatable at all — Google
# has no API that creates an OAuth client, and the IAP OAuth Admin APIs that
# once did were shut down in March 2026 — so `youtube_oauth_client_id` and its
# secret are made once in the console and passed in as variables. The
# `youtube_redirect_uri` output is what to register on that client.
#
# The channel is not configuration: it is connected in the editor's settings,
# and the refresh token that comes back lives in Firestore, because it changes
# without a deploy and belongs to whoever granted it.

locals {
  # Whether a client secret was supplied — and deliberately *not* sensitive.
  #
  # `youtube_oauth_client_secret` is declared sensitive, and every value
  # derived from a sensitive one carries the mark with it. A marked value
  # cannot be iterated, so a `dynamic` block fed one fails at apply with
  # "Cannot use a set of string value in for_each. An iterable collection is
  # required" — a message about the type, for a problem that has nothing to do
  # with the type: it says the same thing about a list of numbers. `count` is
  # refused for the same reason. `terraform validate` passes either way, so
  # this only ever surfaces halfway through a deploy.
  #
  # Whether a secret exists is not itself a secret, which is what `nonsensitive`
  # is for. The secret's *value* stays marked and never leaves Secret Manager.
  youtube_configured = nonsensitive(var.youtube_oauth_client_secret != "")
}

resource "google_secret_manager_secret" "youtube_client_secret" {
  count     = local.youtube_configured ? 1 : 0
  project   = var.project_id
  secret_id = "${local.prefix}-youtube-client-secret"

  replication {
    auto {}
  }

  depends_on = [google_project_service.services]
}

resource "google_secret_manager_secret_version" "youtube_client_secret" {
  count       = local.youtube_configured ? 1 : 0
  secret      = google_secret_manager_secret.youtube_client_secret[0].id
  secret_data = var.youtube_oauth_client_secret
}

# The API exchanges the authorisation code for a refresh token; the media
# service exchanges that refresh token for an access token on every upload.
# Both need the client secret, and neither should hold a copy of it.
resource "google_secret_manager_secret_iam_member" "api_reads_youtube_secret" {
  count     = local.youtube_configured ? 1 : 0
  project   = var.project_id
  secret_id = google_secret_manager_secret.youtube_client_secret[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}

resource "google_secret_manager_secret_iam_member" "media_reads_youtube_secret" {
  count     = local.youtube_configured ? 1 : 0
  project   = var.project_id
  secret_id = google_secret_manager_secret.youtube_client_secret[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.mcp_media.email}"
}

locals {
  # Where Google sends the editor back to after they approve the channel. It
  # must match what is registered on the OAuth client character for character,
  # so it is built once here and both used and printed from the same place.
  youtube_redirect_uri = "${local.app_url}/api/integrations/youtube/callback"
}

output "youtube_redirect_uri" {
  description = "Register this on the OAuth client as an authorised redirect URI."
  value       = local.youtube_redirect_uri
}
