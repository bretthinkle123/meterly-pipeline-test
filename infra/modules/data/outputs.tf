output "database_secret_arn" {
  description = "Secrets Manager ARN holding the app's (non-superuser) DATABASE_URL."
  value       = aws_secretsmanager_secret.app_database_url.arn
}

output "kms_key_arn" {
  description = "The data-tier KMS CMK ARN (RDS storage + Secrets Manager envelope)."
  value       = aws_kms_key.data.arn
}

output "redis_primary_endpoint" {
  description = "ElastiCache Redis primary endpoint address."
  value       = aws_elasticache_replication_group.this.primary_endpoint_address
}

output "db_instance_endpoint" {
  description = "RDS instance endpoint (address:port)."
  value       = aws_db_instance.this.endpoint
}
