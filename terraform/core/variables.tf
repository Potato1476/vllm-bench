variable "region" {
  description = "Lab region. us-east-1 has the lowest GPU price and the best G-family capacity."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Resource name prefix. Kept as vllm-bench so existing buckets are not orphaned."
  type        = string
  default     = "vllm-bench"
}

variable "project_tag" {
  description = "Value of the `project` cost-allocation tag, used to filter in Cost Explorer."
  type        = string
  default     = "da51"
}

variable "owner" {
  description = "Value of the `owner` tag: bao or minh."
  type        = string
  default     = "bao"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.0.0.0/16"
}

variable "budget_emails" {
  description = "Addresses that receive the 50/100/150/180 USD budget alerts. Both team members."
  type        = list(string)
  default     = []
}

variable "budget_limit_usd" {
  description = "Monthly budget ceiling. Alerts fire at fixed dollar thresholds below it."
  type        = number
  default     = 200
}

# Ai duoc ghi model vao bucket, tu account khac.
#
# De trong trong git va dat trong terraform.tfvars (da gitignore): day la ARN cua account
# NGUOI KHAC. Repo nay cong khai, va so account cua doi tac khong phai thu minh dua len
# thay ho.
variable "model_publisher_principals" {
  description = "ARN cac principal cross-account duoc ghi vao models/. Rong = khong mo."
  type        = list(string)
  default     = []
}
