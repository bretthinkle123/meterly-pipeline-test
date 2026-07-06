variable "name_prefix" {
  description = "Resource name prefix (e.g. meterly-staging)."
  type        = string
}

variable "environment" {
  description = "Environment name (staging|prod)."
  type        = string
}

variable "ecs_cluster_name" {
  description = "ECS cluster name (alarm dimension)."
  type        = string
}

variable "ecs_service_name" {
  description = "ECS service name (alarm dimension)."
  type        = string
}

variable "alb_arn_suffix" {
  description = "ALB ARN suffix (alarm dimension)."
  type        = string
}

variable "target_group_arn_suffix" {
  description = "Target group ARN suffix (alarm dimension)."
  type        = string
}

variable "alert_email" {
  description = "Email address subscribed to the alarm/budget SNS topics."
  type        = string
}

variable "budget_monthly_limit_usd" {
  description = "Monthly AWS Budget ceiling for this environment."
  type        = number
}
