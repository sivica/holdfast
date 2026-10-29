output "uri" {
  description = "Cloud Run service URI."
  value       = google_cloud_run_v2_service.holdfast.uri
}

output "service_account" {
  description = "Runtime service account. It can read only the approval secret."
  value       = google_service_account.holdfast.email
}
