# Authentication is enforced by the application, not by IAP.
#
# IAP could not be made to work in this project. Its authorization step ran with
# an empty principal — `authenticationInfo: {}` in the audit log — on both the
# Cloud Run built-in integration and a load-balancer backend service, so no IAM
# binding could match and even allAuthenticatedUsers was refused. The project's
# legacy OAuth brand has zero clients, and the API that could create one was
# shut down in March 2026.
#
# The SPA therefore signs in with Identity Platform and the API verifies that
# token (see api/app/core/auth.py). The uid in a Firebase token is also the one
# Firestore rules compare against, so ownership written by the API is exactly
# what the browser can read back — which an IAP assertion never matched.
#
# Reach is controlled by ingress instead: both services accept traffic only from
# the load balancer, so these allUsers bindings cannot be used to call them
# directly. Nothing sensitive is served unauthenticated — the SPA is static
# files and a Firebase browser API key, which is designed to be public.

resource "google_cloud_run_v2_service_iam_member" "lb_invokes_web" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.web.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_service_iam_member" "lb_invokes_api" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
