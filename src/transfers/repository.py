from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.transfers.models import LedgerEntry, LedgerEntryType, Transfer, TransferStatus
from src.wallets.models import Wallet


class TransferRepository:
    async def get_by_id(self, session: AsyncSession, transfer_id: UUID) -> Transfer | None:
        return await session.scalar(select(Transfer).where(Transfer.id == transfer_id))

    async def create_if_absent(
        self,
        session: AsyncSession,
        *,
        transfer_id: UUID,
        caller_user_id: UUID,
        idempotency_key: str,
        request_hash: str,
        from_wallet_id: UUID,
        to_wallet_id: UUID,
        amount_paise: int,
    ) -> bool:
        result = await session.execute(
            insert(Transfer)
            .values(
                id=transfer_id,
                caller_user_id=caller_user_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                from_wallet_id=from_wallet_id,
                to_wallet_id=to_wallet_id,
                amount_paise=amount_paise,
                status=TransferStatus.PROCESSING,
            )
            .on_conflict_do_nothing(
                index_elements=[Transfer.caller_user_id, Transfer.idempotency_key]
            )
            .returning(Transfer.id)
        )
        return result.scalar_one_or_none() is not None

    async def get_by_idempotency_key(
        self, session: AsyncSession, caller_user_id: UUID, idempotency_key: str
    ) -> Transfer | None:
        return await session.scalar(
            select(Transfer).where(
                Transfer.caller_user_id == caller_user_id,
                Transfer.idempotency_key == idempotency_key,
            )
        )

    async def lock_wallets(
        self, session: AsyncSession, from_wallet_id: UUID, to_wallet_id: UUID
    ) -> list[Wallet]:
        first_wallet_id, second_wallet_id = sorted((from_wallet_id, to_wallet_id))
        first_wallet = await session.scalar(
            select(Wallet).where(Wallet.id == first_wallet_id).with_for_update()
        )
        second_wallet = await session.scalar(
            select(Wallet).where(Wallet.id == second_wallet_id).with_for_update()
        )
        return [wallet for wallet in (first_wallet, second_wallet) if wallet is not None]

    async def debit_if_sufficient(
        self, session: AsyncSession, wallet_id: UUID, amount_paise: int
    ) -> bool:
        result = await session.execute(
            update(Wallet)
            .where(Wallet.id == wallet_id, Wallet.balance_paise >= amount_paise)
            .values(balance_paise=Wallet.balance_paise - amount_paise)
        )
        return result.rowcount == 1

    async def credit(self, session: AsyncSession, wallet_id: UUID, amount_paise: int) -> None:
        await session.execute(
            update(Wallet)
            .where(Wallet.id == wallet_id)
            .values(balance_paise=Wallet.balance_paise + amount_paise)
        )

    async def add_ledger_entries(
        self, session: AsyncSession, transfer: Transfer
    ) -> None:
        session.add_all(
            [
                LedgerEntry(
                    transfer_id=transfer.id,
                    wallet_id=transfer.from_wallet_id,
                    entry_type=LedgerEntryType.DEBIT,
                    amount_paise=transfer.amount_paise,
                ),
                LedgerEntry(
                    transfer_id=transfer.id,
                    wallet_id=transfer.to_wallet_id,
                    entry_type=LedgerEntryType.CREDIT,
                    amount_paise=transfer.amount_paise,
                ),
            ]
        )