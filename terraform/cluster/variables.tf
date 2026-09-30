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
  description = "Control plane version. Keep on a version still in STANDARD support: extended support raises the cluster fee from $0.10 to $0.60 per hour, six times the cost for nothing."
  type        = string
  default     = "1.31"
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
  description = "Tooling nodes for gateway, guardrail and monitoring. Use 3 for the three-replica HA profile."
  type        = number
  default     = 2

  validation {
    condition     = var.cpu_desired >= 1 && var.cpu_desired <= 3 && floor(var.cpu_desired) == var.cpu_desired
    error_message = "cpu_desired must be an integer between 1 and 3."
  }
}

# --- GPU node groups --------------------------------------------------------
variable "gpu_instance_type" {
  description = "Main measurement GPU. g6.xlarge is L4 24GB at $0.8048/hr -- 25% cheaper than g5.xlarge, and Ada so it has native FP8."
  type        = string
  default     = "g6.xlarge"
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
  description = "L40S nodes. 1 only during the four-hour comparison session in week 2; 0 the rest of the project."
  type        = number
  default     = 0

  validation {
    condition     = var.gpu_l40s_desired >= 0 && var.gpu_l40s_desired <= 1
    error_message = "gpu_l40s_desired must be 0 or 1."
  }
}

variable "gpu_vcpu_quota" {
  description = "The account's regional 'Running On-Demand G and VT instances' quota, in vCPU. Used to refuse a plan that cannot launch."
  type        = number
  default     = 16
}
