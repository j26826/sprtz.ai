#!/usr/bin/env bash
# Create the two resources that must exist before Terraform can run at all.
#
# Terraform cannot own either of these:
#   - the state bucket would be holding the state that manages it, so the first
#     apply has nowhere to write and a destroy would delete its own backend;
#   - the Artifact Registry repository has to exist before CI pushes images,
#     which happens before the full apply.
#
# Both are created idempotently, so this is safe on every build. The registry is
# then adopted into state by the targeted apply that follows.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
APP_NAME="${APP_NAME:-sprtz}"
ENVIRONMENT="${ENVIRONMENT:-dev}"

STATE_BUCKET="${TF_STATE_BUCKET:-${PROJECT_ID}-${APP_NAME}-${ENVIRONMENT}-tfstate}"
AR_REPO="${AR_REPO:-${APP_NAME}-${ENVIRONMENT}-containers}"

echo "Project      : $PROJECT_ID"
echo "Region       : $REGION"
echo "State bucket : gs://$STATE_BUCKET"
echo "Registry     : $AR_REPO"
echo

echo "Enabling the APIs needed to bootstrap..."
gcloud services enable \
  storage.googleapis.com \
  artifactregistry.googleapis.com \
  cloudresourcemanager.googleapis.com \
  serviceusage.googleapis.com \
  --project "$PROJECT_ID" --quiet

# --- Terraform state bucket ---------------------------------------------------
if gcloud storage buckets describe "gs://$STATE_BUCKET" --project "$PROJECT_ID" >/dev/null 2>&1; then
  echo "✓ state bucket already exists"
else
  echo "Creating state bucket..."
  gcloud storage buckets create "gs://$STATE_BUCKET" \
    --project "$PROJECT_ID" \
    --location "$REGION" \
    --uniform-bucket-level-access
  echo "✓ state bucket created"
fi

# Enforced whether or not this run created the bucket. A bucket that already
# exists may have been made by something else — gcloud will happily auto-create
# a staging bucket of the same name without versioning — and unversioned state
# has no recovery path from a corrupted or half-written write.
gcloud storage buckets update "gs://$STATE_BUCKET" --versioning --project "$PROJECT_ID" >/dev/null
echo "✓ state bucket versioning on"

# --- Artifact Registry --------------------------------------------------------
if gcloud artifacts repositories describe "$AR_REPO" \
     --project "$PROJECT_ID" --location "$REGION" >/dev/null 2>&1; then
  echo "✓ artifact registry already exists"
else
  echo "Creating artifact registry..."
  gcloud artifacts repositories create "$AR_REPO" \
    --project "$PROJECT_ID" \
    --location "$REGION" \
    --repository-format docker \
    --description "Sprtz AI container images."
  echo "✓ artifact registry created"
fi

echo
echo "Bootstrap complete."
echo "  TF_STATE_BUCKET=$STATE_BUCKET"
