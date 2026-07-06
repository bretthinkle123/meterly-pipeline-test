# Edge module: CloudFront -> AWS WAF (prod only). Staging skips this for
# cost — the app-level Tier-1/Tier-2 rate limiting still applies there
# (iac-conventions "prod edge / WAF").

resource "aws_wafv2_web_acl" "this" {
  count = var.enable_edge_waf ? 1 : 0

  name        = "${var.name_prefix}-waf"
  description = "Managed core + known-bad-inputs rule groups fronting the Meterly ALB."
  scope       = "CLOUDFRONT"

  default_action {
    allow {}
  }

  rule {
    name     = "AWSManagedRulesCommonRuleSet"
    priority = 0
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                 = "${var.name_prefix}-common-rule-set"
      sampled_requests_enabled    = true
    }
  }

  rule {
    name     = "AWSManagedRulesKnownBadInputsRuleSet"
    priority = 1
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
        vendor_name = "AWS"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                 = "${var.name_prefix}-known-bad-inputs"
      sampled_requests_enabled    = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                 = "${var.name_prefix}-waf"
    sampled_requests_enabled    = true
  }
}

resource "aws_cloudfront_distribution" "this" {
  count   = var.enable_edge_waf ? 1 : 0
  enabled = true
  web_acl_id = aws_wafv2_web_acl.this[0].arn

  origin {
    domain_name = var.alb_dns_name
    origin_id   = "${var.name_prefix}-alb-origin"

    custom_origin_config {
      http_port                = 80
      https_port               = 443
      origin_protocol_policy   = "https-only"
      origin_ssl_protocols     = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    allowed_methods        = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods          = ["GET", "HEAD"]
    target_origin_id        = "${var.name_prefix}-alb-origin"
    viewer_protocol_policy   = "redirect-to-https"

    forwarded_values {
      query_string = true
      headers      = ["Authorization", "Idempotency-Key", "Content-Type"]
      cookies {
        forward = "none"
      }
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}
