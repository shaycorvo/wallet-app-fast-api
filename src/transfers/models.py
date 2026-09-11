import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.wallets.models import Base


class TransferStatus(StrEnum):
    PROCESSING = "processing"
    COMPLETED = "completed"
    DECLINED = "declined"


class LedgerEntryType(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


class Transfer(Base):
    __tablename__ = "transfers"
    __table_args__ = (UniqueConstraint("caller_user_id", "idempotency_key"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    caller_user_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    from_wallet_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wallets.id", deferrable=True, initially="DEFERRED"), nullable=False
    )
    to_wallet_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wallets.id", deferrable=True, initially="DEFERRED"), nullable=False
    )
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[TransferStatus] = mapped_column(Enum(TransferStatus), nullable=False)
    decline_reason: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        CheckConstraint("amount_paise > 0", name="amount_positive"),
        UniqueConstraint("transfer_id", "wallet_id", "entry_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    transfer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transfers.id"), nullable=False)
    wallet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wallets.id"), nullable=False)
    entry_type: Mapped[LedgerEntryType] = mapped_column(Enum(LedgerEntryType), nullable=False)
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())