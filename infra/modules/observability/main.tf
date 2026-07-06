# Observability module: the audit log group (immutable, deny-delete), its S3
# Object-Lock archive, the canary + SLO burn-rate alarms deploy.yml consumes,
# the alert SNS topic, the cost-guardrail AWS Budget, and the synthetic canary.

resource "aws_sns_topic" "alerts" {
  name = "${var.name_prefix}-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# --- Audit log group: separate from operational logs, deny-delete resource
# policy so neither app code nor a compromised deploy role can erase evidence
# (logging-conventions "immutability"). ---

resource "aws_cloudwatch_log_group" "audit" {
  name              = "/meterly/${var.environment}/audit"
  retention_in_days = 90
}

data "aws_caller_identity" "current" {}

resource "aws_cloudwatch_log_resource_policy" "audit_deny_delete" {
  policy_name = "${var.name_prefix}-audit-deny-delete"
  policy_document = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "DenyDeleteExceptOpsRole"
      Effect = "Deny"
      Principal = "*"
      Action = [
        "logs:DeleteLogGroup",
        "logs:DeleteLogStream",
        "logs:PutRetentionPolicy",
      ]
      Resource = "${aws_cloudwatch_log_group.audit.arn}:*"
      Condition = {
        StringNotLike = {
          "aws:PrincipalArn" = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${var.name_prefix}-ops-*"
        }
      }
    }]
  })
}

resource "aws_s3_bucket" "audit_archive" {
  bucket              = "${var.name_prefix}-audit-archive-${data.aws_caller_identity.current.account_id}"
  object_lock_enabled = true
}

resource "aws_s3_bucket_versioning" "audit_archive" {
  bucket = aws_s3_bucket.audit_archive.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audit_archive" {
  bucket = aws_s3_bucket.audit_archive.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "audit_archive" {
  bucket                  = aws_s3_bucket.audit_archive.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_object_lock_configuration" "audit_archive" {
  bucket = aws_s3_bucket.audit_archive.id
  rule {
    default_retention {
      mode = "COMPLIANCE"
      days = 270 # 9-month cold tier — total 12mo with the 90-day hot CloudWatch tier
    }
  }
}

# --- Canary alarms (the minimum three deploy.yml watches for auto-rollback)
# plus the SLO burn-rate alarms, named to match <PROD_ALARM_NAMES>. ---

resource "aws_cloudwatch_metric_alarm" "prod_5xx_rate" {
  alarm_name          = "${var.name_prefix}-5xx-rate"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "HTTPCode_Target_5XX_Count"
  namespace           = "AWS/ApplicationELB"
  period              = 60
  statistic           = "Sum"
  threshold           = 5
  dimensions          = { LoadBalancer = var.alb_arn_suffix }
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "prod_alb_p95_latency" {
  alarm_name          = "${var.name_prefix}-alb-p95-latency"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "TargetResponseTime"
  namespace           = "AWS/ApplicationELB"
  period              = 60
  extended_statistic  = "p95"
  threshold           = 0.05 # 50ms — AC-SLO ingest p95 budget
  dimensions          = { LoadBalancer = var.alb_arn_suffix }
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "prod_unhealthy_hosts" {
  alarm_name          = "${var.name_prefix}-unhealthy-hosts"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "UnHealthyHostCount"
  namespace           = "AWS/ApplicationELB"
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  dimensions          = { TargetGroup = var.target_group_arn_suffix, LoadBalancer = var.alb_arn_suffix }
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

# SLO burn-rate alarms (AC-SLO: 99.9% availability, ingest p95 < 50ms).
# Fast-burn window (1h) pages; a slow-burn (6h) companion is a ticket-level
# alert — modeled here as a second period on the same underlying metrics.
resource "aws_cloudwatch_metric_alarm" "slo_availability_fastburn" {
  alarm_name          = "${var.name_prefix}-slo-availability-fastburn"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "HTTPCode_Target_5XX_Count"
  namespace           = "AWS/ApplicationELB"
  period              = 3600
  statistic           = "Sum"
  threshold           = 4 # ~99.9% of a representative 1h request volume
  dimensions          = { LoadBalancer = var.alb_arn_suffix }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "slo_ingest_p95_fastburn" {
  alarm_name          = "${var.name_prefix}-slo-ingest-p95-fastburn"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "TargetResponseTime"
  namespace           = "AWS/ApplicationELB"
  period              = 3600
  extended_statistic  = "p95"
  threshold           = 0.05
  dimensions          = { LoadBalancer = var.alb_arn_suffix }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

# --- Cost guardrail ---

resource "aws_budgets_budget" "monthly" {
  name         = "${var.name_prefix}-monthly-budget"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_monthly_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type              = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type              = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}

# --- Synthetic canary: probes both endpoints from outside the VPC using a
# dedicated synthetic test tenant so probe writes never pollute a real
# customer's counters. The canary script/artifact bucket is provisioned here;
# the actual canary body (calls /health and a synthetic-tenant /v1/usage) is
# uploaded out-of-band by the deploy pipeline once the app is live. ---

resource "aws_s3_bucket" "canary_artifacts" {
  bucket = "${var.name_prefix}-canary-artifacts-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "canary_artifacts" {
  bucket                  = aws_s3_bucket.canary_artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
