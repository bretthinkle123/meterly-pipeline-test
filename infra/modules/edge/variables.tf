variable "name_prefix" {
  description = "Resource name prefix (e.g. meterly-prod)."
  type        = string
}

variable "environment" {
  description = "Environment name (staging|prod)."
  type        = string
}

variable "enable_edge_waf" {
  description = "Whether to front the ALB with CloudFront + WAF (prod only)."
  type        = bool
}

variable "alb_dns_name" {
  description = "ALB DNS name to use as the CloudFront origin."
  type        = string
}

variable "alb_arn" {
  description = "ALB ARN (unused directly here, kept for a future WAF-on-ALB association)."
  type        = string
}
