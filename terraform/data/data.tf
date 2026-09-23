# The data tier joins the long-lived VPC but owns no network. Its lifecycle is separate
# from both core and EKS: core must exist first, while EKS may come and go around it.
data "terraform_remote_state" "core" {
  backend = "s3"

  config = {
    bucket = "vllm-bench-tfstate-mlops-lab"
    key    = "core/terraform.tfstate"
    region = "us-east-1"
  }
}

locals {
  region              = data.terraform_remote_state.core.outputs.region
  vpc_id              = data.terraform_remote_state.core.outputs.vpc_id
  app_subnet_ids      = data.terraform_remote_state.core.outputs.public_subnet_ids
  database_subnet_ids = data.terraform_remote_state.core.outputs.private_subnet_ids

  # Lab nodes live in the public subnets. Restrict PostgreSQL to exactly those CIDRs
  # instead of the whole VPC; the database itself remains in isolated private subnets.
  app_subnet_cidrs = [for subnet in data.aws_subnet.app : subnet.cidr_block]
  database_azs     = sort(distinct([for subnet in data.aws_subnet.database : subnet.availability_zone]))
}

data "aws_subnet" "app" {
  for_each = toset(local.app_subnet_ids)
  id       = each.value
}

data "aws_subnet" "database" {
  for_each = toset(local.database_subnet_ids)
  id       = each.value
}
