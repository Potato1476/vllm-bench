output "vpc_id" {
  description = "VPC the cluster tier attaches to."
  value       = module.vpc.vpc_id
}

output "public_subnet_ids" {
  description = "Public subnets. Nodes live here because the lab has no NAT gateway."
  value       = module.vpc.public_subnets
}

output "private_subnet_ids" {
  description = "Isolated private subnets used by the persistent Aurora data tier."
  value       = module.vpc.private_subnets
}

output "artifacts_bucket_name" {
  description = "Bucket holding model weights, datasets, benchmark runs and metric snapshots."
  value       = aws_s3_bucket.artifacts.id
}

output "artifacts_bucket_arn" {
  description = "Used by the cluster tier to scope the IRSA policies."
  value       = aws_s3_bucket.artifacts.arn
}

output "ecr_bench_runner_url" {
  description = "Repository the bench runner image is pushed to."
  value       = aws_ecr_repository.bench_runner.repository_url
}

output "ecr_guardrail_url" {
  description = "Repository containing the OpenAI-compatible guardrail service image."
  value       = aws_ecr_repository.guardrail.repository_url
}

output "region" {
  description = "Region, re-exported so the cluster tier does not have to be told twice."
  value       = var.region
}
