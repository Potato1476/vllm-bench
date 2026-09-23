variable "project" {
  description = "Resource name prefix."
  type        = string
  default     = "vllm-bench"
}

variable "project_tag" {
  description = "Value of the project cost-allocation tag."
  type        = string
  default     = "da51"
}

variable "owner" {
  description = "Value of the owner tag."
  type        = string
  default     = "bao"
}

variable "database_name" {
  description = "Initial PostgreSQL database used by LiteLLM."
  type        = string
  default     = "litellm"

  validation {
    condition     = can(regex("^[A-Za-z][A-Za-z0-9_]{0,62}$", var.database_name))
    error_message = "database_name must be a valid PostgreSQL identifier up to 63 characters."
  }
}

variable "master_username" {
  description = "Bootstrap administrator; Aurora stores and rotates its password in Secrets Manager."
  type        = string
  default     = "litellm_admin"
}

variable "instance_class" {
  description = "Aurora instance class. db.t4g.medium is the cost-conscious lab default."
  type        = string
  default     = "db.t4g.medium"
}

variable "instance_count" {
  description = "Aurora instances: 2 gives one writer and one failover reader; 1 is a non-HA lab override."
  type        = number
  default     = 2

  validation {
    condition     = contains([1, 2], var.instance_count)
    error_message = "instance_count must be 1 or 2."
  }
}

variable "backup_retention_days" {
  description = "Automated backup retention."
  type        = number
  default     = 7

  validation {
    condition     = var.backup_retention_days >= 1 && var.backup_retention_days <= 35
    error_message = "backup_retention_days must be between 1 and 35."
  }
}

variable "deletion_protection" {
  description = "Protect the stateful cluster from accidental deletion. Keep true outside deliberate teardown."
  type        = bool
  default     = true
}

variable "skip_final_snapshot" {
  description = "Only set true for an explicitly disposable lab database."
  type        = bool
  default     = false
}
