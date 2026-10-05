"""Pydantic schemas for API request and response validation."""

from typing import Optional
from pydantic import BaseModel, Field


class ReserveRequest(BaseModel):
    product_id: str = Field(..., description="Unique product identifier (e.g. PRODUCT_X)")
    customer_id: str = Field(..., description="Unique customer identifier")


class ReserveResponse(BaseModel):
    reservation_id: str
    product_id: str
    customer_id: str
    status: str
    expires_at: str


class PaymentRequest(BaseModel):
    reservation_id: str = Field(..., description="Reservation ID to pay for")


class PaymentResponse(BaseModel):
    payment_id: str
    reservation_id: str
    status: str
    gateway_ref: Optional[str] = None


class ErrorResponse(BaseModel):
    detail: str
