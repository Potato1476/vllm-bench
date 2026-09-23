output "aurora_writer_endpoint" {
  description = "Writer endpoint used by LiteLLM for keys, quota and spend data."
  value       = aws_rds_cluster.litellm.endpoint
}

output "aurora_reader_endpoint" {
  description = "Read-only cluster endpoint; reserved for a future read path."
  value       = aws_rds_cluster.litellm.reader_endpoint
}

output "aurora_port" {
  description = "PostgreSQL port."
  value       = aws_rds_cluster.litellm.port
}

output "database_name" {
  description = "Initial LiteLLM database."
  value       = aws_rds_cluster.litellm.database_name
}

output "master_user_secret_arn" {
  description = "RDS-managed Secrets Manager secret used only to bootstrap the current lab integration."
  value       = aws_rds_cluster.litellm.master_user_secret[0].secret_arn
}

output "aurora_security_group_id" {
  description = "Security group accepting PostgreSQL only from the application subnet CIDRs."
  value       = aws_security_group.aurora.id
}
