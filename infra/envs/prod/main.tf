# Prod root: full sizing, multi-AZ, deletion protection, CloudFront+WAF
# (iac-conventions "Environments split"). Separate state key from staging in
# the same S3 bucket + DynamoDB lock table:
#   terraform init -backend-config="key=meterly/prod/terraform.tfstate" \
#                   -backend-config="bucket=<TF_STATE_BUCKET>" \
#                   -backend-config="dynamodb_table=<TF_LOCK_TABLE>" \
#                   -backend-config="region=<AWS_REGION>"
# deploy.yml applies staging automatically and prod only behind the GitHub
# `production` environment's required-reviewer rule.

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
      environment = "prod"
      service     = "meterly"
      managed-by  = "terraform"
    }
  }
}

locals {
  name_prefix = "meterly-prod"
}

module "network" {
  source = "../../modules/network"

  name_prefix             = local.name_prefix
  environment             = "prod"
  vpc_cidr_block          = "10.30.0.0/16"
  availability_zone_count = 2
}

module "data" {
  source = "../../modules/data"

  name_prefix              = local.name_prefix
  environment              = "prod"
  vpc_id                   = module.network.vpc_id
  private_subnet_ids       = module.network.private_subnet_ids
  rds_security_group_id    = module.network.rds_security_group_id
  redis_security_group_id  = module.network.redis_security_group_id
  db_instance_class        = "db.t4g.medium"
  db_multi_az              = true # required true in prod (iac-conventions production-scale defaults)
  redis_node_type          = "cache.t4g.small"
}

module "compute" {
  source = "../../modules/compute"

  name_prefix             = local.name_prefix
  environment             = "prod"
  vpc_id                  = module.network.vpc_id
  public_subnet_ids       = module.network.public_subnet_ids
  private_subnet_ids      = module.network.private_subnet_ids
  alb_security_group_id   = module.network.alb_security_group_id
  task_security_group_id  = module.network.task_security_group_id
  container_image         = var.container_image
  task_cpu                = 512
  task_memory             = 1024
  desired_count           = 2
  min_count               = 2
  max_count               = 6
  database_secret_arn     = module.data.database_secret_arn
  database_kms_key_arn    = module.data.kms_key_arn
  redis_primary_endpoint  = module.data.redis_primary_endpoint
}

module "observability" {
  source = "../../modules/observability"

  name_prefix              = local.name_prefix
  environment              = "prod"
  ecs_cluster_name         = module.compute.ecs_cluster_name
  ecs_service_name         = module.compute.ecs_service_name
  alb_arn_suffix           = module.compute.alb_arn_suffix
  target_group_arn_suffix  = module.compute.target_group_arn_suffix
  alert_email              = var.alert_email
  budget_monthly_limit_usd = 500
}

module "edge" {
  source = "../../modules/edge"

  name_prefix     = local.name_prefix
  environment     = "prod"
  enable_edge_waf = true # prod fronts the ALB with CloudFront + WAF
  alb_dns_name    = module.compute.alb_dns_name
  alb_arn         = module.compute.alb_arn
}

variable "aws_region" {
  description = "AWS region for prod."
  type        = string
  default     = "us-east-1"
}

variable "container_image" {
  description = "Fully qualified, immutable-tagged ECR image reference to deploy to prod."
  type        = string
}

variable "alert_email" {
  description = "Email address subscribed to prod's alarm/budget SNS topics."
  type        = string
}

output "alb_dns_name" {
  value = module.edge.cloudfront_domain_name
}

output "canary_alarm_names" {
  value = module.observability.canary_alarm_names
}
