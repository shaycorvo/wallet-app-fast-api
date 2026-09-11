from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.transfers.models import TransferStatus


class TransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_wallet_id: UUID = Field(alias="from")
    to_wallet_id: UUID = Field(alias="to")
    amount_paise: int = Field(gt=0, le=9_000_000_000_000_000_000)
    idempotency_key: str = Field(min_length=1, max_length=128)


class TransferResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    from_wallet_id: UUID
    to_wallet_id: UUID
    amount_paise: int
    status: TransferStatus
    decline_reason: str | None
    created_at: datetime
    completed_at: datetime | None