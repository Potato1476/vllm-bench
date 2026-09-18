terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.30"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.14"
    }
  }

  # Applied at the start of a working session and destroyed at the end. Everything in
  # here bills by the hour, which is the entire reason the two tiers are separate.
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
