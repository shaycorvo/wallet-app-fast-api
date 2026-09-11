import pytest
from httpx import ASGITransport, AsyncClient

from src.main import app


@pytest.mark.asyncio
async def test_liveness_readiness_and_metrics_use_route_templates():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        live = await client.get("/live")
        ready = await client.get("/ready")
        metrics = await client.get("/metrics")

    assert live.status_code == 200
    assert ready.status_code == 200
    assert metrics.status_code == 200
    assert 'path="/live"' in metrics.text
    assert 'path="/ready"' in metrics.text