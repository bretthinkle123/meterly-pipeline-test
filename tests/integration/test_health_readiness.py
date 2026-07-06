"""AC21 (readiness half): `/health/ready` performs a real DB + migration-head
check against a live Postgres."""


async def test_readiness_returns_200_when_db_reachable_and_at_migration_head(client):
    """With a live, migrated database, readiness reports 200/ready."""
    response = await client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["database"] == "reachable"
