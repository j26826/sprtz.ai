variable "project_id" {
  type        = string
  description = "Google Cloud project that hosts every Sprtz AI resource."
}

variable "region" {
  type        = string
  description = "Primary region for Cloud Run, Artifact Registry, GCS and Cloud Build."
  default     = "us-central1"
}

# Agent Runtime and Gemini are served from a narrower set of locations than
# Cloud Run. Keeping this separate means the app can sit in a region Vertex
# does not serve without the whole deploy failing. Leave it equal to var.region
# when the region supports both; deploy/scripts/preflight.sh checks the live
# APIs and tells you which to change.
variable "vertex_region" {
  type        = string
  description = "Region for Vertex AI: Agent Runtime, Gemini and the embedding model. Must be a Vertex AI location."
  default     = "us-central1"
}

variable "firestore_location" {
  type        = string
  description = "Firestore location. A region (us-central1) or a multi-region (nam5). Immutable once the database exists."
  default     = "us-central1"
}

variable "app_name" {
  type        = string
  description = "Short name used as a prefix for every resource."
  default     = "sprtz"
}

variable "environment" {
  type        = string
  description = "Deployment environment (dev, staging, prod)."
  default     = "dev"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "image_tag" {
  type        = string
  description = "Container image tag deployed by Cloud Build. Overridden per build with -var."
  default     = "latest"
}

variable "gemini_model" {
  type        = string
  description = "Gemini model used for video analysis and every agent in the pipeline."
  default     = "gemini-2.5-flash"
}

variable "segment_minutes" {
  type        = number
  description = "Length of each analysis segment. A full match is split into segments, analysed in parallel, then merged."
  default     = 15
}

variable "segment_overlap_seconds" {
  type        = number
  description = "Overlap between adjacent segments so a moment straddling a boundary is seen whole by at least one segment."
  default     = 20
}

variable "embedding_model" {
  type        = string
  description = "Vertex AI embedding model backing the Firestore KNN semantic search."
  default     = "gemini-embedding-001"
}

variable "embedding_dimensions" {
  type        = number
  description = "Embedding width. Must match the Firestore vector index exactly."
  default     = 768
}

variable "iap_members" {
  type        = list(string)
  description = "Principals allowed through IAP, e.g. [\"user:a@example.com\", \"domain:example.com\"]."
  default     = []
}

variable "identity_platform_authorized_domains" {
  type        = list(string)
  description = "Domains permitted to complete an Identity Platform sign-in redirect."
  default     = ["localhost"]
}

variable "upload_cors_origins" {
  type        = list(string)
  description = "Origins allowed to PUT directly to the uploads bucket via a signed URL."
  default     = ["http://localhost:5173"]
}

variable "github_owner" {
  type        = string
  description = "GitHub owner for the Cloud Build trigger."
  default     = "j26826"
}

variable "github_repo" {
  type        = string
  description = "GitHub repository name for the Cloud Build trigger."
  default     = "sprtz.ai"
}

variable "create_build_trigger" {
  type        = bool
  description = "Create the Cloud Build push trigger. Requires the repository to be connected to Cloud Build first."
  default     = false
}

variable "agent_min_instances" {
  type        = number
  description = "Minimum Agent Runtime instances. 1 keeps the first analysis warm."
  default     = 1
}

variable "agent_max_instances" {
  type        = number
  description = "Maximum Agent Runtime instances."
  default     = 10
}

# The worker streams sources over HTTPS and drains HLS segments to GCS as they
# are written, so it holds only a few segments locally and copy-remuxes rather
# than re-encodes. That is what lets it run this small.
#
# An earlier revision of this comment blamed a memory-to-CPU ratio for the
# service failing to start. That was wrong: the cause was the startup probe
# window expiring during Python's import of fastmcp and the Google client
# libraries, which takes ~100s on Cloud Run. The shapes that appeared to "work"
# were the ones that happened to import fast enough.
variable "media_worker_cpu" {
  type        = string
  description = "CPU allocation for the ffmpeg-backed MCP server."
  default     = "2"
}

variable "media_worker_memory" {
  type        = string
  description = "Memory allocation for the ffmpeg-backed MCP server. Keep at or below 1GiB per CPU."
  default     = "2Gi"
}

variable "video_retention_days" {
  type        = number
  description = "Days before raw uploads are deleted from the uploads bucket."
  default     = 30
}

variable "cdn_domain" {
  type        = string
  description = <<-EOT
    Domain serving HLS through Cloud CDN, e.g. cdn.sprtz.ai. Leave empty to get an
    HTTP-only load balancer on a bare IP, which is fine locally but will be blocked
    as mixed content by the HTTPS editor. Point the domain's A record at the
    cdn_ip output before the managed certificate can provision.
  EOT
  default     = ""
}

variable "hls_signed_url_ttl_seconds" {
  type        = number
  description = "Lifetime of the Cloud CDN signed cookie handed to the player."
  default     = 21600 # 6 hours — long enough to review a full match in one sitting.
}

variable "cdn_cookie_domain" {
  type        = string
  description = <<-EOT
    Domain the Cloud-CDN-Cookie is set on, e.g. ".sprtz.ai". A browser only sends
    the cookie to the CDN if the CDN host falls under it, so the API and the CDN
    must share a registrable domain (api.sprtz.ai + cdn.sprtz.ai). Leave empty on
    the default *.run.app hostnames: run.app is on the Public Suffix List, so no
    cookie can span two services there and playback will not authorise.
  EOT
  default     = ""
}

variable "rerank_overfetch" {
  type        = number
  description = "Vector-search candidates fetched per result returned, before Gemini reranks them. The reranker can only reorder what retrieval found, so this is what buys the quality."
  default     = 4
}

variable "google_oauth_client_id" {
  type        = string
  description = <<-EOT
    OAuth 2.0 web client ID for Google sign-in in the editor. Create it under
    APIs & Services > Credentials with https://<project>.firebaseapp.com/__/auth/handler
    as an authorized redirect URI. Leave empty to ship email/password sign-in only.
  EOT
  default     = ""
}

variable "google_oauth_client_secret" {
  type        = string
  description = "Secret for google_oauth_client_id."
  default     = ""
  sensitive   = true
}

variable "web_service_name" {
  type        = string
  description = <<-EOT
    Cloud Run service name for the editor. Empty falls back to "<app>-<env>-web".

    Overriding this is the only reliable way to shed stale IAP settings: IAP
    stores accessSettings against the service *name*, and they survive deleting
    and recreating a service under the same name — a service recreated as
    sprtz-dev-web inherited a gcipSettings block pointing at a hosted sign-in UI
    that no longer existed, and no API call could clear it. A new name starts
    clean. Note this is not environment-prefixed, so set it per environment if
    two share a project.
  EOT
  default     = ""
}

variable "app_domain" {
  type        = string
  description = <<-EOT
    Hostname serving the editor and API. Empty derives <lb-ip>.nip.io, which
    resolves back to the load balancer and lets Google issue a managed
    certificate without owning a domain.
  EOT
  default     = ""
}
