import uuid

import jwt
import pytest
from httpx import ASGITransport, AsyncClient

from src.config import get_settings
from src.database import SessionFactory, engine
from src.main import app
from src.wallets.models import Base, Wallet


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


async def create_wallet(user_id: uuid.UUID, balance_paise: int) -> Wallet:
    async with SessionFactory() as session, session.begin():
        wallet = Wallet(user_id=user_id, balance_paise=balance_paise)
        session.add(wallet)
        await session.flush()
        return wallet


@pytest.mark.asyncio
async def test_get_transfer_returns_the_callers_durable_final_status():
    caller_id = uuid.uuid4()
    sender = await create_wallet(caller_id, 500)
    recipient = await create_wallet(uuid.uuid4(), 0)
    payload = {
        "from": str(sender.id),
        "to": str(recipient.id),
        "amount_paise": 200,
        "idempotency_key": "status-test",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/transfers", json=payload, headers=bearer_token(caller_id))
        response = await client.get(
            f"/transfers/{created.json()['id']}", headers=bearer_token(caller_id)
        )

    assert created.status_code == 200
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["amount_paise"] == 200
    assert response.json()["completed_at"] is not None


@pytest.mark.asyncio
async def test_create_transfer_rejects_unknown_request_fields():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/transfers",
            json={
                "from": str(uuid.uuid4()),
                "to": str(uuid.uuid4()),
                "amount_paise": 1,
                "idempotency_key": "invalid-body",
                "unexpected": True,
            },
            headers=bearer_token(uuid.uuid4()),
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_get_transfer_rejects_another_callers_transfer():
    caller_id = uuid.uuid4()
    sender = await create_wallet(caller_id, 500)
    recipient = await create_wallet(uuid.uuid4(), 0)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            "/transfers",
            json={
                "from": str(sender.id),
                "to": str(recipient.id),
                "amount_paise": 200,
                "idempotency_key": "ownership-test",
            },
            headers=bearer_token(caller_id),
        )
        response = await client.get(
            f"/transfers/{created.json()['id']}", headers=bearer_token(uuid.uuid4())
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_get_transfer_returns_not_found_for_unknown_transfer():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/transfers/{uuid.uuid4()}", headers=bearer_token(uuid.uuid4()))

    assert response.status_code == 404