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
# than re-encodes. That is what lets it run this small — and small is also what
# starts reliably here: on this project, every Cloud Run shape above 1GiB of
# memory per CPU (4Gi/2cpu, 8Gi/2cpu, 8Gi/4cpu) failed to launch its instance
# at all, with zero application output, while 2Gi/2cpu and 4Gi/4cpu start
# immediately. If you raise memory, raise CPU with it and verify the shape
# starts before relying on it.
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
  description = "Lifetime of a Cloud CDN signed URL prefix handed to the player."
  default     = 21600 # 6 hours — long enough to review a full match in one sitting.
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
