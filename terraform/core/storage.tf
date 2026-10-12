data "aws_caller_identity" "current" {}

locals {
  artifacts_bucket = "${var.project}-artifacts-${data.aws_caller_identity.current.account_id}"
}

# This bucket already exists -- it was created by hand before Terraform covered it. The
# import block below adopts it on the next apply instead of failing on a name conflict,
# so state and reality converge without anyone running a manual `terraform import`.
import {
  to = aws_s3_bucket.artifacts
  id = local.artifacts_bucket
}

# Holds model weights under models/, datasets under datasets/, benchmark results under
# runs/, and the Prometheus snapshot taken at the end of every session. Everything that
# has to survive `make lab-down` lives here.
resource "aws_s3_bucket" "artifacts" {
  bucket = local.artifacts_bucket
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

# Versioning keeps every overwritten object forever unless something removes it. Re-syncing
# 15 GB of weights twice would quietly triple the bill for this bucket.
resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    id     = "expire-noncurrent"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 7
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 3
    }
  }
}

# The Terraform state bucket is deliberately NOT managed here. A stack cannot own the
# place its own state lives; bootstrapping it by hand is the only order that works.

resource "aws_ecr_repository" "bench_runner" {
  name                 = "${var.project}/bench-runner"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

# ECR bills per GB-month and the runner image is large. Without this, six weeks of
# commit-tagged builds accumulate.
resource "aws_ecr_lifecycle_policy" "bench_runner" {
  repository = aws_ecr_repository.bench_runner.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the 5 most recent images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}

# The guardrail image packages the exact Python pipeline and retrieval corpus committed
# in this repository. Keeping it beside the runner in ECR makes the running policy
# traceable to an immutable image tag rather than copying source into a live pod.
resource "aws_ecr_repository" "guardrail" {
  name                 = "${var.project}/guardrail"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "guardrail" {
  repository = aws_ecr_repository.guardrail.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the 5 most recent images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}

# Quyen ghi model cho ben ghi o account khac (hop dong §7).
#
# Pham vi hep nhat ma `publish_model.py` can: ghi va doc lai duoi models/, liet ke bucket
# gioi han prefix models/. KHONG co DeleteObject, khong cham runs/ (bang chung do nghiem
# thu), datasets/ hay audit/.
#
# PutObject van cho phep GHI DE mot object da co -- S3 khong tach hai viec do. Hai thu
# chan hau qua: versioning dang bat nen ban goc con lay lai duoc, va init container bam
# lai tung file doi chieu manifest da ky moi lan pod khoi dong, nen mot ban ghi de khong
# ky duoc se lam pod khong len thay vi duoc phuc vu.
#
# Object Ownership cua bucket la BucketOwnerEnforced, nen object ho ghi thuoc ve account
# minh -- khong can ho dat ACL gi.
data "aws_iam_policy_document" "artifacts_bucket" {
  count = length(var.model_publisher_principals) > 0 ? 1 : 0

  statement {
    sid    = "ModelPublisherWriteModels"
    effect = "Allow"
    principals {
      type        = "AWS"
      identifiers = var.model_publisher_principals
    }
    actions = [
      "s3:PutObject",
      "s3:GetObject",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]
    resources = ["${aws_s3_bucket.artifacts.arn}/models/*"]
  }

  statement {
    sid    = "ModelPublisherListModelsPrefix"
    effect = "Allow"
    principals {
      type        = "AWS"
      identifiers = var.model_publisher_principals
    }
    # Chi ListBucket. `s3:prefix` khong ap dung cho ListBucketMultipartUploads, nen de
    # chung mot statement thi PutBucketPolicy tra ve MalformedPolicy "Conditions do not
    # apply to combination of actions and resources". Tach ra thi phai cap khong dieu
    # kien, va ben ghi khong can no: do la quyen DO upload dang do, con bucket la cua
    # minh nen minh do duoc.
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.artifacts.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["models/*", "models/"]
    }
  }
}

resource "aws_s3_bucket_policy" "artifacts" {
  count  = length(var.model_publisher_principals) > 0 ? 1 : 0
  bucket = aws_s3_bucket.artifacts.id
  policy = data.aws_iam_policy_document.artifacts_bucket[0].json
}
