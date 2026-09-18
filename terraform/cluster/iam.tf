module "ebs_csi_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.44"

  role_name             = "${var.cluster_name}-ebs-csi"
  attach_ebs_csi_policy = true

  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["kube-system:ebs-csi-controller-sa"]
    }
  }
}

# The vLLM pods read model weights from S3 at startup. Read-only, and scoped to the
# models/ prefix: the engine under test must not be able to write anything, least of all
# the benchmark results that grade it.
resource "aws_iam_policy" "vllm_s3_models" {
  name        = "${var.cluster_name}-vllm-s3-models"
  description = "Read-only access to model weights under models/ in the artifacts bucket."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ListModelsPrefixOnly"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [local.artifacts_arn]
        # ListBucket is a bucket-level action, so the prefix restriction has to come from
        # a condition. Without it this would enumerate the whole bucket, including runs/.
        Condition = {
          StringLike = { "s3:prefix" = ["models/*"] }
        }
      },
      {
        Sid      = "ReadModelObjects"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = ["${local.artifacts_arn}/models/*"]
      },
    ]
  })
}

module "vllm_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.44"

  role_name        = "${var.cluster_name}-vllm"
  role_policy_arns = { s3 = aws_iam_policy.vllm_s3_models.arn }

  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["inference:vllm"]
    }
  }
}

# The load generator reads datasets and writes results. Separate from the vLLM role so
# that a compromised or buggy engine cannot touch the numbers, and vice versa.
resource "aws_iam_policy" "bench_runner_s3" {
  name        = "${var.cluster_name}-bench-runner-s3"
  description = "Read datasets, write benchmark runs and metric snapshots."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ListArtifactsBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [local.artifacts_arn]
      },
      {
        Sid    = "ReadDatasetsAndModels"
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = [
          "${local.artifacts_arn}/datasets/*",
          "${local.artifacts_arn}/models/*",
        ]
      },
      {
        Sid    = "WriteResults"
        Effect = "Allow"
        Action = ["s3:PutObject"]
        Resource = [
          "${local.artifacts_arn}/runs/*",
          "${local.artifacts_arn}/metrics/*",
        ]
      },
    ]
  })
}

module "bench_runner_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.44"

  role_name        = "${var.cluster_name}-bench-runner"
  role_policy_arns = { s3 = aws_iam_policy.bench_runner_s3.arn }

  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["benchmark:bench-runner"]
    }
  }
}
