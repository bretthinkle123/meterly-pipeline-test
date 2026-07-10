# Network module: VPC, public subnets (ALB) + private subnets (Fargate/RDS/
# Redis) across >= 2 AZs, and the VPC flow logs the IaC baseline requires.

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr_block
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${var.name_prefix}-vpc" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.name_prefix}-igw" }
}

resource "aws_subnet" "public" {
  count                   = var.availability_zone_count
  vpc_id                  = aws_vpc.this.id
  cidr_block              = cidrsubnet(var.vpc_cidr_block, 8, count.index)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = false

  tags = { Name = "${var.name_prefix}-public-${count.index}" }
}

resource "aws_subnet" "private" {
  count             = var.availability_zone_count
  vpc_id            = aws_vpc.this.id
  cidr_block        = cidrsubnet(var.vpc_cidr_block, 8, count.index + var.availability_zone_count)
  availability_zone = data.aws_availability_zones.available.names[count.index]

  tags = { Name = "${var.name_prefix}-private-${count.index}" }
}

resource "aws_eip" "nat" {
  count  = var.availability_zone_count
  domain = "vpc"
  tags   = { Name = "${var.name_prefix}-nat-eip-${count.index}" }
}

resource "aws_nat_gateway" "this" {
  count         = var.availability_zone_count
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id
  tags          = { Name = "${var.name_prefix}-nat-${count.index}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = { Name = "${var.name_prefix}-public-rt" }
}

resource "aws_route_table_association" "public" {
  count          = var.availability_zone_count
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "private" {
  count  = var.availability_zone_count
  vpc_id = aws_vpc.this.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.this[count.index].id
  }
  tags = { Name = "${var.name_prefix}-private-rt-${count.index}" }
}

resource "aws_route_table_association" "private" {
  count          = var.availability_zone_count
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

# VPC flow logs — audit/resilience baseline item (iac-conventions).
resource "aws_flow_log" "this" {
  vpc_id               = aws_vpc.this.id
  traffic_type         = "ALL"
  log_destination_type = "cloud-watch-logs"
  log_destination      = aws_cloudwatch_log_group.flow_logs.arn
  iam_role_arn         = aws_iam_role.flow_logs.arn
}

resource "aws_cloudwatch_log_group" "flow_logs" {
  name              = "/meterly/${var.environment}/vpc-flow-logs"
  retention_in_days = 90
}

resource "aws_iam_role" "flow_logs" {
  name = "${var.name_prefix}-flow-logs-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "vpc-flow-logs.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "flow_logs" {
  name = "${var.name_prefix}-flow-logs-policy"
  role = aws_iam_role.flow_logs.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "logs:CreateLogStream",
        "logs:PutLogEvents",
      ]
      Resource = "${aws_cloudwatch_log_group.flow_logs.arn}:*"
    }]
  })
}

# --- Security groups (owned here, not in compute/data, so those two modules
# never need to reference each other and no circular module dependency can
# form — iac-conventions "never 0.0.0.0/0 except the public ALB" baseline). ---

resource "aws_security_group" "alb" {
  name        = "${var.name_prefix}-alb-sg"
  description = "ALB: HTTPS from the internet only."
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "HTTPS from the internet"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # The ALB only forwards to / health-checks the Fargate tasks (app port 8000),
  # all inside this VPC. Scope egress to the VPC CIDR on 8000 rather than
  # 0.0.0.0/0 (clears AWS-0104). A CIDR (not the task SG id) is used so no
  # circular alb<->task security-group reference forms.
  egress {
    description = "Forward / health-check app tasks in this VPC only"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr_block]
  }

  tags = { Name = "${var.name_prefix}-alb-sg" }
}

resource "aws_security_group" "task" {
  name        = "${var.name_prefix}-task-sg"
  description = "Fargate tasks: app port reachable from the ALB only."
  vpc_id      = aws_vpc.this.id

  ingress {
    description     = "App port from the ALB"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  # In-VPC egress: the tasks reach RDS (5432) and Redis (6379), both in this VPC.
  # Scoped to the VPC CIDR (clears AWS-0104); a CIDR avoids a circular
  # task<->rds / task<->redis security-group reference.
  egress {
    description = "Reach in-VPC data stores (RDS 5432 / Redis 6379)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.vpc_cidr_block]
  }

  # Outbound HTTPS to the internet via NAT — a genuine need: ECR image pull,
  # Secrets Manager, CloudWatch Logs, X-Ray, Sentry, and the OTLP exporter, none
  # fronted by a VPC endpoint in this build's scope. Narrowed from all-ports/all-
  # protocols to 443/tcp only (no arbitrary-port exfil path). The residual
  # 0.0.0.0/0 on 443 is an ACCEPTED RISK, not a fixable misconfig here:
  #trivy:ignore:AVD-AWS-0104 The tasks require outbound HTTPS to public AWS/SaaS endpoints (ECR, Secrets Manager, CloudWatch, X-Ray, Sentry) via NAT; no VPC endpoints are provisioned in scope. Egress is restricted to 443/tcp.
  egress {
    description = "Outbound HTTPS to external services via NAT (ECR, Secrets Manager, CloudWatch, X-Ray, Sentry, OTLP)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.name_prefix}-task-sg" }
}

resource "aws_security_group" "rds" {
  name        = "${var.name_prefix}-rds-sg"
  description = "RDS PostgreSQL: reachable from the task SG only, never 0.0.0.0/0."
  vpc_id      = aws_vpc.this.id

  ingress {
    description     = "Postgres from the app tasks"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.task.id]
  }

  # RDS PostgreSQL never initiates outbound connections (SGs are stateful, so
  # replies to the app's inbound queries need no egress rule). Egress is confined
  # to the VPC CIDR on the Postgres port instead of 0.0.0.0/0 — clears the
  # unrestricted-egress finding (AWS-0104) at the source rather than waiving it.
  egress {
    description = "In-VPC only (never 0.0.0.0/0); Postgres wire protocol"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr_block]
  }

  tags = { Name = "${var.name_prefix}-rds-sg" }
}

resource "aws_security_group" "redis" {
  name        = "${var.name_prefix}-redis-sg"
  description = "ElastiCache Redis: reachable from the task SG only, never 0.0.0.0/0."
  vpc_id      = aws_vpc.this.id

  ingress {
    description     = "Redis from the app tasks"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.task.id]
  }

  # ElastiCache Redis never initiates outbound connections either. Egress is
  # confined to the VPC CIDR on the Redis port instead of 0.0.0.0/0 — clears
  # AWS-0104 at the source.
  egress {
    description = "In-VPC only (never 0.0.0.0/0); Redis protocol"
    from_port   = 6379
    to_port     = 6379
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr_block]
  }

  tags = { Name = "${var.name_prefix}-redis-sg" }
}
