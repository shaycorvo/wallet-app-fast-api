import asyncio
import uuid

import pytest
from sqlalchemy import func, select

from src.database import SessionFactory, engine
from src.wallets.models import Base, Wallet
from src.wallets.service import WalletService


@pytest.fixture(autouse=True)
async def reset_database():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)


async def create_wallet_for(user_id: uuid.UUID) -> uuid.UUID:
    async with SessionFactory() as session:
        wallet = await WalletService().get_or_create(session, user_id)
        return wallet.id


@pytest.mark.asyncio
async def test_concurrent_get_or_create_returns_one_wallet_for_one_user():
    user_id = uuid.uuid4()

    wallet_ids = await asyncio.gather(*(create_wallet_for(user_id) for _ in range(50)))

    assert len(set(wallet_ids)) == 1
    async with SessionFactory() as session:
        count = await session.scalar(
            select(func.count()).select_from(Wallet).where(Wallet.user_id == user_id)
        )
    assert count == 1