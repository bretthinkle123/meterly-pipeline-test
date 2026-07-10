#!/usr/bin/env python
"""Provision an API key: generates a key, prints the plaintext exactly once,
and stores only its Argon2id hash. There is no key-creation HTTP endpoint in
this build's scope (`PROJECT.md` — RBAC/key-management is a later feature),
so this CLI is the only provisioning path.

Also used to seed the low-privilege DAST test key (DAST-2) in staging/local.
Its printed secret must be captured by the operator and written to SSM /
read from env by the DAST job — this script never persists the plaintext
anywhere itself, and the plaintext never lands in a migration file
(`code-standards` forbids credentials in migration files).

Usage:
    poetry run python scripts/seed_api_key.py --label "dast-test-key" --rate-limit 50
"""

import argparse
import asyncio
import sys
from pathlib import Path

# This is a standalone entrypoint, not an installed console script. When run as
# `python scripts/seed_api_key.py`, Python puts the script's own directory
# (`scripts/`) on sys.path[0], NOT the repo root, so `import src` fails whenever
# the package isn't pip-installed (e.g. CI's `poetry install --no-root`). Put the
# repo root on the path first so the import resolves regardless of install mode.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.auth.api_key import generate_split_token, hash_new_secret  # noqa: E402
from src.db.session import dispose_engine, get_engine  # noqa: E402
from src.repositories.api_keys_repo import create_api_key  # noqa: E402


async def _seed(label: str, rate_limit_per_sec: int, *, admin: bool) -> None:
    """Generate a new API key, persist its Argon2id hash, and print the
    plaintext presented-key value to stdout exactly once.

    `admin=True` provisions the key with `scope='admin'` — the superset scope
    required to call `PUT /v1/quotas` (a tenant that wants quotas provisions
    one admin-scoped key and uses it for both ingest and administration).
    """
    key_id, secret, presented_key = generate_split_token()
    secret_hash = hash_new_secret(secret)
    scope = "admin" if admin else "ingest"

    async with get_engine().begin() as connection:
        api_key_row_id = await create_api_key(
            connection,
            key_id=key_id,
            secret_hash=secret_hash,
            label=label,
            rate_limit_per_sec=rate_limit_per_sec,
            scope=scope,
        )

    await dispose_engine()

    sys.stderr.write(f"Created api_keys.id={api_key_row_id} label={label!r}\n")
    sys.stderr.write("Store this value now — it is never shown again:\n")
    print(presented_key)


def main() -> None:
    """Parse CLI arguments and run the seeding coroutine."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True, help="Human-readable label for this key")
    parser.add_argument(
        "--rate-limit", type=int, default=100, dest="rate_limit_per_sec",
        help="Tier-2 per-second token-bucket budget for this key (default: 100)",
    )
    parser.add_argument(
        "--admin", action="store_true",
        help="Provision this key with scope='admin' (required to call PUT /v1/quotas); default is scope='ingest'",
    )
    args = parser.parse_args()
    asyncio.run(_seed(args.label, args.rate_limit_per_sec, admin=args.admin))


if __name__ == "__main__":
    main()
