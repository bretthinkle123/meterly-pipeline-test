# Meterly

## Stack
- Cloud environment: AWS (ECS Fargate + RDS PostgreSQL + ALB), Terraform under infra/
- Language/runtime: Python 3.12
- Framework(s): FastAPI
- Data stores: PostgreSQL
- Migration tool: Alembic
- Cloud / IaC: AWS + Terraform
- Auth provider: API keys (Argon2id-hashed at rest), per-key rate limiting — no third-party IdP
- Observability: CloudWatch + X-Ray + Sentry (release-tagged)
- Packaging / runtime: container (Docker) — justified: deploys through the ECS canary path

## How to run / build / test
- Start: `uvicorn src.main:app --port 8000` (smoke check expects HTTP 200 at `http://localhost:8000/health`)
- Test:  `pytest --cov=src --cov-branch`
- Migrate: `alembic upgrade head` (run before deploying; also run locally after pulling schema changes)
- Deploy: CI on merge — see docs/pipeline-deployment-targets.md (build-provenance → deploy.yml chain)

## Frontend design source
- Design source: none (API only)

## Conventions
- Module layout: src/ package (src/main.py app entry), facade modules per code-standards defaults
- Test locations: tests/ as test_*.py

## What "done" means
- Smoke check passes, security report clean, tests pass at >= 85% coverage,
  docs updated for touched directories, PR description written.

<!--
  REMEMBER per project: the smoke-check.sh hook reads Start/health values above.
  Set them here, or export SMOKE_START_CMD / SMOKE_HEALTH_URL, or (frontend-only)
  swap in the build-check variant of smoke-check.sh from the spec.
-->
