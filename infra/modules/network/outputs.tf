output "vpc_id" {
  description = "The VPC id."
  value       = aws_vpc.this.id
}

output "public_subnet_ids" {
  description = "Public subnet ids (ALB)."
  value       = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  description = "Private subnet ids (Fargate tasks, RDS, Redis)."
  value       = aws_subnet.private[*].id
}

output "alb_security_group_id" {
  description = "Security group id for the ALB."
  value       = aws_security_group.alb.id
}

output "task_security_group_id" {
  description = "Security group id for the Fargate tasks."
  value       = aws_security_group.task.id
}

output "rds_security_group_id" {
  description = "Security group id for RDS."
  value       = aws_security_group.rds.id
}

output "redis_security_group_id" {
  description = "Security group id for ElastiCache Redis."
  value       = aws_security_group.redis.id
}
