terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }

  # Aurora survives the disposable EKS cluster. Keeping it in a third state prevents
  # `make lab-down` from deleting keys, quotas and usage history with the compute tier.
  backend "s3" {
    bucket       = "vllm-bench-tfstate-mlops-lab"
    key          = "data/terraform.tfstate"
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
      layer     = "data"
      ManagedBy = "terraform"
    }
  }
}
