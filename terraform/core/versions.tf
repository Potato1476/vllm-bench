terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }

  # Core holds everything that must outlive a working session: the VPC, the buckets, the
  # registry, the budget alarms. It is applied once in week 1 and destroyed in week 6.
  # Nothing in here bills by the hour, so leaving it up costs nothing.
  backend "s3" {
    bucket       = "vllm-bench-tfstate-mlops-lab"
    key          = "core/terraform.tfstate"
    region       = "us-east-1"
    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      project   = var.project_tag
      owner     = var.owner
      layer     = "core"
      ManagedBy = "terraform"
    }
  }
}
