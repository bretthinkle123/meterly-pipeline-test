# Staging root: shape-parity with prod (same modules, same topology) at
# reduced size and cost — single-AZ RDS allowed, no CloudFront/WAF
# (iac-conventions "Environments split"). Separate state key from prod in the
# same S3 bucket + DynamoDB lock table:
#   terraform init -backend-config="key=meterly/staging/terraform.tfstate" \
#                   -backend-config="bucket=<TF_STATE_BUCKET>" \
#                   -backend-config="dynamodb_table=<TF_LOCK_TABLE>" \
#                   -backend-config="region=<AWS_REGION>"
#
# This is a self-contained Terraform root (its own provider/backend) rather
# than a wrapper around `infra/` as a module — Terraform only allows a
# backend configuration in a root module, so each env owns its own root
# instead of nesting one root inside another.

terraform {
  backend "s3" {
    encrypt = true
  }

  required_version = ">= 1.7.0"

  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      environment = "staging"
      service     = "meterly"
      managed-by  = "terraform"
    }
  }
}

locals {
  name_prefix = "meterly-staging"
}

module "network" {
  source = "../../modules/network"

  name_prefix             = local.name_prefix
  environment             = "staging"
  vpc_cidr_block          = "10.20.0.0/16"
  availability_zone_count = 2
}

module "data" {
  source = "../../modules/data"

  name_prefix              = local.name_prefix
  environment              = "staging"
  vpc_id                   = module.network.vpc_id
  private_subnet_ids       = module.network.private_subnet_ids
  rds_security_group_id    = module.network.rds_security_group_id
  redis_security_group_id  = module.network.redis_security_group_id
  db_instance_class        = "db.t4g.micro"
  db_multi_az              = false # staging may run single-AZ (iac-conventions "shape-parity, not scale-parity")
  redis_node_type          = "cache.t4g.micro"
}

module "compute" {
  source = "../../modules/compute"

  name_prefix             = local.name_prefix
  environment             = "staging"
  vpc_id                  = module.network.vpc_id
  public_subnet_ids       = module.network.public_subnet_ids
  private_subnet_ids      = module.network.private_subnet_ids
  alb_security_group_id   = module.network.alb_security_group_id
  task_security_group_id  = module.network.task_security_group_id
  container_image         = var.container_image
  task_cpu                = 256
  task_memory             = 512
  desired_count           = 2
  min_count               = 2
  max_count               = 3
  database_secret_arn     = module.data.database_secret_arn
  database_kms_key_arn    = module.data.kms_key_arn
  redis_primary_endpoint  = module.data.redis_primary_endpoint
}

module "observability" {
  source = "../../modules/observability"

  name_prefix              = local.name_prefix
  environment              = "staging"
  ecs_cluster_name         = module.compute.ecs_cluster_name
  ecs_service_name         = module.compute.ecs_service_name
  alb_arn_suffix           = module.compute.alb_arn_suffix
  target_group_arn_suffix  = module.compute.target_group_arn_suffix
  alert_email              = var.alert_email
  budget_monthly_limit_usd = 150
}

module "edge" {
  source = "../../modules/edge"

  name_prefix     = local.name_prefix
  environment     = "staging"
  enable_edge_waf = false # staging skips CloudFront/WAF for cost (iac-conventions)
  alb_dns_name    = module.compute.alb_dns_name
  alb_arn         = module.compute.alb_arn
}

variable "aws_region" {
  description = "AWS region for staging."
  type        = string
  default     = "us-east-1"
}

variable "container_image" {
  description = "Fully qualified, immutable-tagged ECR image reference to deploy to staging."
  type        = string
}

variable "alert_email" {
  description = "Email address subscribed to staging's alarm/budget SNS topics."
  type        = string
}

output "alb_dns_name" {
  value = module.compute.alb_dns_name
}

output "canary_alarm_names" {
  value = module.observability.canary_alarm_names
}
