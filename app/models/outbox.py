"""Domain models for transactional outbox events."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class OutboxEvent:
    """Represents an outbox event for reliable asynchronous processing."""
    event_id: str
    type: str
    payload: str
    status: str
    attempts: int = 0
    created_at: Optional[str] = None
