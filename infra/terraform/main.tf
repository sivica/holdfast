terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# Paper desk only. This module does not create a trading key and does not set
# HLGUARD_ALLOW_MAINNET. Mainnet stays closed unless an operator edits the
# image config and sets that variable outside this module.

resource "google_service_account" "holdfast" {
  account_id   = "holdfast-runner"
  display_name = "Holdfast paper agent"
}

resource "google_secret_manager_secret" "approval" {
  secret_id = var.approval_secret_id
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_iam_member" "approval" {
  secret_id = google_secret_manager_secret.approval.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.holdfast.email}"
}

resource "google_cloud_run_v2_service" "holdfast" {
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.holdfast.email
    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }
    containers {
      image = var.image
      ports {
        container_port = 8741
      }
      env {
        name  = "HLGUARD_VENUE"
        value = "paper"
      }
      env {
        name  = "HLGUARD_MARKET_SOURCE"
        value = "file"
      }
      env {
        name = "HLGUARD_APPROVAL_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.approval.secret_id
            version = "latest"
          }
        }
      }
      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "public" {
  count    = var.public_invoker ? 1 : 0
  name     = google_cloud_run_v2_service.holdfast.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}
