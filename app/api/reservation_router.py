"""FastAPI router for reservation endpoints."""

from typing import Optional
from fastapi import APIRouter, Header, HTTPException, Response, status
from app.api.schemas import ReserveRequest, ReserveResponse, ErrorResponse
from app.services.reservation_service import ReservationService

router = APIRouter(tags=["Reservations"])


@router.post(
    "/reserve",
    response_model=ReserveResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"model": ReserveResponse, "description": "Reservation successfully created"},
        200: {"model": ReserveResponse, "description": "Duplicate request returned original reservation"},
        400: {"model": ErrorResponse, "description": "Missing or empty Idempotency-Key header"},
        409: {"model": ErrorResponse, "description": "SOLD_OUT when available quantity is zero"},
    },
)
def reserve_stock(
    body: ReserveRequest,
    response: Response,
    idempotency_key: Optional[str] = Header(
        None,
        alias="Idempotency-Key",
        description="Mandatory client idempotency key to prevent duplicate purchases",
    ),
):
    """Reserve a single unit of stock atomically for 10 minutes.
    
    Guarantees:
    - Mandatory Header: Returns HTTP 400 if Idempotency-Key is missing or empty.
    - Idempotency Replay: Returns HTTP 200 with original reservation without double-decrementing stock.
    - Concurrent Race Safety: Catches UNIQUE constraint conflicts on idempotency_key and returns the winning record.
    - Sold-out detection: Returns HTTP 409 with 'SOLD_OUT' if available quantity is 0.
    """
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing Idempotency-Key header",
        )

    service = ReservationService()
    reservation, is_new = service.reserve(
        product_id=body.product_id,
        customer_id=body.customer_id,
        idempotency_key=idempotency_key.strip(),
    )

    if reservation is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="SOLD_OUT",
        )

    if not is_new:
        response.status_code = status.HTTP_200_OK

    return ReserveResponse(
        reservation_id=reservation.reservation_id,
        product_id=reservation.product_id,
        customer_id=reservation.customer_id,
        status=reservation.status,
        expires_at=reservation.expires_at,
    )
