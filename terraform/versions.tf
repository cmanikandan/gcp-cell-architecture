terraform {
  required_version = ">= 1.6.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.12.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = ">= 6.12.0"
    }
  }
}

provider "google" {
  project = var.platform_project_id
  region  = var.primary_region
}

provider "google-beta" {
  project = var.platform_project_id
  region  = var.primary_region
}
