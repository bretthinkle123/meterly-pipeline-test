"""AC13: strict enforcement under concurrency. N concurrent distinct-
idempotency-key posts of `Q` against a cap `L` must never drive
`usage_rollup.total_quantity` above `L` — the `FOR UPDATE OF q` row lock in
`src/repositories/quotas_repo.py` serializes every writer for the same
`(customer, metric)`, so the excess requests are rejected 429 rather than the
cap being exceeded (`plan §"The atomic read-and-decide"`).
"""

import asyncio
from decimal import Decimal

from sqlalchemy import text


async def test_concurrent_posts_never_exceed_limit(client, make_api_key, postgres_url):
    """20 concurrent posts of quantity=1 (distinct idempotency_key) against a
    cap of 10 must land exactly 10 winners and 10 rejections, with the final
    rollup total never exceeding the cap."""
    admin_key, api_key_id = await make_api_key(scope="admin", rate_limit_per_sec=1000)
    headers = {"Authorization": f"Bearer {admin_key}"}
    limit_per_window = 10
    concurrent_requests = 20

    quota_response = await client.put(
        "/v1/quotas",
        json={"customer_id": "cust_concurrent", "metric": "api_calls", "limit_per_window": limit_per_window},
        headers=headers,
    )
    assert quota_response.status_code == 201

    async def _post(index: int):
        return await client.post(
            "/v1/events",
            json={
                "customer_id": "cust_concurrent",
                "metric": "api_calls",
                "quantity": "1",
                "idempotency_key": f"concurrent-{index}",
            },
            headers=headers,
        )

    responses = await asyncio.gather(*(_post(index) for index in range(concurrent_requests)))
    statuses = [response.status_code for response in responses]
    winners = [status for status in statuses if status == 201]
    rejections = [status for status in statuses if status == 429]

    assert len(winners) == limit_per_window, f"expected exactly {limit_per_window} winners, got statuses: {statuses}"
    assert len(rejections) == concurrent_requests - limit_per_window
    assert all(response.json()["error"]["code"] == "quota_exceeded" for response in responses if response.status_code == 429)

    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(postgres_url)
    async with engine.connect() as connection:
        rollup = (
            await connection.execute(
                text(
                    "SELECT total_quantity, event_count FROM usage_rollup "
                    "WHERE api_key_id = :id AND customer_id = 'cust_concurrent' AND metric = 'api_calls'"
                ),
                {"id": api_key_id},
            )
        ).mappings().first()
    await engine.dispose()

    assert Decimal(rollup["total_quantity"]) <= Decimal(limit_per_window), "usage must never exceed the cap"
    assert Decimal(rollup["total_quantity"]) == Decimal(limit_per_window), "the lock must let exactly L worth land"
    assert rollup["event_count"] == limit_per_window
