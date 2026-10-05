"""Repository for transactional outbox events."""

import sqlite3
from typing import List, Optional
from app.db import get_connection
from app.models.outbox import OutboxEvent


class OutboxRepository:
    """Handles CRUD operations for the outbox_event table."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path

    def _get_active_conn(self, conn: Optional[sqlite3.Connection]):
        if conn is not None:
            return conn, False
        if self.db_path:
            return get_connection(self.db_path), True
        return get_connection(), True

    def create(
        self, event: OutboxEvent, conn: Optional[sqlite3.Connection] = None
    ) -> OutboxEvent:
        """Inserts a new outbox event."""
        active_conn, is_owned = self._get_active_conn(conn)
        try:
            cursor = active_conn.cursor()
            cursor.execute(
                """
                INSERT INTO outbox_event (event_id, type, payload, status, attempts, created_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (event.event_id, event.type, event.payload, event.status, event.attempts),
            )
            if is_owned:
                active_conn.commit()
            return event
        except Exception:
            if is_owned:
                active_conn.rollback()
            raise
        finally:
            if is_owned:
                active_conn.close()

    def get_pending(
        self, limit: int = 50, conn: Optional[sqlite3.Connection] = None
    ) -> List[OutboxEvent]:
        """Fetch pending outbox events ordered by creation time."""
        active_conn, is_owned = self._get_active_conn(conn)
        try:
            cursor = active_conn.cursor()
            cursor.execute(
                """
                SELECT event_id, type, payload, status, attempts, created_at
                FROM outbox_event
                WHERE status = 'PENDING'
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (limit,),
            )
            rows = cursor.fetchall()
            return [
                OutboxEvent(
                    event_id=row["event_id"],
                    type=row["type"],
                    payload=row["payload"],
                    status=row["status"],
                    attempts=row["attempts"],
                    created_at=row["created_at"],
                )
                for row in rows
            ]
        finally:
            if is_owned:
                active_conn.close()

    def update_status(
        self, event_id: str, status: str, conn: Optional[sqlite3.Connection] = None
    ) -> bool:
        """Update outbox event status."""
        active_conn, is_owned = self._get_active_conn(conn)
        try:
            cursor = active_conn.cursor()
            cursor.execute(
                """
                UPDATE outbox_event
                SET status = ?
                WHERE event_id = ?
                """,
                (status, event_id),
            )
            updated = cursor.rowcount > 0
            if is_owned:
                active_conn.commit()
            return updated
        except Exception:
            if is_owned:
                active_conn.rollback()
            raise
        finally:
            if is_owned:
                active_conn.close()

    def increment_attempts(
        self, event_id: str, conn: Optional[sqlite3.Connection] = None
    ) -> bool:
        """Increment retry attempt counter."""
        active_conn, is_owned = self._get_active_conn(conn)
        try:
            cursor = active_conn.cursor()
            cursor.execute(
                """
                UPDATE outbox_event
                SET attempts = attempts + 1
                WHERE event_id = ?
                """,
                (event_id,),
            )
            updated = cursor.rowcount > 0
            if is_owned:
                active_conn.commit()
            return updated
        except Exception:
            if is_owned:
                active_conn.rollback()
            raise
        finally:
            if is_owned:
                active_conn.close()
