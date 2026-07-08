# Data module: RDS PostgreSQL (events/usage_rollup/api_keys), the app's KMS
# CMK, the Secrets Manager entries the app's secrets facade reads at
# runtime, and ElastiCache Redis for the rate-limit token buckets.

resource "aws_kms_key" "data" {
  description             = "${var.name_prefix} CMK — RDS storage encryption + Secrets Manager envelope"
  deletion_window_in_days = 30
  enable_key_rotation     = true
}

resource "aws_kms_alias" "data" {
  name          = "alias/${var.name_prefix}-data"
  target_key_id = aws_kms_key.data.key_id
}

resource "aws_db_subnet_group" "this" {
  name       = "${var.name_prefix}-db-subnets"
  subnet_ids = var.private_subnet_ids
}

# RDS-managed master-user credential — used only for the one-time app-role
# bootstrap (below) and by the migration job; the running application never
# uses the master credential (least privilege).
resource "aws_db_instance" "this" {
  identifier     = "${var.name_prefix}-postgres"
  engine         = "postgres"
  engine_version = "16.4"
  instance_class = var.db_instance_class

  allocated_storage     = 50
  max_allocated_storage = 200
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.data.arn

  db_name  = "meterly"
  username = "meterly_admin"
  manage_master_user_password   = true
  master_user_secret_kms_key_id = aws_kms_key.data.arn

  multi_az               = var.db_multi_az
  publicly_accessible    = false
  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [var.rds_security_group_id]

  backup_retention_period = 7
  deletion_protection     = true
  skip_final_snapshot     = false
  final_snapshot_identifier = "${var.name_prefix}-postgres-final"

  tags = { Name = "${var.name_prefix}-postgres" }
}

# The application's own least-privilege Postgres role: no BYPASSRLS, so the
# RLS policies on events/usage_rollup (alembic 0001/0002) are an unbypassable
# backstop even if a repository query were ever missing its api_key_id filter
# (plan §"Row-level security", iac-conventions baseline "no BYPASSRLS").
resource "random_password" "app_db_password" {
  length  = 32
  special = false
}

resource "null_resource" "app_role_bootstrap" {
  # Runs once against the primary to create the non-superuser app role.
  # Prerequisite: the executing runner must have network line-of-sight to
  # the RDS private subnet (a VPC-attached CI runner or bastion/SSM tunnel)
  # — documented here rather than assumed, since GitHub-hosted runners have
  # no VPC access by default.
  triggers = {
    db_instance_id = aws_db_instance.this.id
  }

  provisioner "local-exec" {
    command = <<-EOT
      psql "host=${aws_db_instance.this.address} port=5432 dbname=meterly user=meterly_admin sslmode=require" \
        -v ON_ERROR_STOP=1 \
        -c "DO $$ BEGIN
              IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'meterly_app') THEN
                CREATE ROLE meterly_app LOGIN PASSWORD '${random_password.app_db_password.result}' NOBYPASSRLS;
              END IF;
            END $$;" \
        -c "GRANT CONNECT ON DATABASE meterly TO meterly_app;" \
        -c "GRANT USAGE, CREATE ON SCHEMA public TO meterly_app;" \
        -c "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO meterly_app;"
    EOT
  }

  depends_on = [aws_db_instance.this]
}

resource "aws_secretsmanager_secret" "app_database_url" {
  name       = "meterly/${var.environment}/database-url"
  kms_key_id = aws_kms_key.data.arn
}

resource "aws_secretsmanager_secret_version" "app_database_url" {
  secret_id = aws_secretsmanager_secret.app_database_url.id
  secret_string = "postgresql+asyncpg://meterly_app:${random_password.app_db_password.result}@${aws_db_instance.this.address}:5432/meterly"

  depends_on = [null_resource.app_role_bootstrap]
}

# Feature 3 (Usage Dashboard) — the BFF's server-held `dashboard-reader`
# credential. Terraform provisions the secret *container* only, encrypted
# with the existing data CMK (so no new KMS grant is needed on the task
# role); its real value is set out-of-band by `scripts/seed_api_key.py
# --write-to-secret` and `ignore_changes` stops a later `apply` from ever
# reverting that operator write back to the placeholder — the plaintext
# never lands in *.tfstate/*.tfvars (plan §Infrastructure, I-D1).
resource "aws_secretsmanager_secret" "dashboard_reader" {
  name       = "meterly/${var.environment}/dashboard-reader-key"
  kms_key_id = aws_kms_key.data.arn
}

resource "aws_secretsmanager_secret_version" "dashboard_reader" {
  secret_id     = aws_secretsmanager_secret.dashboard_reader.id
  secret_string = "REPLACED_OUT_OF_BAND_BY_scripts/seed_api_key.py"

  lifecycle {
    ignore_changes = [secret_string]
  }
}

resource "aws_elasticache_subnet_group" "this" {
  name       = "${var.name_prefix}-redis-subnets"
  subnet_ids = var.private_subnet_ids
}

resource "aws_elasticache_replication_group" "this" {
  replication_group_id = "${var.name_prefix}-redis"
  description           = "Meterly rate-limit token-bucket store"

  engine         = "redis"
  engine_version = "7.1"
  node_type      = var.redis_node_type

  num_cache_clusters = 2
  automatic_failover_enabled = true

  subnet_group_name  = aws_elasticache_subnet_group.this.name
  security_group_ids = [var.redis_security_group_id]

  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  kms_key_id                 = aws_kms_key.data.arn
}
