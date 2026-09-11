import secrets
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.database import get_session
from src.wallets.models import Wallet

router = APIRouter(prefix="/internal/test", include_in_schema=False)


class FundWalletRequest(BaseModel):
    amount_paise: int = Field(gt=0, le=9_000_000_000_000_000_000)


class FundWalletResponse(BaseModel):
    wallet_id: UUID
    balance_paise: int


def verify_test_seed_access(x_test_seed_key: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if settings.app_environment not in {"local", "test", "demo"} or not settings.test_seed_key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if x_test_seed_key is None or not secrets.compare_digest(x_test_seed_key, settings.test_seed_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid test seed key")


@router.post("/wallets/{wallet_id}/fund", response_model=FundWalletResponse)
async def fund_wallet(
    wallet_id: UUID,
    request: FundWalletRequest,
    _: None = Depends(verify_test_seed_access),
    session: AsyncSession = Depends(get_session),
) -> FundWalletResponse:
    async with session.begin():
        result = await session.execute(
            update(Wallet)
            .where(Wallet.id == wallet_id)
            .values(balance_paise=Wallet.balance_paise + request.amount_paise)
            .returning(Wallet.balance_paise)
        )
        balance_paise = result.scalar_one_or_none()
        if balance_paise is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wallet not found")
    return FundWalletResponse(wallet_id=wallet_id, balance_paise=balance_paise)