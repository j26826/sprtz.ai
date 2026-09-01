#!/usr/bin/env bash
# Verify the target regions can actually host every service before Terraform
# starts creating resources. Vertex AI (Agent Runtime, Gemini) is offered in
# fewer regions than Cloud Run, and Firestore's location is immutable once the
# database exists — both are far cheaper to catch here than mid-apply.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
VERTEX_REGION="${VERTEX_REGION:-$REGION}"
FIRESTORE_LOCATION="${FIRESTORE_LOCATION:-$REGION}"

if [[ -z "$PROJECT_ID" ]]; then
  echo "PROJECT_ID is not set and gcloud has no default project." >&2
  exit 2
fi

echo "Project           : $PROJECT_ID"
echo "Region            : $REGION"
echo "Vertex region     : $VERTEX_REGION"
echo "Firestore location: $FIRESTORE_LOCATION"
echo

fail=0

note_fail() {
  echo "  ✗ $1" >&2
  fail=1
}

echo "Enabling the APIs needed to run these checks..."
gcloud services enable aiplatform.googleapis.com run.googleapis.com \
  firestore.googleapis.com --project "$PROJECT_ID" --quiet

# Enabling returns before the API is queryable, and a list issued too early
# comes back empty — which is what made this script fail a perfectly good
# region once.
for _ in 1 2 3 4 5 6; do
  if gcloud firestore locations list --project "$PROJECT_ID" \
       --format='value(locationId)' 2>/dev/null | grep -q .; then
    break
  fi
  echo "  waiting for the Firestore API to become queryable..."
  sleep 10
done

# --- Vertex AI ----------------------------------------------------------------
echo "Checking Vertex AI availability in $VERTEX_REGION..."
vertex_locations="$(
  curl -sS -H "Authorization: Bearer $(gcloud auth print-access-token)" \
    "https://aiplatform.googleapis.com/v1/projects/${PROJECT_ID}/locations" |
    python3 -c 'import sys,json; print("\n".join(l.get("locationId","") for l in json.load(sys.stdin).get("locations",[])))'
)"

if [[ -z "$vertex_locations" ]]; then
  note_fail "Could not list Vertex AI locations. Check permissions and retry."
elif ! grep -qx "$VERTEX_REGION" <<<"$vertex_locations"; then
  note_fail "Vertex AI is not offered in '$VERTEX_REGION'."
  echo "    Set vertex_region in deploy/terraform/envs/*.tfvars to one of:" >&2
  grep '^us' <<<"$vertex_locations" | sed 's/^/      /' >&2
else
  echo "  ✓ Vertex AI available in $VERTEX_REGION"
fi

# Agent Runtime is a subset of Vertex AI's regions, so probe the endpoint itself.
echo "Checking Agent Runtime (reasoningEngines) in $VERTEX_REGION..."
ar_status="$(
  curl -sS -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer $(gcloud auth print-access-token)" \
    "https://${VERTEX_REGION}-aiplatform.googleapis.com/v1beta1/projects/${PROJECT_ID}/locations/${VERTEX_REGION}/reasoningEngines?pageSize=1" || echo 000
)"
case "$ar_status" in
  200) echo "  ✓ Agent Runtime reachable in $VERTEX_REGION" ;;
  403) note_fail "Agent Runtime returned 403 — grant roles/aiplatform.user and retry." ;;
  404|400) note_fail "Agent Runtime is not available in '$VERTEX_REGION' (HTTP $ar_status). Set vertex_region to a supported region such as us-central1 or us-east4." ;;
  *)   note_fail "Unexpected HTTP $ar_status probing Agent Runtime in '$VERTEX_REGION'." ;;
esac

# --- Gemini model -------------------------------------------------------------
# Probe with a real generateContent call. A GET on the publisher model resource
# returns 404 in regions that serve the model perfectly well, so it cannot be
# used to decide availability.
MODEL="${GEMINI_MODEL:-gemini-2.5-flash}"
echo "Checking $MODEL in $VERTEX_REGION..."
model_status="$(
  curl -sS -o /dev/null -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $(gcloud auth print-access-token)" \
    -H "Content-Type: application/json" \
    "https://${VERTEX_REGION}-aiplatform.googleapis.com/v1/projects/${PROJECT_ID}/locations/${VERTEX_REGION}/publishers/google/models/${MODEL}:generateContent" \
    -d '{"contents":[{"role":"user","parts":[{"text":"ok"}]}],"generationConfig":{"maxOutputTokens":8,"thinkingConfig":{"thinkingBudget":0}}}' || echo 000
)"
case "$model_status" in
  200) echo "  ✓ $MODEL served from $VERTEX_REGION" ;;
  404) note_fail "$MODEL is not served from '$VERTEX_REGION'. Set vertex_region to a region that serves it." ;;
  403) note_fail "$MODEL returned 403 — grant roles/aiplatform.user and retry." ;;
  429) echo "  ✓ $MODEL served from $VERTEX_REGION (quota-limited right now)" ;;
  *)   note_fail "Unexpected HTTP $model_status calling $MODEL in '$VERTEX_REGION'." ;;
esac

# --- Cloud Run ----------------------------------------------------------------
echo "Checking Cloud Run in $REGION..."
run_regions="$(gcloud run regions list --project "$PROJECT_ID" \
  --format='value(locationId)' 2>/dev/null || true)"

if [[ -z "$run_regions" ]]; then
  note_fail "Could not list Cloud Run regions. The API may still be enabling — retry in a minute."
elif grep -qx "$REGION" <<<"$run_regions"; then
  echo "  ✓ Cloud Run available in $REGION"
else
  note_fail "Cloud Run is not available in '$REGION'."
fi

# --- Firestore ----------------------------------------------------------------
# An empty list means the query failed — the API was only just enabled, or the
# caller lacks permission — and must not be reported as "location unsupported".
# That false negative once failed a build against a location Firestore does in
# fact serve, so the empty case is called out separately.
echo "Checking Firestore location $FIRESTORE_LOCATION..."
fs_locations="$(gcloud firestore locations list --project "$PROJECT_ID" \
  --format='value(locationId)' 2>/dev/null || true)"

if [[ -z "$fs_locations" ]]; then
  note_fail "Could not list Firestore locations. The API may still be enabling — retry in a minute."
elif grep -qx "$FIRESTORE_LOCATION" <<<"$fs_locations"; then
  echo "  ✓ Firestore can be created in $FIRESTORE_LOCATION"
else
  note_fail "Firestore is not offered in '$FIRESTORE_LOCATION'. Offered in this project:"
  grep -E '^(us|nam)' <<<"$fs_locations" | sed 's/^/      /' >&2
fi

echo
if [[ "$fail" -ne 0 ]]; then
  echo "Preflight failed. Fix the items above before running terraform apply." >&2
  exit 1
fi
echo "Preflight passed."
