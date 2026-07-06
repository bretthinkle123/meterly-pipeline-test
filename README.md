# Meterly

Usage-metering ingestion API. See `PROJECT.md` for the product spec and
`docs/system_architecture.md` for the implementation overview.

## Quickstart

```bash
poetry install
poetry run alembic upgrade head
poetry run uvicorn src.main:app --port 8000
```

`GET /health` returns 200 with no dependencies (liveness). `GET /health/ready`
checks the database connection and migration head (readiness).
