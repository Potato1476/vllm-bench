# The identity GitHub Actions uses to drive rollouts. No access key exists anywhere:
# the workflow trades its OIDC token for this role, for the length of one run.
#
# READ ONLY, and narrowly. The controller reads two things -- versions published under
# models/ and the reports the in-cluster Jobs leave under runs/rollout/ -- and writes
# nothing to AWS at all. Its only output is a pull request. So a leaked workflow can learn
# which model versions exist; it cannot publish one, delete one, or forge a report.
#
# In core, not cluster: the rollout state outlives every cluster, and the schedule keeps
# running while the cluster is down -- it just finds nothing to advance.

variable "github_repository" {
  description = "owner/repo whose workflows on main may assume the rollout role."
  type        = string
  default     = "Potato1476/vllm-bench"
}

variable "github_oidc_provider_arn" {
  description = <<-EOT
    ARN of an existing token.actions.githubusercontent.com OIDC provider. An account can
    hold only one per URL, so if another project already created it, pass it here; empty
    creates it.
  EOT
  type        = string
  default     = ""
}

variable "model_queue_arn" {
  description = "SQS queue the model publisher announces on. Empty: poll S3 only."
  type        = string
  default     = ""
}

resource "aws_iam_openid_connect_provider" "github" {
  count          = var.github_oidc_provider_arn == "" ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

locals {
  github_oidc_arn = (var.github_oidc_provider_arn != ""
    ? var.github_oidc_provider_arn
  : aws_iam_openid_connect_provider.github[0].arn)
}

data "aws_iam_policy_document" "github_rollout_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    # main only. A pull request's workflow runs the PR author's code; letting it assume
    # this role would hand the bucket listing to anyone who can open a PR on a public
    # repository. Schedule, dispatch and repository_dispatch all run on main.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:ref:refs/heads/main"]
    }
  }
}

data "aws_iam_policy_document" "github_rollout" {
  statement {
    sid       = "ListModelsAndReports"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.artifacts.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["models/*", "runs/rollout/*"]
    }
  }
  statement {
    sid     = "ReadModelsAndReports"
    actions = ["s3:GetObject"]
    # models/* because the CPU gates read manifest.json, the signature bundle and
    # config.json. Weights are never downloaded -- sizes come from the listing -- but
    # GetObject cannot be scoped by file type.
    resources = [
      "${aws_s3_bucket.artifacts.arn}/models/*",
      "${aws_s3_bucket.artifacts.arn}/runs/rollout/*",
    ]
  }
  dynamic "statement" {
    for_each = var.model_queue_arn == "" ? [] : [var.model_queue_arn]
    content {
      sid       = "DrainModelAnnouncements"
      actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
      resources = [statement.value]
    }
  }
}

resource "aws_iam_role" "github_rollout" {
  name                 = "${var.project}-github-rollout"
  assume_role_policy   = data.aws_iam_policy_document.github_rollout_trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "github_rollout" {
  name   = "rollout-read"
  role   = aws_iam_role.github_rollout.id
  policy = data.aws_iam_policy_document.github_rollout.json
}

output "github_rollout_role_arn" {
  description = "Set as the ROLLOUT_ROLE_ARN Actions variable (Settings > Variables)."
  value       = aws_iam_role.github_rollout.arn
}
