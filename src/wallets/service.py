from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.wallets.models import Wallet
from src.wallets.repository import WalletRepository


class WalletService:
    def __init__(self, repository: WalletRepository | None = None) -> None:
        self._repository = repository or WalletRepository()

    async def get_or_create(self, session: AsyncSession, user_id: UUID) -> Wallet:
        async with session.begin():
            return await self._repository.get_or_create(session, user_id)

    async def get_by_id(self, session: AsyncSession, wallet_id: UUID) -> Wallet | None:
        return await self._repository.get_by_id(session, wallet_id)