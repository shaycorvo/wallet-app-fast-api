from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.wallets.models import Wallet


class WalletRepository:
    async def get_or_create(self, session: AsyncSession, user_id: UUID) -> Wallet:
        statement = (
            insert(Wallet)
            .values(user_id=user_id, balance_paise=0)
            .on_conflict_do_nothing(index_elements=[Wallet.user_id])
        )
        await session.execute(statement)
        wallet = await session.scalar(select(Wallet).where(Wallet.user_id == user_id))
        if wallet is None:
            raise RuntimeError("Wallet insert completed without a matching wallet row")
        return wallet

    async def get_by_id(self, session: AsyncSession, wallet_id: UUID) -> Wallet | None:
        return await session.scalar(select(Wallet).where(Wallet.id == wallet_id))