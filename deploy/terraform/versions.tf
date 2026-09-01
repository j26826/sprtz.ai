terraform {
  required_version = ">= 1.6.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.13"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 7.13"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}

# Some APIs (Identity Platform, IAP) bill against the consumer project and need
# user_project_override to be set explicitly.
provider "google-beta" {
  alias                 = "billing_override"
  project               = var.project_id
  region                = var.region
  billing_project       = var.project_id
  user_project_override = true
}
