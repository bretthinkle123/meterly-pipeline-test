# Root module: providers + composition of the single-purpose child modules.
# Nothing outside this file reaches into a child module's internals — the
# facade discipline `iac-conventions` requires.
#
# Dependency order (network owns all security groups so data/compute never
# form a circular module reference): network -> data -> compute ->
# observability -> edge.
#
# This file is a standalone reference composition (its own provider +
# `terraform init` target) for a single, undifferentiated environment.
# The actual deploy targets are the self-contained roots under `envs/staging`
# and `envs/prod` (Terraform only allows a `backend` block in a root module,
# so each env owns its own root rather than nesting this one inside theirs —
# see the comment at the top of each `envs/*/main.tf`). `modules/*` is the
# single source of truth both this file and the envs compose identically.

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      environment = var.environment
      service     = "meterly"
      managed-by  = "terraform"
    }
  }
}

locals {
  name_prefix = "meterly-${var.environment}"
}

module "network" {
  source = "./modules/network"

  name_prefix             = local.name_prefix
  environment             = var.environment
  vpc_cidr_block          = var.vpc_cidr_block
  availability_zone_count = var.availability_zone_count
}

module "data" {
  source = "./modules/data"

  name_prefix             = local.name_prefix
  environment             = var.environment
  vpc_id                  = module.network.vpc_id
  private_subnet_ids      = module.network.private_subnet_ids
  rds_security_group_id   = module.network.rds_security_group_id
  redis_security_group_id = module.network.redis_security_group_id
  db_instance_class       = var.db_instance_class
  db_multi_az             = var.db_multi_az
  redis_node_type         = var.redis_node_type
}

module "compute" {
  source = "./modules/compute"

  name_prefix            = local.name_prefix
  environment            = var.environment
  vpc_id                 = module.network.vpc_id
  public_subnet_ids      = module.network.public_subnet_ids
  private_subnet_ids     = module.network.private_subnet_ids
  alb_security_group_id  = module.network.alb_security_group_id
  task_security_group_id = module.network.task_security_group_id
  container_image        = var.container_image
  task_cpu               = var.ecs_task_cpu
  task_memory            = var.ecs_task_memory
  desired_count          = var.ecs_desired_count
  min_count              = var.ecs_min_count
  max_count              = var.ecs_max_count
  database_secret_arn    = module.data.database_secret_arn
  database_kms_key_arn   = module.data.kms_key_arn
  redis_primary_endpoint = module.data.redis_primary_endpoint
  dashboard_reader_secret_arn = module.data.dashboard_reader_secret_arn
}

module "observability" {
  source = "./modules/observability"

  name_prefix              = local.name_prefix
  environment              = var.environment
  ecs_cluster_name         = module.compute.ecs_cluster_name
  ecs_service_name         = module.compute.ecs_service_name
  alb_arn_suffix           = module.compute.alb_arn_suffix
  target_group_arn_suffix  = module.compute.target_group_arn_suffix
  alert_email              = var.alert_email
  budget_monthly_limit_usd = var.budget_monthly_limit_usd
}

module "edge" {
  source = "./modules/edge"

  name_prefix     = local.name_prefix
  environment     = var.environment
  enable_edge_waf = var.enable_edge_waf
  alb_dns_name    = module.compute.alb_dns_name
  alb_arn         = module.compute.alb_arn
}
