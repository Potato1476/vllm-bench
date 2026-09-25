terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }

  # Applied at the start of a working session and destroyed at the end. Persistent
  # Aurora lives in the separate data state, so cluster teardown cannot erase gateway
  # identities, quotas or usage history.
  backend "s3" {
    bucket       = "vllm-bench-tfstate-mlops-lab"
    key          = "cluster/terraform.tfstate"
    region       = "us-east-1"
    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region = local.region

  default_tags {
    tags = {
      project   = var.project_tag
      owner     = var.owner
      layer     = "cluster"
      ManagedBy = "terraform"
    }
  }
}
