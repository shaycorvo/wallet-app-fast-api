from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user_id
from src.database import get_session
from src.wallets.schemas import WalletResponse
from src.wallets.service import WalletService

router = APIRouter(prefix="/wallets", tags=["wallets"])
service = WalletService()


@router.post("", response_model=WalletResponse, status_code=status.HTTP_200_OK)
async def get_or_create_wallet(
    user_id=Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> WalletResponse:
    return await service.get_or_create(session, user_id)


@router.get("/{wallet_id}", response_model=WalletResponse)
async def get_wallet(
    wallet_id: UUID,
    user_id=Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> WalletResponse:
    wallet = await service.get_by_id(session, wallet_id)
    if wallet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wallet not found")
    if wallet.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Wallet access denied")
    return wallet