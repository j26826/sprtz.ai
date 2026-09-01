# One external load balancer fronting both Cloud Run services on a single
# hostname: "/" serves the editor and "/api/*" the API. That makes them
# same-origin, which removes CORS entirely and lets the browser send its
# Identity Platform token on a plain relative fetch.
#
# The Cloud Run services are reachable *only* through this load balancer:
# ingress is restricted to internal-and-cloud-load-balancing, so the allUsers
# invoker binding below cannot be used to reach them directly.

resource "google_compute_region_network_endpoint_group" "web" {
  project               = var.project_id
  name                  = "${local.prefix}-web-neg"
  region                = var.region
  network_endpoint_type = "SERVERLESS"

  cloud_run {
    service = google_cloud_run_v2_service.web.name
  }
}

resource "google_compute_region_network_endpoint_group" "api" {
  project               = var.project_id
  name                  = "${local.prefix}-api-neg"
  region                = var.region
  network_endpoint_type = "SERVERLESS"

  cloud_run {
    service = google_cloud_run_v2_service.api.name
  }
}

resource "google_compute_backend_service" "web" {
  project               = var.project_id
  name                  = "${local.prefix}-web-backend"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  protocol              = "HTTP"

  backend {
    group = google_compute_region_network_endpoint_group.web.id
  }
}

resource "google_compute_backend_service" "api" {
  project               = var.project_id
  name                  = "${local.prefix}-api-backend"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  protocol              = "HTTP"
  # Long enough for the agent's SSE stream, which stays open while an analysis
  # runs. The default 30s would cut every conversation short.
  timeout_sec = 3600

  backend {
    group = google_compute_region_network_endpoint_group.api.id
  }
}

resource "google_compute_global_address" "app" {
  project = var.project_id
  name    = "${local.prefix}-app-ip"
}

# A managed certificate needs a resolvable name. With no custom domain,
# nip.io resolves <ip>.nip.io straight back to the address above, which is
# enough for Google to issue a real, browser-trusted certificate.
locals {
  app_host = var.app_domain != "" ? var.app_domain : "${google_compute_global_address.app.address}.nip.io"
  app_url  = "https://${local.app_host}"
}

resource "google_compute_managed_ssl_certificate" "app" {
  project = var.project_id
  name    = "${local.prefix}-app-cert"

  managed {
    domains = [local.app_host]
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "google_compute_url_map" "app" {
  project         = var.project_id
  name            = "${local.prefix}-app-map"
  default_service = google_compute_backend_service.web.id

  host_rule {
    hosts        = ["*"]
    path_matcher = "main"
  }

  path_matcher {
    name            = "main"
    default_service = google_compute_backend_service.web.id

    path_rule {
      paths   = ["/api/*"]
      service = google_compute_backend_service.api.id
    }
  }
}

resource "google_compute_target_https_proxy" "app" {
  project          = var.project_id
  name             = "${local.prefix}-app-https-proxy"
  url_map          = google_compute_url_map.app.id
  ssl_certificates = [google_compute_managed_ssl_certificate.app.id]
}

resource "google_compute_global_forwarding_rule" "app_https" {
  project               = var.project_id
  name                  = "${local.prefix}-app-https"
  target                = google_compute_target_https_proxy.app.id
  ip_address            = google_compute_global_address.app.id
  port_range            = "443"
  load_balancing_scheme = "EXTERNAL_MANAGED"
}

# Redirects http to https so the certificate is always used.
resource "google_compute_url_map" "app_redirect" {
  project = var.project_id
  name    = "${local.prefix}-app-redirect"

  default_url_redirect {
    https_redirect         = true
    redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
    strip_query            = false
  }
}

resource "google_compute_target_http_proxy" "app_redirect" {
  project = var.project_id
  name    = "${local.prefix}-app-http-proxy"
  url_map = google_compute_url_map.app_redirect.id
}

resource "google_compute_global_forwarding_rule" "app_http" {
  project               = var.project_id
  name                  = "${local.prefix}-app-http"
  target                = google_compute_target_http_proxy.app_redirect.id
  ip_address            = google_compute_global_address.app.id
  port_range            = "80"
  load_balancing_scheme = "EXTERNAL_MANAGED"
}
