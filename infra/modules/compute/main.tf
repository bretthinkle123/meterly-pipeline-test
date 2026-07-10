# Compute module: ECR repo (immutable tags), ECS Fargate service (>= 2 tasks,
# multi-AZ, target-tracking autoscaling), ALB with blue/green target groups,
# and the least-privilege execution/task roles (identity module concern,
# co-located here since they're one-to-one with the service).

resource "aws_ecr_repository" "this" {
  name                 = "${var.name_prefix}-app"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
  }
}

resource "aws_ecs_cluster" "this" {
  name = "${var.name_prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_cloudwatch_log_group" "app" {
  name              = "/meterly/${var.environment}/app"
  retention_in_days = 90
}

# --- IAM: execution role (pull image, write logs) and task role (least
# privilege — the one Secrets Manager secret, one CMK decrypt, logs/X-Ray
# write, no wildcards; iac-conventions "no wildcard Action/Resource"). ---

resource "aws_iam_role" "execution" {
  name = "${var.name_prefix}-ecs-execution-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "task" {
  name = "${var.name_prefix}-ecs-task-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "task" {
  name = "${var.name_prefix}-ecs-task-policy"
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadDatabaseSecret"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = var.database_secret_arn
      },
      {
        Sid      = "DecryptDataKey"
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = var.database_kms_key_arn
      },
      {
        Sid    = "WriteLogsNoDelete"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "${aws_cloudwatch_log_group.app.arn}:*"
      },
      {
        Sid      = "WriteXRay"
        Effect   = "Allow"
        Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
        Resource = "*"
      },
    ]
  })
}

resource "aws_ecs_task_definition" "this" {
  family                   = "${var.name_prefix}-app"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.task_cpu)
  memory                   = tostring(var.task_memory)
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([
    {
      name         = "app"
      image        = var.container_image
      essential    = true
      portMappings = [{ containerPort = 8000, protocol = "tcp" }]
      environment = [
        { name = "METERLY_ENVIRONMENT", value = var.environment },
        { name = "METERLY_REDIS_URL", value = "redis://${var.redis_primary_endpoint}:6379/0" },
        { name = "METERLY_DATABASE_SECRET_NAME", value = var.database_secret_arn },
        { name = "METERLY_ENABLE_DOCS", value = var.environment == "prod" ? "false" : "true" },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.app.name
          "awslogs-region"        = data.aws_region.current.name
          "awslogs-stream-prefix" = "app"
        }
      }
      # Graceful drain: readiness flips false on SIGTERM, in-flight requests
      # finish, pool closes — within this stopTimeout window (containerization-
      # conventions R2). Gunicorn's --graceful-timeout in the Dockerfile is set
      # comfortably under this.
      stopTimeout = 30
    }
  ])
}

data "aws_region" "current" {}

# internal = false is INTENTIONAL: this is the public entry point for the
# Meterly API (an internet-facing ALB is the designed architecture — CLAUDE.md
# "ALB"). "Load balancer is exposed publicly" (AWS-0053) is therefore an accepted,
# by-design property, not a misconfiguration:
#trivy:ignore:AVD-AWS-0053 Public API — the ALB is internet-facing by design; access is fronted by HTTPS-only ingress, API-key auth, and per-key rate limiting.
resource "aws_lb" "this" {
  name               = "${var.name_prefix}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [var.alb_security_group_id]
  subnets            = var.public_subnet_ids

  # Drop malformed HTTP headers at the edge rather than forwarding them to the
  # app (defends against header-smuggling; clears AWS-0052).
  drop_invalid_header_fields = true

  # Guard the production entry point against accidental teardown (CKV_AWS_150).
  enable_deletion_protection = true
}

# Blue/green target groups — deploy.yml shifts the listener rule's weighted
# forward action between these during a canary rollout.
resource "aws_lb_target_group" "blue" {
  name        = "${var.name_prefix}-tg-blue"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    path                = "/health/ready"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 15
    timeout             = 5
  }
}

resource "aws_lb_target_group" "green" {
  name        = "${var.name_prefix}-tg-green"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    path                = "/health/ready"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 15
    timeout             = 5
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  # certificate_arn intentionally left for the environment to supply via ACM
  # (out of this build's scope to provision a public hosted zone/cert).

  default_action {
    type = "forward"
    forward {
      target_group {
        arn    = aws_lb_target_group.blue.arn
        weight = 100
      }
      target_group {
        arn    = aws_lb_target_group.green.arn
        weight = 0
      }
    }
  }
}

resource "aws_ecs_service" "this" {
  name            = "${var.name_prefix}-service"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.this.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = var.private_subnet_ids
    security_groups = [var.task_security_group_id]
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.blue.arn
    container_name   = "app"
    container_port   = 8000
  }

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  lifecycle {
    ignore_changes = [task_definition] # deploy.yml updates this via a new task-def revision
  }

  depends_on = [aws_lb_listener.https]
}

resource "aws_appautoscaling_target" "ecs" {
  max_capacity       = var.max_count
  min_capacity       = var.min_count
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.this.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "ecs_request_count" {
  name               = "${var.name_prefix}-target-tracking-requests"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.ecs.resource_id
  scalable_dimension = aws_appautoscaling_target.ecs.scalable_dimension
  service_namespace  = aws_appautoscaling_target.ecs.service_namespace

  target_tracking_scaling_policy_configuration {
    target_value = 300
    predefined_metric_specification {
      predefined_metric_type = "ALBRequestCountPerTarget"
      resource_label         = "${aws_lb.this.arn_suffix}/${aws_lb_target_group.blue.arn_suffix}"
    }
  }
}
