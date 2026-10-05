"""Application configuration settings."""

import os


class Config:
    @classmethod
    def db_path(cls) -> str:
        return os.getenv("SALESTORM_DB_PATH", "salestorm.db")

    @classmethod
    def inventory_strategy(cls) -> str:
        return os.getenv("INVENTORY_STRATEGY", "atomic").lower()

    @classmethod
    def reservation_ttl_seconds(cls) -> int:
        return int(os.getenv("RESERVATION_TTL_SECONDS", "600"))
