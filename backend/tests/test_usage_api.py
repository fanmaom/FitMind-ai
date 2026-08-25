"""用量统计 API。"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.asyncio
async def test_empty_usage_summary_is_zeroed():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        registered = await client.post("/api/v1/auth/register", json={
            "email": f"usage-api-{uuid.uuid4().hex[:8]}@t.com", "password": "pw123456",
        })
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        out = (await client.get("/api/v1/usage/summary", headers=headers)).json()
        assert out["request_count"] == 0
        assert out["cache_hit_rate"] == 0
        assert out["degradation_rate"] == 0
