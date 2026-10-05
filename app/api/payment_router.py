"""FastAPI router for payment endpoints."""

from typing import Optional
from fastapi import APIRouter, Header, HTTPException, Response, status
from app.api.schemas import PaymentRequest, PaymentResponse, ErrorResponse
from app.services.payment_service import (
    PaymentService,
    ReservationNotFoundError,
    InvalidReservationStateError,
)

router = APIRouter(tags=["Payments"])


@router.post(
    "/pay",
    response_model=PaymentResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"model": PaymentResponse, "description": "Payment successfully initiated and processed"},
        200: {"model": PaymentResponse, "description": "Duplicate request returned original payment result"},
        400: {"model": ErrorResponse, "description": "Missing Idempotency-Key or invalid reservation state"},
        404: {"model": ErrorResponse, "description": "Reservation not found"},
    },
)
def process_payment(
    body: PaymentRequest,
    response: Response,
    idempotency_key: Optional[str] = Header(
        None,
        alias="Idempotency-Key",
        description="Mandatory client idempotency key to prevent double charging",
    ),
):
    """Processes payment for an existing reservation.
    
    Guarantees:
    - Mandatory Header: 400 if Idempotency-Key is missing.
    - Idempotency Replay: 200 with original payment result if already charged.
    - Atomic Transitions:
        * SUCCESS: payment=SUCCESS, reservation=CONFIRMED, stock reserved->sold, outbox event created.
        * FAILURE: payment=FAILED, reservation=RELEASED, stock reserved->available.
        * TIMEOUT: payment=UNKNOWN (reconciliation job resolves).
    """
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing Idempotency-Key header",
        )

    service = PaymentService()
    try:
        payment, is_new = service.process_payment(
            reservation_id=body.reservation_id,
            idempotency_key=idempotency_key.strip(),
        )
    except ReservationNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except InvalidReservationStateError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    if not is_new:
        response.status_code = status.HTTP_200_OK

    return PaymentResponse(
        payment_id=payment.payment_id,
        reservation_id=payment.reservation_id,
        status=payment.status,
        gateway_ref=payment.gateway_ref,
    )
