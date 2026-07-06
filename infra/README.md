# infra/

## Purpose

Infrastructure-as-code (Terraform) for AWS deployment: modular resources (network, compute, data, observability, edge), environment-specific configurations (staging, prod), and deployment automation.

## Modules

| Directory / Module | Responsibility |
|---|---|
| `modules/network/` | VPC, subnets (public and private, multi-AZ), NAT gateway, security groups. Foundational networking layer. |
| `modules/compute/` | ECS Fargate cluster, task definitions, auto-scaling, ALB target groups. Runs the Meterly app containers. |
| `modules/data/` | RDS PostgreSQL (multi-AZ with synchronous standby), ElastiCache Redis (for rate limiting), KMS keys (encryption at rest). Storage layer. |
| `modules/edge/` | CloudFront distribution (optional; for future CDN caching), WAF rules (if needed). Edge security layer. |
| `modules/observability/` | CloudWatch log groups, X-Ray sampling rules, SNS topics, SLO alarms (availability, p95 latency, error rate, unhealthy hosts). Monitoring and alerting. |
| `envs/staging/main.tf` | Staging environment root module: instantiates all modules at staging scale (1-2 ECS tasks, smaller RDS instance). Separate backend and state. |
| `envs/prod/main.tf` | Production environment root module: instantiates all modules at prod scale (≥2 ECS tasks multi-AZ, larger RDS with backups). Separate backend and state. |
| `main.tf` | Root module outputs and variable defaults (used by environments). |

## Relationships

**Module composition:**
- Each `modules/*` directory is a reusable Terraform module (encapsulates related resources).
- `envs/staging` and `envs/prod` are root modules that instantiate and wire the base modules with environment-specific variables.
- Both environments use the same module code but different variable values (e.g., instance sizes, replica counts, alarm thresholds).

**Public interfaces:**
- Each module exposes `variables.tf` (input parameters) and `outputs.tf` (outputs for other modules).
- E.g., `network` outputs the VPC ID and subnet IDs, which `compute` consumes as inputs.

**Infrastructure as code principles:**
- **Immutable infrastructure:** containers are built once, deployed many times (no SSH into instances for patching).
- **Declarative:** Terraform describes the desired state; it computes the diff and applies only what changed.
- **Version controlled:** all infrastructure code is in this directory, reviewed before apply.
- **Modular:** each module is independently testable and reusable.

## Notes

**Deployment workflow:**
- Code push → GitHub Actions (pipeline-ci.yml) → build container image → push to ECR.
- Manual approval gate (human reviews the plan and PR).
- GitHub Actions (deploy.yml) runs `terraform plan` against staging, then applies the image update.
- If the canary rollout and smoke tests pass, the same image is deployed to prod.

**State management:**
- Terraform state is stored in S3 (with versioning and locks).
- Separate state files for staging (`envs/staging/terraform.tfstate`) and prod.
- State is not checked into Git (security); it's managed by the CI pipeline.

**Encryption at rest:**
- RDS PostgreSQL: KMS encryption (key in `modules/data`).
- ElastiCache Redis: encrypted by default (AWS-managed or customer-managed key).
- EBS volumes: KMS encryption.
- CloudWatch Logs: KMS encryption.
- Secrets Manager (for DB credentials): AWS-managed encryption.

**High availability:**
- RDS: multi-AZ (primary + synchronous standby; failover < 1 min).
- ECS: min_capacity = 2, distributed across AZs (no single point of failure).
- ALB: deployed across AZs, health checks every 5s, drain connections on shutdown.

**Security baselines (per module):**
- Network: private subnets for compute/data; public subnets for NAT (egress gateway).
- Compute: ECS task role (least-privilege IAM); no SSH access; container images scanned.
- Data: RDS/Redis in private subnets; no public IP; app role without `BYPASSRLS` on RDS.
- Observability: log retention policies (30 days default); alarm SNS topics (notify on-call).

**Future scaling:**
- Partitioning: when a single RDS primary is outgrown, partition by `api_key_id` (hash) — infrastructure is ready to accommodate this (comments in modules note the escalation path).
- Read replicas: read-heavy workloads can be offloaded to RDS read replicas (provisioned but not used yet; usage query is already optimized with the rollup).
- CDN caching: static responses (e.g., usage totals) could be cached by CloudFront (edge module is a stub for future use).
