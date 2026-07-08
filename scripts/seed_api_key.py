#!/usr/bin/env python
"""Provision an API key: generates a key, prints the plaintext exactly once,
and stores only its Argon2id hash. There is no key-creation HTTP endpoint in
this build's scope (`PROJECT.md` — RBAC/key-management is a later feature),
so this CLI is the only provisioning path.

Also used to seed the low-privilege DAST test key (DAST-2) in staging/local,
and the dashboard's server-held `dashboard-reader` key (feature 3). This
script never persists a plaintext key anywhere itself, and a plaintext key
never lands in a migration file (`code-standards` forbids credentials in
migration files).

Usage:
    poetry run python scripts/seed_api_key.py --label "dast-test-key" --rate-limit 50

    # Dashboard reader key: mint it, hash it into api_keys, AND write its
    # plaintext to the Secrets Manager container Terraform provisions
    # (infra/modules/data/main.tf) — out-of-band, so the value never touches
    # *.tfstate/*.tfvars (iac-conventions / secrets-management).
    poetry run python scripts/seed_api_key.py --label "dashboard-reader" \\
        --rate-limit 50 --write-to-secret meterly/staging/dashboard-reader-key
"""

import argparse
import asyncio
import sys

from src.auth.api_key import generate_split_token, hash_new_secret
from src.config.settings import get_settings
from src.db.session import dispose_engine, get_engine
from src.repositories.api_keys_repo import create_api_key


def _write_secret_out_of_band(secret_name: str, presented_key: str, *, aws_region: str) -> None:
    """Write a freshly minted key's plaintext to Secrets Manager out-of-band.

    Terraform provisions the secret *container* only (`infra/modules/data/main.tf`,
    `ignore_changes = [secret_string]`) — this is the one supported path that
    populates its real value, run by an operator with Secrets Manager write
    access, never by CI/Terraform, so the plaintext never lands in
    `*.tfstate` or a committed `*.tfvars` (I-D1).
    """
    import boto3

    client = boto3.client("secretsmanager", region_name=aws_region)
    client.put_secret_value(SecretId=secret_name, SecretString=presented_key)


async def _seed(
    label: str, rate_limit_per_sec: int, *, write_to_secret: str | None, aws_region: str
) -> None:
    """Generate a new API key, persist its Argon2id hash, and either print
    the plaintext presented-key value to stdout (default) or write it
    out-of-band to `write_to_secret` (the dashboard-reader provisioning path)
    — never both, so the plaintext has exactly one destination."""
    key_id, secret, presented_key = generate_split_token()
    secret_hash = hash_new_secret(secret)

    async with get_engine().begin() as connection:
        api_key_row_id = await create_api_key(
            connection,
            key_id=key_id,
            secret_hash=secret_hash,
            label=label,
            rate_limit_per_sec=rate_limit_per_sec,
        )

    await dispose_engine()

    sys.stderr.write(f"Created api_keys.id={api_key_row_id} label={label!r}\n")

    if write_to_secret:
        _write_secret_out_of_band(write_to_secret, presented_key, aws_region=aws_region)
        sys.stderr.write(f"Wrote plaintext key to Secrets Manager secret {write_to_secret!r}\n")
    else:
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
        "--write-to-secret", default=None, dest="write_to_secret",
        help=(
            "Secrets Manager secret name/ARN to write this key's plaintext to "
            "out-of-band, instead of printing it to stdout (the dashboard-reader path)."
        ),
    )
    parser.add_argument(
        "--aws-region", default=None, dest="aws_region",
        help="AWS region for --write-to-secret (defaults to Settings.aws_region)",
    )
    args = parser.parse_args()
    asyncio.run(
        _seed(
            args.label,
            args.rate_limit_per_sec,
            write_to_secret=args.write_to_secret,
            aws_region=args.aws_region or get_settings().aws_region,
        )
    )


if __name__ == "__main__":
    main()
