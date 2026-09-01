project_id = "REPLACE_WITH_PROJECT_ID"
region     = "us-south1"

# Vertex AI (Agent Runtime + Gemini) supports fewer regions than Cloud Run.
# Run deploy/scripts/preflight.sh to confirm us-south1 is offered for your
# project; if it is not, set this to the nearest supported region.
vertex_region      = "us-south1"
firestore_location = "us-south1"
environment        = "prod"


# Who may reach the editor. Use domain:example.com to admit a whole workspace.
iap_members = [
  "user:REPLACE_WITH_YOUR_EMAIL",
]

identity_platform_authorized_domains = []

upload_cors_origins = [
  "http://localhost:5173",
]

gemini_model            = "gemini-2.5-flash"
embedding_model         = "gemini-embedding-001"
embedding_dimensions    = 768
segment_minutes         = 15
segment_overlap_seconds = 20

agent_min_instances = 2
agent_max_instances = 20

create_build_trigger = true

# Point this domain's A record at the cdn_ip output, then re-apply so the
# managed certificate can provision. Empty means HTTP-only on a bare IP.
cdn_domain = ""

# Google sign-in. Create an OAuth 2.0 web client under APIs & Services >
# Credentials with https://<project>.firebaseapp.com/__/auth/handler as an
# authorized redirect URI. Leave empty for email/password sign-in only.
google_oauth_client_id     = ""
google_oauth_client_secret = ""
