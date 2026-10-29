variable "project_id" {
  type        = string
  description = "GCP project id."
}

variable "region" {
  type        = string
  description = "Cloud Run region. europe-west1 is the default for this portfolio."
  default     = "europe-west1"
}

variable "service_name" {
  type    = string
  default = "holdfast"
}

variable "image" {
  type        = string
  description = "Container image for the paper desk. Build and push the repo Dockerfile yourself."
}

variable "approval_secret_id" {
  type    = string
  default = "holdfast-approval-secret"
}

variable "public_invoker" {
  type        = bool
  description = "Allow unauthenticated Cloud Run invoke. Leave false until you intend the desk to be public."
  default     = false
}
