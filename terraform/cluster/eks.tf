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

      # min_size 0, not 1, and this is the difference between a clean teardown and an
      # instance that will not die.
      #
      # A managed node group is an autoscaling group underneath, and an ASG whose minimum
      # is 1 REPLACES any instance that terminates, within about a minute. That is correct
      # ASG behaviour and exactly wrong here: when a destroy fails partway -- or when
      # somebody terminates the box by hand to stop the billing -- AWS quietly launches
      # another, and the symptom reads as "EC2 keeps creating instances in a loop".
      #
      # desired_size still brings one node up on apply, so nothing about normal operation
      # changes. All min_size=0 gives up is the guarantee that the node comes back by
      # itself, which for a lab that is torn down every evening was never a guarantee
      # worth having.
      # desired_size 2, and the 1 it replaces is a regression that came back because the
      # first fix was only ever an `aws eks update-nodegroup-config` against a live
      # cluster. That cluster is destroyed every evening, so the fix died with it -- the
      # same mistake charts/litellm/values.yaml documents for the 1Gi memory limit, made
      # a second time in the same repo.
      #
      # READ THIS BEFORE EDITING desired_size AND EXPECTING AN APPLY TO DO ANYTHING.
      # The upstream module sets
      #     ignore_changes = [scaling_config[0].desired_size]
      # on the node group, so this value is honoured ONLY when the group is created. On a
      # live cluster an apply reports no change and the count does not move; scale it with
      # `aws eks update-nodegroup-config` instead, which is why `make gpu` uses the CLI
      # rather than Terraform. Committing it here is still the fix rather than a formality,
      # because this cluster tier is destroyed and recreated every session -- so creation
      # is the only moment that matters.
      #
      # One t3.large is 2 vCPU, and the tooling node carries Prometheus, Grafana, Tempo,
      # LiteLLM, the guardrail and TEI. It ran out of CPU four times in one session.
      #
      # It is now load-bearing for correctness, not just comfort: bench/k6 runs as a Job
      # with nodeSelector workload=tooling and requests a full CPU. On a single 2-vCPU
      # node the load generator would contend with the gateway it is measuring through --
      # README.md's warning about a generator stealing CPU and inflating the latency it
      # reports, except self-inflicted and on the client side.
      min_size     = 0
      max_size     = 3
      desired_size = var.cpu_desired

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

      min_size = 0
      # 4, because the approved quota is 16 vCPU for G instances and a g6.xlarge is 4.
      # Three was the old ceiling and it silently capped the scale test one node below
      # what the account actually allows.
      max_size     = 4
      desired_size = var.gpu_desired

      cloudinit_pre_nodeadm = local.gpu_cloudinit

      labels = {
        workload         = "inference"
        gpu-type         = local.gpu_type
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

    # Began as one four-hour comparison session in week 2 to get dollars-per-req/s against
    # L4. It is now the candidate serving card: four L4s measured 40 req/s against TC1's
    # 50, the 16 vCPU quota allows no fifth L4, and g6e.xlarge is also 4 vCPU -- so four
    # L40S fit the quota that four L4s already fill. Still 0 by default; this group costs
    # 1.861 USD/hr per node.
    # Tang model nhe: mot card rieng, nho va re, thay vi cat mot phan card cua 7B.
    #
    # Mot 1.5B o `mode: shared` chiem 25% card va de lai cho 7B 14.6 GiB -- du cho AWQ
    # nhung lam giam tran thong luong cua chinh model dang phai dat 50 req/s. Mot T4 16GB
    # rieng phuc vu 1.5B thoai mai (FP16 chi 2.9 GiB) voi gia bang mot nua g5.
    #
    # desired_size 0 va se con 0: quota G la 16 vCPU, moi .xlarge an 4, va ca bon node
    # dang can cho 7B. Xem var.gpu_light_desired.
    gpu-light = {
      name           = "gpu-light"
      subnet_ids     = local.node_subnet_ids
      instance_types = [var.gpu_light_instance_type]
      capacity_type  = "ON_DEMAND"

      ami_type = "AL2023_x86_64_NVIDIA"

      min_size = 0
      # 2, khong phai 1: du cho mot ban sao thu hai khi can do kha dung cua tang nhe,
      # va van nam trong quota neu 7B lui ve 3 node.
      max_size     = 2
      desired_size = var.gpu_light_desired

      cloudinit_pre_nodeadm = local.gpu_cloudinit

      labels = {
        workload         = "inference"
        gpu-type         = local.gpu_light_type
        # Nhan rieng de chart co the ghim dung tang, thay vi chi noi "co GPU". Khong co
        # no, mot pod 7B co the roi vao T4 va hong luc nap voi thong bao ve bo nho chu
        # khong phai ve viec xep lich sai cho.
        tier             = "light"
        "nvidia.com/gpu" = "true"
      }

      taints = [{
        # Chi workload khai ro moi duoc xuong day. Mot card T4 re tien rat de bi mot pod
        # khong lien quan chiem mat.
        key    = "tier"
        value  = "light"
        effect = "NO_SCHEDULE"
      }]
    }

    gpu-l40s = {
      name           = "gpu-l40s"
      subnet_ids     = local.node_subnet_ids
      instance_types = [var.gpu_l40s_instance_type]
      capacity_type  = "ON_DEMAND"

      ami_type = "AL2023_x86_64_NVIDIA"

      min_size = 0
      # 4 to match the quota, not 1. See the note on var.gpu_l40s_desired: this was one of
      # four ceilings that each had to be widened before a second L40S could exist, and
      # three of them failed silently.
      #
      # Capacity, not just the ceiling, is why node_subnet_count matters here. A week-2
      # apply died on InsufficientInstanceCapacity for g6e.xlarge in us-east-1a, and with
      # the group pinned to one subnet there was no second AZ for the ASG to try. The
      # instance type is OFFERED in four us-east-1 AZs; being offered is not being
      # available, and the only configuration that can react to that is more than one
      # subnet.
      max_size     = 4
      desired_size = var.gpu_l40s_desired

      cloudinit_pre_nodeadm = local.gpu_cloudinit

      labels = {
        workload         = "inference"
        gpu-type         = local.gpu_alt_type
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
