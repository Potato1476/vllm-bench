locals {
  cluster_identifier = "${var.project}-litellm"
}

resource "aws_db_subnet_group" "litellm" {
  name       = local.cluster_identifier
  subnet_ids = local.database_subnet_ids

  tags = {
    Name = local.cluster_identifier
  }
}

resource "aws_security_group" "aurora" {
  name_prefix = "${local.cluster_identifier}-"
  description = "Aurora PostgreSQL ingress from the EKS application subnets only"
  vpc_id      = local.vpc_id

  tags = {
    Name = "${local.cluster_identifier}-aurora"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "postgres_from_app" {
  for_each = toset(local.app_subnet_cidrs)

  security_group_id = aws_security_group.aurora.id
  description       = "PostgreSQL from EKS app subnet ${each.value}"
  cidr_ipv4         = each.value
  from_port         = 5432
  to_port           = 5432
  ip_protocol       = "tcp"
}

resource "aws_cloudwatch_log_group" "postgresql" {
  name              = "/aws/rds/cluster/${local.cluster_identifier}/postgresql"
  retention_in_days = 7
}

resource "aws_rds_cluster" "litellm" {
  cluster_identifier = local.cluster_identifier
  engine             = "aurora-postgresql"
  database_name      = var.database_name
  master_username    = var.master_username

  # No database password enters Terraform variables or state. RDS creates the secret,
  # encrypts it with the AWS-managed Secrets Manager KMS key and rotates it.
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.litellm.name
  vpc_security_group_ids = [aws_security_group.aurora.id]
  port                   = 5432
  storage_encrypted      = true

  backup_retention_period      = var.backup_retention_days
  preferred_backup_window      = "18:00-19:00"
  preferred_maintenance_window = "sun:19:00-sun:20:00"
  copy_tags_to_snapshot        = true
  delete_automated_backups     = false
  deletion_protection          = var.deletion_protection
  skip_final_snapshot          = var.skip_final_snapshot
  final_snapshot_identifier    = "${local.cluster_identifier}-final"

  enabled_cloudwatch_logs_exports     = ["postgresql"]
  iam_database_authentication_enabled = true

  depends_on = [aws_cloudwatch_log_group.postgresql]
}

resource "aws_rds_cluster_instance" "litellm" {
  count = var.instance_count

  identifier         = "${local.cluster_identifier}-${count.index + 1}"
  cluster_identifier = aws_rds_cluster.litellm.id
  instance_class     = var.instance_class
  engine             = aws_rds_cluster.litellm.engine
  engine_version     = aws_rds_cluster.litellm.engine_version
  availability_zone  = local.database_azs[count.index]

  db_subnet_group_name       = aws_db_subnet_group.litellm.name
  publicly_accessible        = false
  auto_minor_version_upgrade = true
  promotion_tier             = count.index
  apply_immediately          = true

  lifecycle {
    precondition {
      condition     = var.instance_count <= length(local.database_azs)
      error_message = "instance_count exceeds the number of Availability Zones in the database subnet group."
    }
  }
}
