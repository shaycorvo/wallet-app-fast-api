import asyncio
import hashlib
import json
import random
import uuid
from datetime import UTC, datetime

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from src.transfers.exceptions import IdempotencyConflict, SourceWalletAccessDenied, WalletNotFound
from src.transfers.models import Transfer, TransferStatus
from src.transfers.repository import TransferRepository
from src.transfers.schemas import TransferRequest


class TransferService:
    _RETRYABLE_SQLSTATES = {"40001", "40P01", "55P03"}
    _MAX_TRANSACTION_ATTEMPTS = 3

    def __init__(self, repository: TransferRepository | None = None) -> None:
        self._repository = repository or TransferRepository()

    async def get_by_id(self, session: AsyncSession, transfer_id: uuid.UUID) -> Transfer | None:
        return await self._repository.get_by_id(session, transfer_id)

    async def create(
        self, session: AsyncSession, caller_user_id: uuid.UUID, request: TransferRequest
    ) -> tuple[Transfer, bool]:
        request_hash = self._request_hash(caller_user_id, request)
        transfer_id = uuid.uuid4()
        for attempt in range(self._MAX_TRANSACTION_ATTEMPTS):
            try:
                return await self._create_once(
                    session, caller_user_id, request, request_hash, transfer_id
                )
            except DBAPIError as error:
                sqlstate = getattr(error.orig, "sqlstate", None)
                if sqlstate not in self._RETRYABLE_SQLSTATES or attempt == self._MAX_TRANSACTION_ATTEMPTS - 1:
                    raise
                await asyncio.sleep(0.01 * (2**attempt) + random.uniform(0, 0.01))
        raise RuntimeError("Transfer retry loop exited unexpectedly")

    async def _create_once(
        self,
        session: AsyncSession,
        caller_user_id: uuid.UUID,
        request: TransferRequest,
        request_hash: str,
        transfer_id: uuid.UUID,
    ) -> tuple[Transfer, bool]:
        async with session.begin():
            created = await self._repository.create_if_absent(
                session,
                transfer_id=transfer_id,
                caller_user_id=caller_user_id,
                idempotency_key=request.idempotency_key,
                request_hash=request_hash,
                from_wallet_id=request.from_wallet_id,
                to_wallet_id=request.to_wallet_id,
                amount_paise=request.amount_paise,
            )
            if not created:
                existing = await self._repository.get_by_idempotency_key(
                    session, caller_user_id, request.idempotency_key
                )
                if existing is None:
                    raise RuntimeError("Idempotency conflict did not return an existing transfer")
                if existing.request_hash != request_hash:
                    raise IdempotencyConflict()
                return existing, True

            locked_wallets = await self._repository.lock_wallets(
                session, request.from_wallet_id, request.to_wallet_id
            )
            if len(locked_wallets) != 2:
                raise WalletNotFound()
            wallets_by_id = {wallet.id: wallet for wallet in locked_wallets}
            source = wallets_by_id[request.from_wallet_id]
            if source.user_id != caller_user_id:
                raise SourceWalletAccessDenied()

            transfer = await self._repository.get_by_idempotency_key(
                session, caller_user_id, request.idempotency_key
            )
            if transfer is None:
                raise RuntimeError("Created transfer was not found")
            debited = await self._repository.debit_if_sufficient(
                session, source.id, request.amount_paise
            )
            if not debited:
                transfer.status = TransferStatus.DECLINED
                transfer.decline_reason = "insufficient_funds"
                transfer.completed_at = datetime.now(UTC)
                return transfer, False

            await self._repository.credit(session, request.to_wallet_id, request.amount_paise)
            await self._repository.add_ledger_entries(session, transfer)
            transfer.status = TransferStatus.COMPLETED
            transfer.completed_at = datetime.now(UTC)
            return transfer, False

    @staticmethod
    def _request_hash(caller_user_id: uuid.UUID, request: TransferRequest) -> str:
        payload = json.dumps(
            {
                "caller_user_id": str(caller_user_id),
                "from_wallet_id": str(request.from_wallet_id),
                "to_wallet_id": str(request.to_wallet_id),
                "amount_paise": request.amount_paise,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()