import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user_id
from src.database import get_session
from src.observability import REPLAYS, TRANSFERS, TRANSFERS_CREATED, TRANSFERS_DECLINED
from src.transfers.exceptions import IdempotencyConflict, SourceWalletAccessDenied, WalletNotFound
from src.transfers.schemas import TransferRequest, TransferResponse
from src.transfers.service import TransferService

router = APIRouter(prefix="/transfers", tags=["transfers"])
service = TransferService()
logger = logging.getLogger("wallet_service")


@router.post("", response_model=TransferResponse, status_code=status.HTTP_200_OK)
async def create_transfer(
    request: TransferRequest,
    http_request: Request,
    user_id=Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> TransferResponse:
    if request.from_wallet_id == request.to_wallet_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Source and destination wallets must differ",
        )
    try:
        transfer, replay = await service.create(session, user_id, request)
    except IdempotencyConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency key was reused with a different request",
        ) from error
    except SourceWalletAccessDenied as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Source wallet access denied",
        ) from error
    except WalletNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Wallet not found",
        ) from error
    correlation_id = http_request.state.correlation_id
    log_fields = {
        "correlation_id": correlation_id,
        "transfer_id": str(transfer.id),
        "status": transfer.status,
        "amount_paise": transfer.amount_paise,
    }
    if replay:
        REPLAYS.inc()
        logger.info("transfer_idempotent_replay", extra=log_fields)
    else:
        TRANSFERS_CREATED.inc()
        TRANSFERS.labels(transfer.status).inc()
        logger.info("transfer_created", extra=log_fields)
        if transfer.status == "completed":
            logger.info("transfer_debited", extra=log_fields)
            logger.info("transfer_credited", extra=log_fields)
        else:
            TRANSFERS_DECLINED.labels(reason=transfer.decline_reason or "unknown").inc()
            logger.info("transfer_declined", extra=log_fields)
    return transfer


@router.get("/{transfer_id}", response_model=TransferResponse)
async def get_transfer(
    transfer_id: UUID,
    user_id=Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> TransferResponse:
    transfer = await service.get_by_id(session, transfer_id)
    if transfer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transfer not found")
    if transfer.caller_user_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Transfer access denied")
    return transfer