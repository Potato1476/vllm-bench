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

# --- CPU node group ---------------------------------------------------------
variable "cpu_instance_type" {
  description = "Runs the gateway, guardrail, Prometheus and Grafana. m7i.large is what the budget assumes."
  type        = string
  default     = "m7i.large"
}

# --- GPU node groups --------------------------------------------------------
variable "gpu_instance_type" {
  description = "Main measurement GPU. g6.xlarge is L4 24GB at $0.8048/hr -- 25% cheaper than g5.xlarge, and Ada so it has native FP8."
  type        = string
  default     = "g6.xlarge"
}

variable "gpu_desired" {
  description = "L4 nodes. 0 outside a measurement window, 1 for normal work, 3 for the scale-verification session."
  type        = number
  default     = 0

  validation {
    condition     = var.gpu_desired >= 0 && var.gpu_desired <= 3
    error_message = "gpu_desired must be between 0 and 3."
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
