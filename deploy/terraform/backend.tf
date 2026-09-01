# State lives in GCS so Cloud Build and a local operator share one lock.
# Bootstrap: run the first apply with -backend=false, then
#   terraform init -migrate-state -backend-config=bucket=<tf_state_bucket output>
terraform {
  backend "gcs" {
    prefix = "terraform/state"
  }
}
