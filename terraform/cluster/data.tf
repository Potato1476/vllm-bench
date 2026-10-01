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

  # Subnets the NODE GROUPS may use. The cluster itself still gets all of them.
  node_subnet_ids = slice(local.public_subnet_ids, 0, var.node_subnet_count)

  # Every G-family instance in this account draws from one regional vCPU quota. Both GPU
  # node groups use 4 vCPU per node, so the two desired counts compete for the same pool.
  gpu_vcpus_requested = (var.gpu_desired + var.gpu_l40s_desired) * 4

  # The node label that says WHICH CARD a node carries, derived from the instance type
  # instead of written next to it.
  #
  # It was two hardcoded strings, `gpu-type = "l4"` and `gpu-type = "l40s"`, which were
  # true only for the default instance type of each group. The moment g6e.xlarge turned
  # out to have no capacity in either AZ and the main group was pointed at g5.xlarge, that
  # label would have gone on reading "l4" while an A10G answered every request -- and the
  # label is not decoration. It selects nodes, it tags DCGM series, and it is what a
  # report six weeks from now uses to say which card produced which number. A wrong one
  # does not fail; it publishes.
  gpu_type_labels = {
    "g6.xlarge"   = "l4"
    "g6.2xlarge"  = "l4"
    "g6e.xlarge"  = "l40s"
    "g6e.2xlarge" = "l40s"
    "g5.xlarge"   = "a10g"
    "g5.2xlarge"  = "a10g"
  }
  gpu_type     = lookup(local.gpu_type_labels, var.gpu_instance_type, "unknown")
  gpu_alt_type = lookup(local.gpu_type_labels, var.gpu_l40s_instance_type, "unknown")
}
