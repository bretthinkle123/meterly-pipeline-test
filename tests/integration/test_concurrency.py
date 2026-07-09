"""AC-CONCURRENCY (AC7): 50 concurrent POSTs with the same idempotency_key
must create exactly one `events` row; the 49 losers each get the idempotent
replay (200), and `usage_rollup.event_count` increments exactly once.
"""

import asyncio

from sqlalchemy import text


async def test_fifty_concurrent_identical_posts_create_exactly_one_event(client, make_api_key, postgres_url):
    presented_key, api_key_id = await make_api_key()
    headers = {"Authorization": f"Bearer {presented_key}"}
    payload = {
        "customer_id": "cust_concurrent",
        "metric": "api_calls",
        "quantity": "1",
        "idempotency_key": "same-key-across-all-fifty",
    }

    responses = await asyncio.gather(
        *(client.post("/v1/events", json=payload, headers=headers) for _ in range(50))
    )

    statuses = [response.status_code for response in responses]
    winners = [status for status in statuses if status == 201]
    losers = [status for status in statuses if status == 200]

    assert len(winners) == 1, f"expected exactly one winning insert, got statuses: {statuses}"
    assert len(losers) == 49, "the 49 losers must each receive the idempotent replay (200)"

    event_ids = {response.json()["event_id"] for response in responses}
    assert len(event_ids) == 1, "every response (winner and losers) must reference the same event"

    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(postgres_url)
    async with engine.connect() as connection:
        event_count = (
            await connection.execute(
                text("SELECT COUNT(*) FROM events WHERE api_key_id = :id AND idempotency_key = :key"),
                {"id": api_key_id, "key": payload["idempotency_key"]},
            )
        ).scalar_one()
        rollup = (
            await connection.execute(
                text(
                    "SELECT event_count, total_quantity FROM usage_rollup "
                    "WHERE api_key_id = :id AND customer_id = :cust AND metric = :metric"
                ),
                {"id": api_key_id, "cust": "cust_concurrent", "metric": "api_calls"},
            )
        ).mappings().first()
    await engine.dispose()

    assert event_count == 1, "the UNIQUE constraint must admit exactly one row despite 50 concurrent attempts"
    assert rollup["event_count"] == 1, "the rollup counter must increment exactly once, not 50 times"
