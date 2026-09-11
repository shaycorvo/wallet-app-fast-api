import asyncio
import uuid

import pytest
from sqlalchemy import func, select

from src.database import SessionFactory, engine
from src.transfers.exceptions import IdempotencyConflict
from src.transfers.models import LedgerEntry, Transfer, TransferStatus
from src.transfers.schemas import TransferRequest
from src.transfers.service import TransferService
from src.wallets.models import Base, Wallet


@pytest.fixture(autouse=True)
async def reset_database():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)


async def create_wallet(user_id: uuid.UUID, balance_paise: int) -> Wallet:
    async with SessionFactory() as session, session.begin():
        wallet = Wallet(user_id=user_id, balance_paise=balance_paise)
        session.add(wallet)
        await session.flush()
        return wallet


async def submit_transfer(user_id: uuid.UUID, request: TransferRequest):
    async with SessionFactory() as session:
        return await TransferService().create(session, user_id, request)


@pytest.mark.asyncio
async def test_idempotency_storm_applies_one_transfer_and_returns_one_result():
    user_id = uuid.uuid4()
    sender = await create_wallet(user_id, 10_000)
    recipient = await create_wallet(uuid.uuid4(), 0)
    request = TransferRequest(
        **{
            "from": sender.id,
            "to": recipient.id,
            "amount_paise": 750,
            "idempotency_key": "storm-key",
        }
    )

    results = await asyncio.gather(*(submit_transfer(user_id, request) for _ in range(30)))

    assert {transfer.id for transfer, _ in results} == {results[0][0].id}
    assert {transfer.status for transfer, _ in results} == {TransferStatus.COMPLETED}
    async with SessionFactory() as session:
        balances = (await session.scalars(select(Wallet.balance_paise).order_by(Wallet.id))).all()
        transfer_count = await session.scalar(select(func.count()).select_from(Transfer))
        ledger_count = await session.scalar(select(func.count()).select_from(LedgerEntry))
    assert sum(balances) == 10_000
    assert sorted(balances) == [750, 9_250]
    assert transfer_count == 1
    assert ledger_count == 2


@pytest.mark.asyncio
async def test_same_idempotency_key_with_different_request_is_rejected():
    user_id = uuid.uuid4()
    sender = await create_wallet(user_id, 1_000)
    recipient = await create_wallet(uuid.uuid4(), 0)
    first = TransferRequest(
        **{"from": sender.id, "to": recipient.id, "amount_paise": 100, "idempotency_key": "key"}
    )
    conflicting = TransferRequest(
        **{"from": sender.id, "to": recipient.id, "amount_paise": 101, "idempotency_key": "key"}
    )

    await submit_transfer(user_id, first)
    with pytest.raises(IdempotencyConflict):
        await submit_transfer(user_id, conflicting)

    async with SessionFactory() as session:
        sender_balance = await session.scalar(select(Wallet.balance_paise).where(Wallet.id == sender.id))
        recipient_balance = await session.scalar(
            select(Wallet.balance_paise).where(Wallet.id == recipient.id)
        )
    assert sender_balance == 900
    assert recipient_balance == 100


@pytest.mark.asyncio
async def test_insufficient_funds_is_a_durable_decline_without_partial_movement():
    user_id = uuid.uuid4()
    sender = await create_wallet(user_id, 99)
    recipient = await create_wallet(uuid.uuid4(), 0)
    request = TransferRequest(
        **{"from": sender.id, "to": recipient.id, "amount_paise": 100, "idempotency_key": "decline"}
    )

    transfer, replay = await submit_transfer(user_id, request)
    replayed, is_replay = await submit_transfer(user_id, request)

    assert replay is False
    assert transfer.status == TransferStatus.DECLINED
    assert transfer.decline_reason == "insufficient_funds"
    assert transfer.completed_at is not None
    assert is_replay is True
    assert replayed.id == transfer.id
    async with SessionFactory() as session:
        balances = (await session.scalars(select(Wallet.balance_paise))).all()
        ledger_count = await session.scalar(select(func.count()).select_from(LedgerEntry))
    assert sorted(balances) == [0, 99]
    assert ledger_count == 0


@pytest.mark.asyncio
async def test_bidirectional_contention_preserves_total_and_never_overdrafts():
    owner_ids = [uuid.uuid4() for _ in range(4)]
    wallet_owners = [(await create_wallet(owner_id, 1_000), owner_id) for owner_id in owner_ids]
    wallets = [wallet for wallet, _ in wallet_owners]
    owners_by_wallet_id = {wallet.id: owner_id for wallet, owner_id in wallet_owners}
    requests = []
    for index in range(200):
        source = wallets[index % len(wallets)]
        destination = wallets[(index + 1) % len(wallets)]
        requests.append((
            owners_by_wallet_id[source.id],
            TransferRequest(
                **{
                    "from": source.id,
                    "to": destination.id,
                    "amount_paise": 75 if index % 5 else 1_500,
                    "idempotency_key": f"contention-{index}",
                }
            ),
        ))

    semaphore = asyncio.Semaphore(10)

    async def submit_with_bounded_concurrency(owner_id: uuid.UUID, request: TransferRequest):
        async with semaphore:
            return await submit_transfer(owner_id, request)

    await asyncio.gather(
        *(submit_with_bounded_concurrency(owner_id, request) for owner_id, request in requests)
    )

    async with SessionFactory() as session:
        balances = (await session.scalars(select(Wallet.balance_paise))).all()
        completed = await session.scalar(
            select(func.count()).select_from(Transfer).where(Transfer.status == TransferStatus.COMPLETED)
        )
        ledger_count = await session.scalar(select(func.count()).select_from(LedgerEntry))
    assert sum(balances) == 4_000
    assert all(balance >= 0 for balance in balances)
    assert ledger_count == completed * 2