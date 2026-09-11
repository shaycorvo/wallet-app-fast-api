import uuid

import jwt
import pytest
from httpx import ASGITransport, AsyncClient

from src.config import get_settings
from src.database import engine
from src.main import app
from src.wallets.models import Base


@pytest.fixture(autouse=True)
async def reset_database():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)


def bearer_token(user_id: uuid.UUID) -> dict[str, str]:
    token = jwt.encode({"sub": str(user_id)}, get_settings().jwt_secret, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_get_wallet_returns_the_authenticated_users_committed_balance():
    user_id = uuid.uuid4()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/wallets", headers=bearer_token(user_id))
        wallet_id = created.json()["id"]

        response = await client.get(f"/wallets/{wallet_id}", headers=bearer_token(user_id))

    assert response.status_code == 200
    assert response.json()["id"] == wallet_id
    assert response.json()["balance_paise"] == 0


@pytest.mark.asyncio
async def test_get_wallet_rejects_another_users_wallet():
    owner_id = uuid.uuid4()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/wallets", headers=bearer_token(owner_id))
        wallet_id = created.json()["id"]

        response = await client.get(f"/wallets/{wallet_id}", headers=bearer_token(uuid.uuid4()))

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_get_wallet_returns_not_found_for_unknown_wallet():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/wallets/{uuid.uuid4()}", headers=bearer_token(uuid.uuid4()))

    assert response.status_code == 404