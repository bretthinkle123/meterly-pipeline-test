"""AC14/AC16: Tier-2 per-owner (api_key_id-keyed) rate limiting.

Discriminating shape (per test-conventions): two principals sharing one
client IP must get independent buckets, and a single principal across two
IPs must still share one bucket — proving the limiter is keyed on
`api_key_id`, never the client IP.
"""

from httpx import ASGITransport, AsyncClient


def _usage_query_params() -> dict:
    from datetime import datetime, timezone

    return {"customer_id": "cust_1", "metric": "api_calls", "window": datetime.now(timezone.utc).isoformat()}


async def test_two_principals_sharing_one_ip_get_independent_buckets(truncate_tables, make_api_key):
    """Principal A exhausts its own (tiny) bucket -> 429; principal B, same
    client IP, still succeeds -> the bucket key is the api_key_id, not the IP."""
    from src.main import app

    key_a, _ = await make_api_key(label="a", rate_limit_per_sec=1)
    key_b, _ = await make_api_key(label="b", rate_limit_per_sec=100)

    same_ip_transport = ASGITransport(app=app, client=("10.0.0.5", 4000))
    async with AsyncClient(transport=same_ip_transport, base_url="http://test") as client:
        # Exhaust A's 1-token bucket.
        first = await client.get("/v1/usage", params=_usage_query_params(), headers={"Authorization": f"Bearer {key_a}"})
        second = await client.get("/v1/usage", params=_usage_query_params(), headers={"Authorization": f"Bearer {key_a}"})

        assert first.status_code == 200
        assert second.status_code == 429
        assert "Retry-After" in second.headers

        # Principal B, same client IP, is unaffected.
        b_response = await client.get("/v1/usage", params=_usage_query_params(), headers={"Authorization": f"Bearer {key_b}"})
        assert b_response.status_code == 200


async def test_one_principal_across_two_ips_still_shares_one_bucket(truncate_tables, make_api_key):
    """The same api_key_id from two different client IPs still shares a single
    bucket (Tier-2 is never IP-keyed, unlike Tier-1)."""
    from src.main import app

    key_a, _ = await make_api_key(label="a", rate_limit_per_sec=1)

    transport_ip1 = ASGITransport(app=app, client=("10.0.0.1", 1))
    transport_ip2 = ASGITransport(app=app, client=("10.0.0.2", 2))

    async with AsyncClient(transport=transport_ip1, base_url="http://test") as client1, AsyncClient(
        transport=transport_ip2, base_url="http://test"
    ) as client2:
        first = await client1.get("/v1/usage", params=_usage_query_params(), headers={"Authorization": f"Bearer {key_a}"})
        second = await client2.get("/v1/usage", params=_usage_query_params(), headers={"Authorization": f"Bearer {key_a}"})

        assert first.status_code == 200
        assert second.status_code == 429, "the same api_key_id from a different IP must still share the exhausted bucket"
