# scripts/

## Purpose

Utility scripts for deployment, testing, and operational tasks: API key seeding, smoke checks, security scanning hooks, and CI workflow helpers.

## Modules

| File / Module | Responsibility |
|---|---|
| `seed_api_key.py` | Generates and provisions a new API key: creates a random secret, hashes it with Argon2id, inserts it into the database as a new `api_keys` row. Output includes the public key_id and the secret (displayed once, never stored). Used for initial key setup and test key generation. |
| `smoke_import_check.py` | Python import smoke test: attempts to import the main app module (`src.main`) to catch syntax errors and early import failures before deployment. Run by the CI smoke-check hook. |
| `ci/lockfile-check.sh` | Validates that `poetry.lock` is in sync with `pyproject.toml` (no stale/diverged dependencies). Run by CI before build. |
| `ci/asvs-sast.sh` | ASVS Tier-1 SAST hook: runs Semgrep with project-specific rules to detect common security issues. Output feeds into the security report. |
| `ci/dast-review.sh` | DAST review aggregator: collects and summarizes findings from runtime security scanning (e.g., OWASP ZAP). Run post-deployment. |
| `ci/guard-source-markers.sh` | Validates that all deployment-critical markers are in place (e.g., PR description file, release notes). |
| `ci/store-compliance.sh` | Validates app store (Apple, Google Play) compliance metadata (not applicable for this API-only project; stub for cross-repo standardization). |

## Relationships

**Invocation:**
- `seed_api_key.py` — run manually during setup: `poetry run python scripts/seed_api_key.py`.
- `smoke_import_check.py` — run by CI smoke check: `poetry run python scripts/smoke_import_check.py`.
- `ci/*.sh` scripts — run by GitHub Actions workflows (pipeline-ci.yml, deploy.yml, etc.).

**Entrypoints for external systems:**
- CI/CD pipelines call the `ci/*.sh` scripts to validate and report on the change.
- Human operators call `seed_api_key.py` to provision new API keys (for staging/test/prod as needed).

## Notes

**API key provisioning:**
- `seed_api_key.py` reads the database connection from `DATABASE_URL` env var (or Secrets Manager if not set).
- Takes optional CLI args: `--key_id` (public key handle, defaults to generated random), `--label` (human description), `--rate_limit_per_sec` (token bucket limit, defaults to 100).
- Outputs: the full token `mtr_live_<key_id>_<secret>`, which should be shared with the caller.
- The script runs in the context of the deployed environment (same database as the app).

**Smoke check:**
- `smoke_import_check.py` is fast (< 1 second) and safe (no state changes, no external calls).
- Used as a pre-flight check before running the full test suite (catches early failures).

**CI integration:**
- All `ci/*.sh` scripts are invoked by GitHub Actions workflows.
- They read artifacts and env vars set by prior steps (e.g., test results, security scan outputs).
- They exit with status code 0 (pass) or non-zero (fail), which GitHub Actions interprets as workflow pass/fail.

**Security scanning hooks:**
- `asvs-sast.sh` runs Semgrep (via Docker) on the changed code.
- Output is JSON, aggregated into the security report.
- Flags like `--docker` enable Docker-based execution (for Semgrep to work on systems without native Python/Node/Go runtimes).
