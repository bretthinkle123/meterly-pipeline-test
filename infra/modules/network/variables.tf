variable "name_prefix" {
  description = "Resource name prefix (e.g. meterly-staging)."
  type        = string
}

variable "environment" {
  description = "Environment name, used in log group naming."
  type        = string
}

variable "vpc_cidr_block" {
  description = "CIDR block for the VPC."
  type        = string
}

variable "availability_zone_count" {
  description = "Number of AZs to spread subnets across."
  type        = number
}
