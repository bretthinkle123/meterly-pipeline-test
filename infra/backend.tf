# Remote state: one S3 bucket (SSE-encrypted) + one DynamoDB lock table,
# shared across environments via separate state keys (iac-conventions).
# Bucket/table names and region are supplied via `-backend-config` at
# `terraform init` time (never hardcoded) so the same root module works for
# every environment without editing this file.
terraform {
  backend "s3" {
    encrypt = true
    # key            = "meterly/<environment>/terraform.tfstate"  (per-env, via -backend-config)
    # bucket         = "<TF_STATE_BUCKET>"                        (via -backend-config)
    # dynamodb_table = "<TF_LOCK_TABLE>"                          (via -backend-config)
    # region         = "<AWS_REGION>"                             (via -backend-config)
  }

  required_version = ">= 1.7.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}
