variable "name_prefix" {
  description = "Resource name prefix (e.g. meterly-staging)."
  type        = string
}

variable "environment" {
  description = "Environment name (staging|prod)."
  type        = string
}

variable "vpc_id" {
  description = "VPC id to provision the DB/cache subnet groups into."
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnet ids for the RDS/ElastiCache subnet groups."
  type        = list(string)
}

variable "rds_security_group_id" {
  description = "Security group id (from the network module) allowing 5432 from the task SG only."
  type        = string
}

variable "redis_security_group_id" {
  description = "Security group id (from the network module) allowing 6379 from the task SG only."
  type        = string
}

variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
}

variable "db_multi_az" {
  description = "Whether RDS runs Multi-AZ (synchronous standby)."
  type        = bool
}

variable "redis_node_type" {
  description = "ElastiCache Redis node type."
  type        = string
}
