# The cluster tier reads the VPC, subnets and bucket from core rather than creating them.
# This is the join between the two lifecycles: destroying the cluster every evening must
# not touch anything core owns.
data "terraform_remote_state" "core" {
  backend = "s3"

  config = {
    bucket = "vllm-bench-tfstate-mlops-lab"
    key    = "core/terraform.tfstate"
    region = "us-east-1"
  }
}

locals {
  region            = data.terraform_remote_state.core.outputs.region
  vpc_id            = data.terraform_remote_state.core.outputs.vpc_id
  public_subnet_ids = data.terraform_remote_state.core.outputs.public_subnet_ids
  artifacts_bucket  = data.terraform_remote_state.core.outputs.artifacts_bucket_name
  artifacts_arn     = data.terraform_remote_state.core.outputs.artifacts_bucket_arn

  # Every G-family instance in this account draws from one regional vCPU quota. Both GPU
  # node groups use 4 vCPU per node, so the two desired counts compete for the same pool.
  gpu_vcpus_requested = (var.gpu_desired + var.gpu_l40s_desired) * 4
}
