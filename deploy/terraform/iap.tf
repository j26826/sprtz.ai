# IAP fronts the web and api Cloud Run services. Users authenticate against
# Identity Platform; IAP validates and forwards a signed assertion header that
# the API verifies on every request.
#
# There is deliberately no google_iap_brand or google_iap_client here. Those
# resources drive the IAP OAuth Admin APIs, which were deprecated in January
# 2025 and permanently shut down in March 2026 — new projects cannot use them at
# all. Cloud Run's built-in IAP integration (iap_enabled on the service) uses a
# Google-managed OAuth client instead and needs no brand.

# Access list for the editor.
resource "google_iap_web_cloud_run_service_iam_member" "web_accessors" {
  for_each = toset(var.iap_members)

  project                = var.project_id
  location               = var.region
  cloud_run_service_name = google_cloud_run_v2_service.web.name
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = each.value
}

# Access list for the API — the same principals, since the SPA calls it directly
# from the browser with the user's IAP session.
resource "google_iap_web_cloud_run_service_iam_member" "api_accessors" {
  for_each = toset(var.iap_members)

  project                = var.project_id
  location               = var.region
  cloud_run_service_name = google_cloud_run_v2_service.api.name
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = each.value
}

# IAP's service agent must be able to invoke the services it fronts.
resource "google_project_service_identity" "iap" {
  provider = google-beta
  project  = var.project_id
  service  = "iap.googleapis.com"

  depends_on = [google_project_service.services]
}

resource "google_cloud_run_v2_service_iam_member" "iap_invokes_web" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.web.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_project_service_identity.iap.email}"
}

resource "google_cloud_run_v2_service_iam_member" "iap_invokes_api" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_project_service_identity.iap.email}"
}
