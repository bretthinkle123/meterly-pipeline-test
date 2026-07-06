output "cloudfront_domain_name" {
  description = "CloudFront distribution domain name (empty string when WAF/CDN is disabled, e.g. staging)."
  value       = var.enable_edge_waf ? aws_cloudfront_distribution.this[0].domain_name : ""
}
