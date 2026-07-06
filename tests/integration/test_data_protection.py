"""AC8/AC-DATA-PROTECTION: the persisted `api_keys.secret_hash` column is an
Argon2id hash, never the plaintext key, while the normal auth path still
verifies successfully end to end."""

from datetime import datetime, timezone

from sqlalchemy import text


async def test_persisted_secret_hash_is_argon2id_not_plaintext(client, make_api_key, postgres_url):
    """Read the RAW stored column (bypassing the app's verify path) and assert
    it is an Argon2id hash, never the plaintext secret — while the normal
    authenticated request still succeeds (the app's decrypt/verify path
    round-trips)."""
    presented_key, api_key_id = await make_api_key()
    plaintext_secret = presented_key.split("_")[-1]

    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(postgres_url)
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT secret_hash FROM api_keys WHERE id = :id"), {"id": api_key_id}
            )
        ).mappings().first()
    await engine.dispose()

    stored_value = row["secret_hash"]
    assert stored_value != presented_key
    assert plaintext_secret not in stored_value
    assert stored_value.startswith("$argon2id$")

    # The app's normal authenticated path still succeeds against this same
    # stored hash (verify() round-trips).
    response = await client.get(
        "/v1/usage",
        params={"customer_id": "cust_1", "metric": "api_calls", "window": datetime.now(timezone.utc).isoformat()},
        headers={"Authorization": f"Bearer {presented_key}"},
    )
    assert response.status_code == 200
