variable "project_tag" {
  description = "Value of the `project` cost-allocation tag."
  type        = string
  default     = "da51"
}

variable "owner" {
  description = "Value of the `owner` tag: bao or minh."
  type        = string
  default     = "bao"
}

variable "cluster_name" {
  description = "EKS cluster name."
  type        = string
  default     = "da51-lab"
}

variable "k8s_version" {
  description = "Control plane version. Must be in EKS STANDARD support -- version_guard.tf refuses to plan otherwise, because extended support bills $0.60/h instead of $0.10/h."
  type        = string
  # 1.35: standard support until 2027-03-27. Was 1.31, which aged into extended support on
  # 2025-11-26 underneath a comment warning against exactly that; see version_guard.tf.
  default = "1.35"
}

variable "allow_extended_support" {
  description = "Set true only to stay on an out-of-standard-support version on purpose. Costs $0.50/h extra per cluster."
  type        = bool
  default     = false
}

# No default on purpose. With nodes on public subnets this CIDR is the cluster's only
# front door, so a stale or forgotten value must fail loudly rather than widen access.
# Residential IPs rotate; refresh with `make fix-cidr`.
variable "allowed_cidrs" {
  description = "CIDRs allowed to reach the EKS public API endpoint. One /32 per team member."
  type        = list(string)

  validation {
    condition     = length(var.allowed_cidrs) > 0
    error_message = "allowed_cidrs must not be empty."
  }

  validation {
    condition     = !contains(var.allowed_cidrs, "0.0.0.0/0")
    error_message = "Refusing 0.0.0.0/0: that exposes the API endpoint to the whole internet."
  }
}

# EKS itself requires subnets in at least two Availability Zones -- that is an AWS
# constraint on the control plane and cannot be avoided. Subnets, route tables and the
# internet gateway are free, so spanning two costs nothing.
#
# What this controls is where the NODES go. Pinning every node group to one AZ keeps the
# benchmark on one network segment: no cross-AZ hop between the load generator and the
# engine, and no chance of an EBS volume being stranded in the AZ a replacement node did
# not come up in. Set to 2 for the week-6 HA demo, which is the one time the point is to
# show the topology surviving an AZ.
variable "node_subnet_count" {
  description = "How many AZs the node groups may use. 1 for measurement work, 2 for the HA demo."
  type        = number
  default     = 1

  validation {
    condition     = contains([1, 2], var.node_subnet_count)
    error_message = "node_subnet_count must be 1 or 2."
  }
}

# --- CPU node group ---------------------------------------------------------
variable "cpu_instance_type" {
  description = "Runs the gateway, guardrail, Prometheus and Grafana. m7i.large is what the budget assumes."
  type        = string
  default     = "m7i.large"
}

variable "cpu_desired" {
  description = "Tooling nodes for gateway, guardrail, monitoring and Argo CD."
  type        = number

  # 3, raised from 2 when Argo CD joined the tooling tier.
  #
  # Two was already tight: on 2026-10-07 the two nodes sat at 67% and 73% CPU with 625m
  # and 510m free, and the k6 job -- which requests 1000m -- had to have a third node
  # switched on by hand before it would schedule. Argo CD adds another 350m across five
  # workloads on top of that.
  #
  # The cost of being wrong is not an error. A pod that does not fit sits Pending with
  # "Insufficient cpu", which reads as a scheduling hiccup rather than as a sizing
  # mistake, and the thing that fails is whichever component happened to restart last.
  #
  # READ THE COMMENT IN eks.tf BEFORE EXPECTING AN APPLY TO CHANGE A RUNNING CLUSTER:
  # the upstream module ignores desired_size after creation, so this value only takes
  # effect when the node group is created. That is every morning here, which is why
  # committing it is the fix rather than a formality.
  default = 3

  validation {
    condition     = var.cpu_desired >= 1 && var.cpu_desired <= 3 && floor(var.cpu_desired) == var.cpu_desired
    error_message = "cpu_desired must be an integer between 1 and 3."
  }
}

# --- GPU node groups --------------------------------------------------------
variable "gpu_instance_type" {
  description = "Main measurement GPU. g5.xlarge is A10G 24GB at $1.006/hr."
  type        = string
  # g5.xlarge, NOT the cheaper g6.xlarge, and the difference decides whether TC1a is met.
  #
  # Four L4s serve 40 req/s against the criterion's 50 -- eks.tf says so in the gpu-l40s
  # comment, and the measured per-card figures in bench/scripts/finops_curve.py agree:
  # 12.5 req/s on A10G against 10.0 on L4. The 16 vCPU quota allows no fifth node of
  # either kind, so on L4 there is no way to reach 50.
  #
  # This default was g6.xlarge while every reported result was measured on A10G, which
  # only worked because a gitignored terraform.tfvars on one laptop said g5.xlarge. The
  # committed configuration did not reproduce the committed numbers, and the way that
  # surfaces is a rebuilt cluster quietly missing the criterion by 20%.
  default = "g5.xlarge"
}

# --- tang node nhe -----------------------------------------------------------------
#
# Model 1.5B khong nen an mot phan cua card ma 7B dang can. `mode: shared` chia mot card
# lam hai, va phan con lai cho 7B khong con du de dat 50 req/s; mot card rieng, nho va
# re, dung voi hinh dang cua cong viec hon.
#
# desired 0, VA SE CON 0 CHO TOI KHI QUOTA TANG. Quota G hien la 16 vCPU va moi .xlarge
# an 4, nen bon node la tran -- ca bon deu dang can cho 7B (12.5 req/s moi card x 4 = 50,
# vua du). Bat node nay len bay gio la lay mat mot card cua 7B va TC1a tut xuong 37.5.
#
# Mo bang cach xin quota len 20 vCPU, roi dat gpu_light_desired = 1. Chi phi them 0.526
# USD/gio.
variable "gpu_light_instance_type" {
  description = "GPU cho model nhe. g4dn.xlarge la T4 16GB o $0.526/hr -- du rong cho 1.5B."
  type        = string
  default     = "g4dn.xlarge"
}

variable "gpu_light_desired" {
  description = "Node cho tang model nhe. Giu 0 cho toi khi quota G vuot 16 vCPU."
  type        = number
  default     = 0

  validation {
    condition     = var.gpu_light_desired >= 0 && var.gpu_light_desired <= 2
    error_message = "gpu_light_desired must be between 0 and 2."
  }
}

variable "gpu_desired" {
  description = "L4 nodes. 0 outside a measurement window, 1 for normal work, 4 for the scale-verification session."
  type        = number
  default     = 0

  # 4, not 3. The G-family quota is 16 vCPU and a g6.xlarge is 4, so four nodes is exactly
  # what the account allows -- the old ceiling of 3 gave away a quarter of the available
  # capacity, and it did so at a layer no error message pointed at: the node group
  # max_size was also 3, so raising one and not the other still fails.
  #
  # The real ceiling is the `gpu_quota` check in eks.tf, which counts BOTH node groups
  # against var.gpu_vcpu_quota. That is the one that should refuse a plan; this bound only
  # catches a typo.
  validation {
    condition     = var.gpu_desired >= 0 && var.gpu_desired <= 4
    error_message = "gpu_desired must be between 0 and 4 (16 vCPU quota / 4 vCPU per g6.xlarge)."
  }
}

variable "gpu_l40s_instance_type" {
  description = "Comparison GPU for the single week-2 session. g6e.xlarge is L40S 48GB at $1.861/hr."
  type        = string
  default     = "g6e.xlarge"
}

variable "gpu_l40s_desired" {
  description = "L40S nodes. 0 for ordinary sessions; up to 4 when L40S is the serving card under test."
  type        = number
  default     = 0

  # 4, not 1. The old bound encoded an assumption rather than a limit: L40S was only ever
  # going to be one node for one comparison session. Then the measured 40 req/s ceiling on
  # four L4s left TC1's 50 req/s out of reach, and the 16 vCPU quota allows no fifth L4 --
  # while g6e.xlarge is also 4 vCPU, so four L40S fit the same quota. The card stopped
  # being a comparison and became the candidate, and `<= 1` was then one of four separate
  # places that had to be widened to find that out.
  validation {
    condition     = var.gpu_l40s_desired >= 0 && var.gpu_l40s_desired <= 4
    error_message = "gpu_l40s_desired must be between 0 and 4 (16 vCPU quota / 4 vCPU per g6e.xlarge)."
  }
}

variable "gpu_vcpu_quota" {
  description = "The account's regional 'Running On-Demand G and VT instances' quota, in vCPU. Used to refuse a plan that cannot launch."
  type        = number
  default     = 16
}
