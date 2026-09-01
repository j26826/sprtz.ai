#!/bin/sh
# Write the runtime config Terraform injected as environment variables, so the
# same image can be promoted between environments without a rebuild.
set -eu

cat > /usr/share/nginx/html/config.js <<CONFIG
window.SPRTZ_CONFIG = {
  apiBaseUrl:         "${API_BASE_URL:-}",   // empty = same-origin via the load balancer
  projectId:          "${GOOGLE_CLOUD_PROJECT:-}",
  firebaseApiKey:     "${FIREBASE_API_KEY:-}",
  firebaseAuthDomain: "${FIREBASE_AUTH_DOMAIN:-}",
  environment:        "${ENVIRONMENT:-dev}"
};
CONFIG

exec nginx -g 'daemon off;'
