locals {
  # AL2023 nodes are configured through nodeadm. RAID0 puts the instance-store NVMe under
  # containerd and the kubelet, which is what makes an emptyDir land on local NVMe instead
  # of the EBS root volume. Both g6.xlarge and g6e.xlarge ship 250 GB of it, free, and it
  # is far faster than gp3 -- the model weights are synced into it on every pod start.
  # Without this the disk is present but unmounted and the emptyDir silently falls back to
  # the root volume, which is the failure that looks like "S3 sync got slower".
  nodeadm_raid0 = <<-YAML
    apiVersion: node.eks.aws/v1alpha1
    kind: NodeConfig
    spec:
      instance:
        localStorage:
          strategy: RAID0
  YAML

  gpu_cloudinit = [{
    content_type = "application/node.eks.aws"
    content      = local.nodeadm_raid0
  }]
}

# A plan that asks for more G-family vCPU than the account is allowed does not fail at
# plan time -- it fails after the cluster exists, when the node group cannot scale. The
# check moves that discovery to before anything is created.
check "gpu_quota" {
  assert {
    condition = local.gpu_vcpus_requested <= var.gpu_vcpu_quota
    error_message = format(
      "gpu_desired=%d and gpu_l40s_desired=%d need %d vCPU, but the G-family quota is %d. Raise the quota or lower a count.",
      var.gpu_desired, var.gpu_l40s_desired, local.gpu_vcpus_requested, var.gpu_vcpu_quota
    )
  }
}

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.24"

  cluster_name    = var.cluster_name
  cluster_version = var.k8s_version

  vpc_id = local.vpc_id

  # Public subnets: there is no NAT gateway for private ones to reach out through.
  # See the budget-profile note in the core tier's network.tf.
  subnet_ids = local.public_subnet_ids

  cluster_endpoint_public_access       = true
  cluster_endpoint_public_access_cidrs = var.allowed_cidrs

  # Nothing beyond what the module opens for node-to-node and node-to-control-plane.
  # Any CIDR rule added here would be directly internet-reachable, because the nodes
  # carry public IPs.
  node_security_group_additional_rules = {}

  enable_irsa                              = true
  enable_cluster_creator_admin_permissions = true

  # A customer-managed KMS key is not deleted by `terraform destroy`; it is scheduled for
  # deletion 30 days out and billed at $1/month the whole time. With a cluster created and
  # destroyed every working day that becomes dozens of pending keys guarding a cluster
  # that holds no real secrets. etcd is still encrypted with an AWS-managed key.
  create_kms_key            = false
  cluster_encryption_config = {}

  # Control plane logging off for the same reason: audit is the chattiest stream, ingestion
  # bills per GB, and the log group outlives the cluster with 90-day retention. Turn
  # `authenticator` back on temporarily if nodes ever fail to join.
  create_cloudwatch_log_group = false
  cluster_enabled_log_types   = []

  cluster_addons = {
    coredns    = {}
    kube-proxy = {}
    vpc-cni    = {}
    aws-ebs-csi-driver = {
      service_account_role_arn = module.ebs_csi_irsa.iam_role_arn
    }
    # Pod Identity replaces IRSA for new workloads: no OIDC trust policy to write, and
    # the association is a first-class API object.
    eks-pod-identity-agent = {}
    metrics-server         = {}
  }

  eks_managed_node_groups = {
    # Gateway, guardrail, Prometheus and Grafana. The load generator deliberately does
    # NOT run here -- it gets its own t3.large so that a saturated client cannot be
    # mistaken for a saturated engine.
    cpu = {
      name           = "cpu"
      subnet_ids     = local.node_subnet_ids
      instance_types = [var.cpu_instance_type]
      capacity_type  = "ON_DEMAND"

      min_size     = 1
      max_size     = 2
      desired_size = 1

      labels = {
        workload = "tooling"
      }

      # v20 always builds a launch template, which makes the module's `disk_size` input a
      # no-op. Root volume size has to come from the block device mapping.
      block_device_mappings = {
        xvda = {
          device_name = "/dev/xvda"
          ebs = {
            volume_size           = 30
            volume_type           = "gp3"
            encrypted             = true
            delete_on_termination = true
          }
        }
      }
    }

    # The measurement machine. L4, 24 GB, Ada -- so FP8 is available, which the capacity
    # model in the plan depends on.
    gpu = {
      name           = "gpu"
      subnet_ids     = local.node_subnet_ids
      instance_types = [var.gpu_instance_type]
      capacity_type  = "ON_DEMAND"

      ami_type = "AL2023_x86_64_NVIDIA"

      min_size     = 0
      max_size     = 3
      desired_size = var.gpu_desired

      cloudinit_pre_nodeadm = local.gpu_cloudinit

      labels = {
        workload         = "inference"
        gpu-type         = "l4"
        "nvidia.com/gpu" = "true"
      }

      # Nothing lands on the machine being measured unless it deliberately tolerates
      # this. A log shipper or a stray job co-tenanting here is measurement noise.
      taints = {
        gpu = {
          key    = "nvidia.com/gpu"
          value  = "true"
          effect = "NO_SCHEDULE"
        }
      }

      # 40 GiB is enough for the OS and the container images; the weights go to the
      # 250 GB NVMe, not here.
      block_device_mappings = {
        xvda = {
          device_name = "/dev/xvda"
          ebs = {
            volume_size           = 40
            volume_type           = "gp3"
            encrypted             = true
            delete_on_termination = true
          }
        }
      }
    }

    # Used for exactly one four-hour session in week 2, to get dollars-per-req/s for L40S
    # against L4. That single number is what the production hardware choice rests on, so
    # it is worth 7.44 USD. Left at 0 for the rest of the project.
    gpu-l40s = {
      name           = "gpu-l40s"
      subnet_ids     = local.node_subnet_ids
      instance_types = [var.gpu_l40s_instance_type]
      capacity_type  = "ON_DEMAND"

      ami_type = "AL2023_x86_64_NVIDIA"

      min_size     = 0
      max_size     = 1
      desired_size = var.gpu_l40s_desired

      cloudinit_pre_nodeadm = local.gpu_cloudinit

      labels = {
        workload         = "inference"
        gpu-type         = "l40s"
        "nvidia.com/gpu" = "true"
      }

      taints = {
        gpu = {
          key    = "nvidia.com/gpu"
          value  = "true"
          effect = "NO_SCHEDULE"
        }
      }

      block_device_mappings = {
        xvda = {
          device_name = "/dev/xvda"
          ebs = {
            volume_size           = 40
            volume_type           = "gp3"
            encrypted             = true
            delete_on_termination = true
          }
        }
      }
    }
  }
}
