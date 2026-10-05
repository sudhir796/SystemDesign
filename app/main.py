"""Main FastAPI application entrypoint."""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.api.reservation_router import router as reservation_router
from app.api.payment_router import router as payment_router
from app.config import Config
from app.db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initializes tables on startup."""
    init_db(Config.db_path())
    yield


app = FastAPI(
    title="SALESTORM Flash Sale Prototype",
    description="High-concurrency prototype demonstrating atomic reservation & concurrency patterns.",
    version="0.1.0",
    lifespan=lifespan,
)

# Register routers
app.include_router(reservation_router)
app.include_router(payment_router)


@app.get("/health")
def health_check():
    """Health check endpoint."""
    return {"status": "ok", "inventory_strategy": Config.inventory_strategy()}


@app.post("/admin/order-service/down")
def simulate_order_service_down(seconds: float = 30.0):
    """Simulates OrderService downtime for the given seconds."""
    from app.services.order_service import get_mock_order_service
    get_mock_order_service().simulate_down_for(seconds)
    return {"status": "order_service_down", "down_for_seconds": seconds}


@app.post("/admin/order-service/restore")
def restore_order_service():
    """Restores OrderService to healthy status."""
    from app.services.order_service import get_mock_order_service
    get_mock_order_service().set_down(False)
    return {"status": "order_service_restored"}


@app.post("/admin/workers/outbox")
def trigger_outbox_worker(limit: int = 100):
    """Triggers a batch of outbox event processing."""
    from app.workers.outbox_worker import OutboxWorker
    processed = OutboxWorker().process_batch(limit=limit)
    return {"processed_orders": processed}


@app.post("/admin/workers/expiry")
def trigger_expiry_worker(limit: int = 100):
    """Triggers a batch of stale reservation expirations."""
    from app.workers.expiry_worker import ExpiryWorker
    expired = ExpiryWorker().process_expired_batch(limit=limit)
    return {"expired_reservations": expired}

