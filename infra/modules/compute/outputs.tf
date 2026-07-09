output "ecs_cluster_name" {
  description = "ECS cluster name — deploy.yml targets this for the service update."
  value       = aws_ecs_cluster.this.name
}

output "ecs_service_name" {
  description = "ECS service name."
  value       = aws_ecs_service.this.name
}

output "alb_dns_name" {
  description = "ALB public DNS name."
  value       = aws_lb.this.dns_name
}

output "alb_arn" {
  description = "ALB ARN (consumed by the edge module for CloudFront origin config)."
  value       = aws_lb.this.arn
}

output "alb_arn_suffix" {
  description = "ALB ARN suffix — used by the observability module's CloudWatch alarm dimensions."
  value       = aws_lb.this.arn_suffix
}

output "target_group_arn_suffix" {
  description = "Blue target group ARN suffix — used by the canary alarms."
  value       = aws_lb_target_group.blue.arn_suffix
}

output "task_security_group_id" {
  description = "Re-exported for convenience — the source of truth is the network module."
  value       = var.task_security_group_id
}
