output "canary_alarm_names" {
  description = "The minimum-three canary alarms plus the SLO burn-rate alarms, referenced by deploy.yml's <PROD_ALARM_NAMES>."
  value = [
    aws_cloudwatch_metric_alarm.prod_5xx_rate.alarm_name,
    aws_cloudwatch_metric_alarm.prod_alb_p95_latency.alarm_name,
    aws_cloudwatch_metric_alarm.prod_unhealthy_hosts.alarm_name,
    aws_cloudwatch_metric_alarm.slo_availability_fastburn.alarm_name,
    aws_cloudwatch_metric_alarm.slo_ingest_p95_fastburn.alarm_name,
  ]
}

output "alerts_sns_topic_arn" {
  description = "SNS topic ARN alarms/budgets publish to."
  value       = aws_sns_topic.alerts.arn
}
