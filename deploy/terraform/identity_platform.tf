# Identity Platform (GCIP) is the identity provider for both the SPA's Firestore
# session and IAP's external-identity mode.
resource "google_identity_platform_config" "default" {
  provider = google-beta.billing_override
  project  = var.project_id

  autodelete_anonymous_users = true

  sign_in {
    allow_duplicate_emails = false

    email {
      enabled           = true
      password_required = true
    }

    anonymous {
      enabled = false
    }
  }

  # The load balancer hostname must be here or the browser SDK refuses to sign
  # in with auth/unauthorized-domain. The Cloud Run URL is kept for direct
  # access during development, though ingress now blocks it in this deployment.
  authorized_domains = distinct(concat(
    [
      "localhost",
      "${var.project_id}.firebaseapp.com",
      local.app_host,
      replace(replace(google_cloud_run_v2_service.web.uri, "https://", ""), "/", ""),
    ],
    var.identity_platform_authorized_domains,
  ))

  depends_on = [google_project_service.services]
}

# Google sign-in for the SPA's Firestore session.
#
# This needs a normal OAuth 2.0 web client, created once under
# APIs & Services > Credentials, with
#   https://<project>.firebaseapp.com/__/auth/handler
# as an authorized redirect URI. It used to be possible to reuse the IAP OAuth
# client here, but the API that created those was shut down in March 2026.
#
# Left unset, the tenant is created with email/password sign-in only and Google
# can be enabled later without touching anything else.
resource "google_identity_platform_default_supported_idp_config" "google" {
  count    = var.google_oauth_client_id == "" ? 0 : 1
  provider = google-beta.billing_override
  project  = var.project_id

  enabled       = true
  idp_id        = "google.com"
  client_id     = var.google_oauth_client_id
  client_secret = var.google_oauth_client_secret

  depends_on = [google_identity_platform_config.default]
}

# Browser API key used by the Firebase JS SDK for Identity Platform sign-in.
# Restricted to the Identity Toolkit API so a leaked key buys nothing else.
resource "google_apikeys_key" "identity_platform" {
  project      = var.project_id
  name         = "${local.prefix}-web-key"
  display_name = "Sprtz AI web (Identity Platform)"

  restrictions {
    api_targets {
      service = "identitytoolkit.googleapis.com"
    }
    api_targets {
      service = "firestore.googleapis.com"
    }
  }

  depends_on = [google_project_service.services]
}
