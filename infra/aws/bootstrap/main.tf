# One-time AWS setup for Kite's infrastructure workflow (.github/workflows/infra.yml), run once by the
# account owner with their own credentials (local state, nothing secret is created):
#   * an S3 bucket for the Terraform state of infra/aws (versioned, encrypted, private; locking with a
#     lock file in the bucket, no DynamoDB table);
#   * GitHub's OIDC identity provider (skip with create_oidc_provider=false if the account has it);
#   * a role GitHub Actions assumes from this repository's "aws" environment only: no AWS key is ever
#     stored in GitHub.
#
#   terraform -chdir=infra/aws/bootstrap init
#   terraform -chdir=infra/aws/bootstrap apply -var region=us-east-1
# Then put the outputs in the repository's variables (Settings > Secrets and variables > Actions >
# Variables): AWS_ROLE_ARN, AWS_REGION, TF_STATE_BUCKET.

terraform {
  required_version = ">= 1.10, < 2.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

variable "region" {
  description = "AWS region of the state bucket (and of Kite's machine)"
  type        = string
  default     = "us-east-1"
}

variable "github_repository" {
  description = "owner/name of the repository whose workflow may manage the infrastructure"
  type        = string
  default     = "23521549-lang/kite"
}

variable "github_environment" {
  description = "GitHub environment the infrastructure job runs in (add required reviewers to it if wanted)"
  type        = string
  default     = "aws"
}

variable "create_oidc_provider" {
  description = "false when the account already has token.actions.githubusercontent.com"
  type        = bool
  default     = true
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "kite" }
  }
}

data "aws_caller_identity" "me" {}

locals {
  owner = split("/", var.github_repository)[0]
  name  = split("/", var.github_repository)[1]
  oidc  = "token.actions.githubusercontent.com"
}

resource "aws_s3_bucket" "state" {
  bucket = "kite-tfstate-${data.aws_caller_identity.me.account_id}-${var.region}"
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_oidc_provider ? 1 : 0
  url            = "https://${local.oidc}"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 0 : 1
  url   = "https://${local.oidc}"
}

locals {
  oidc_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : data.aws_iam_openid_connect_provider.github[0].arn
}

data "aws_iam_policy_document" "trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.oidc_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc}:aud"
      values   = ["sts.amazonaws.com"]
    }
    # Both forms of GitHub's subject: by name, and with the owner's and repository's ids (repositories
    # created since July 2026).
    condition {
      test     = "StringLike"
      variable = "${local.oidc}:sub"
      values   = [
        "repo:${var.github_repository}:environment:${var.github_environment}",
        "repo:${local.owner}@*/${local.name}@*:environment:${var.github_environment}",
      ]
    }
  }
}

resource "aws_iam_role" "infra" {
  name                 = "kite-github-infra"
  assume_role_policy   = data.aws_iam_policy_document.trust.json
  max_session_duration = 3600
}

# What infra/aws needs: EC2 (instance, data volume, address, security group, key pair), the Ubuntu image
# id from SSM's public parameters, and the state bucket. No IAM: the machine gets no AWS role.
resource "aws_iam_role_policy_attachment" "ec2" {
  role       = aws_iam_role.infra.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2FullAccess"
}

data "aws_iam_policy_document" "infra" {
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.state.arn]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.state.arn}/*"]
  }
  statement {
    actions   = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = ["arn:aws:ssm:*::parameter/aws/service/canonical/*"]
  }
}

resource "aws_iam_role_policy" "infra" {
  name   = "kite-infra-state-and-images"
  role   = aws_iam_role.infra.id
  policy = data.aws_iam_policy_document.infra.json
}

output "AWS_ROLE_ARN" {
  value = aws_iam_role.infra.arn
}

output "TF_STATE_BUCKET" {
  value = aws_s3_bucket.state.bucket
}

output "AWS_REGION" {
  value = var.region
}
