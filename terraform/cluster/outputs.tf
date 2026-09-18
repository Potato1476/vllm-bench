output "cluster_name" {
  description = "EKS cluster name."
  value       = module.eks.cluster_name
}

output "cluster_endpoint" {
  description = "Kubernetes API server endpoint."
  value       = module.eks.cluster_endpoint
}

output "oidc_provider_arn" {
  description = "Cluster OIDC provider, needed to add further IRSA roles."
  value       = module.eks.oidc_provider_arn
}

output "vllm_role_arn" {
  description = "Annotate this on the inference:vllm service account."
  value       = module.vllm_irsa.iam_role_arn
}

output "bench_runner_role_arn" {
  description = "Annotate this on the benchmark:bench-runner service account."
  value       = module.bench_runner_irsa.iam_role_arn
}

output "artifacts_bucket_name" {
  description = "Re-exported from core so the Makefile only has to read one state."
  value       = local.artifacts_bucket
}

output "gpu_vcpus_requested" {
  description = "G-family vCPU this configuration will draw, against the account quota."
  value       = local.gpu_vcpus_requested
}

output "kubeconfig_command" {
  description = "Ready-to-run command that points kubectl at this cluster."
  value       = "aws eks update-kubeconfig --region ${local.region} --name ${module.eks.cluster_name}"
}
