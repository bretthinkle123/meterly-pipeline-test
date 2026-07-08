variable "name_prefix" {
  description = "Resource name prefix (e.g. meterly-staging)."
  type        = string
}

variable "environment" {
  description = "Environment name (staging|prod)."
  type        = string
}

variable "vpc_id" {
  description = "VPC id."
  type        = string
}

variable "public_subnet_ids" {
  description = "Public subnet ids for the ALB."
  type        = list(string)
}

variable "private_subnet_ids" {
  description = "Private subnet ids for the Fargate tasks."
  type        = list(string)
}

variable "alb_security_group_id" {
  description = "Security group id (from the network module) for the ALB."
  type        = string
}

variable "task_security_group_id" {
  description = "Security group id (from the network module) for the Fargate tasks."
  type        = string
}

variable "container_image" {
  description = "Fully qualified, immutable-tagged ECR image reference to deploy."
  type        = string
}

variable "task_cpu" {
  description = "Fargate task vCPU units."
  type        = number
}

variable "task_memory" {
  description = "Fargate task memory (MiB)."
  type        = number
}

variable "desired_count" {
  description = "Desired ECS service task count."
  type        = number
}

variable "min_count" {
  description = "Minimum task count for autoscaling."
  type        = number
}

variable "max_count" {
  description = "Maximum task count for autoscaling."
  type        = number
}

variable "database_secret_arn" {
  description = "Secrets Manager ARN the task role may read the DB credential from."
  type        = string
}

variable "database_kms_key_arn" {
  description = "KMS CMK ARN the task role may call kms:Decrypt on."
  type        = string
}

variable "redis_primary_endpoint" {
  description = "ElastiCache Redis primary endpoint, passed to the app as config (not a secret)."
  type        = string
}

variable "dashboard_reader_secret_arn" {
  description = "Secrets Manager ARN the task role may read the dashboard-reader credential from."
  type        = string
}
