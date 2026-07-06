"""AC23 (DAST-2): `scripts/seed_api_key.py` provisions a low-privilege test
key against a real database — its secret is printed once (never persisted
in plaintext) and comes from the CLI/operator, never a hardcoded value."""

import subprocess
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

_REPO_ROOT = Path(__file__).resolve().parents[2]


async def test_seed_api_key_script_persists_only_the_hash(postgres_url, truncate_tables):
    """Running the seed script inserts a row whose stored `secret_hash` is
    Argon2id (never the printed plaintext), and prints the plaintext key
    exactly once to stdout for the operator to capture."""
    import os

    env = dict(os.environ)
    env["DATABASE_URL"] = postgres_url

    result = subprocess.run(
        [sys.executable, "scripts/seed_api_key.py", "--label", "dast-test-key", "--rate-limit", "50"],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"seed script failed:\n{result.stdout}\n{result.stderr}"
    printed_key = result.stdout.strip().splitlines()[-1]
    assert printed_key.startswith("mtr_live_")

    engine = create_async_engine(postgres_url)
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT secret_hash, label, rate_limit_per_sec FROM api_keys WHERE label = 'dast-test-key'")
            )
        ).mappings().first()
    await engine.dispose()

    assert row is not None
    assert row["secret_hash"].startswith("$argon2id$")
    assert row["secret_hash"] not in printed_key
    assert row["rate_limit_per_sec"] == 50
