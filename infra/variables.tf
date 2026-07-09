# Typed root-module inputs. Each `envs/<environment>` instantiation supplies
# its own values — this file has no environment-specific defaults baked in
# beyond the ones that are genuinely safe everywhere (see `sensitive` note).

variable "environment" {
  description = "Deployment environment name (staging|prod) — used in resource naming/tagging."
  type        = string
  validation {
    condition     = contains(["staging", "prod"], var.environment)
    error_message = "environment must be one of: staging, prod."
  }
}

variable "aws_region" {
  description = "AWS region to provision into."
  type        = string
  default     = "us-east-1"
}

variable "vpc_cidr_block" {
  description = "CIDR block for the Meterly VPC."
  type        = string
  default     = "10.20.0.0/16"
}

variable "availability_zone_count" {
  description = "Number of AZs to spread public/private subnets across (>= 2 per iac-conventions)."
  type        = number
  default     = 2
}

variable "ecs_task_cpu" {
  description = "Fargate task vCPU units."
  type        = number
  default     = 512
}

variable "ecs_task_memory" {
  description = "Fargate task memory (MiB)."
  type        = number
  default     = 1024
}

variable "ecs_desired_count" {
  description = "Desired ECS service task count (>= 2, no single point of failure)."
  type        = number
  default     = 2
  validation {
    condition     = var.ecs_desired_count >= 2
    error_message = "ecs_desired_count must be >= 2 for a production-scale service."
  }
}

variable "ecs_min_count" {
  description = "Minimum task count for target-tracking autoscaling."
  type        = number
  default     = 2
}

variable "ecs_max_count" {
  description = "Maximum task count for target-tracking autoscaling."
  type        = number
  default     = 6
}

variable "container_image" {
  description = "Fully qualified, immutable-tagged ECR image reference (digest or immutable tag) to deploy."
  type        = string
}

variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.medium"
}

variable "db_multi_az" {
  description = "Whether RDS runs Multi-AZ (synchronous standby). Required true in prod."
  type        = bool
  default     = true
}

variable "redis_node_type" {
  description = "ElastiCache Redis node type."
  type        = string
  default     = "cache.t4g.small"
}

variable "enable_edge_waf" {
  description = "Whether to front the ALB with CloudFront + WAF (prod only, cost-gated in staging)."
  type        = bool
  default     = false
}

variable "alert_email" {
  description = "Email address subscribed to the cost-guardrail and canary-alarm SNS topics."
  type        = string
}

variable "budget_monthly_limit_usd" {
  description = "Monthly AWS Budget ceiling for this environment (cost guardrail)."
  type        = number
  default     = 500
}
