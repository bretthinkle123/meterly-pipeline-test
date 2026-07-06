# Values other stages (deploy.yml, the DAST/load-campaign workflows) consume.

output "alb_dns_name" {
  description = "Public DNS name of the ALB (or CloudFront domain when edge WAF is enabled)."
  value       = var.enable_edge_waf ? module.edge.cloudfront_domain_name : module.compute.alb_dns_name
}

output "ecs_cluster_name" {
  description = "ECS cluster name — used by deploy.yml to target the service update."
  value       = module.compute.ecs_cluster_name
}

output "ecs_service_name" {
  description = "ECS service name — used by deploy.yml's canary rollout."
  value       = module.compute.ecs_service_name
}

output "database_secret_arn" {
  description = "Secrets Manager ARN holding the RDS connection string (read by the app's secrets facade)."
  value       = module.data.database_secret_arn
  sensitive   = true
}

output "canary_alarm_names" {
  description = "The minimum-three canary alarms deploy.yml watches for auto-rollback, plus the SLO burn-rate alarms."
  value       = module.observability.canary_alarm_names
}
