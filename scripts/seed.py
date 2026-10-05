"""Seed script to initialize database and set Product X to 100 units."""

import os
import sys

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db import init_db, DB_PATH
from app.models.inventory import Inventory
from app.repositories.inventory_repo import SqliteInventoryRepository


def seed(db_path: str = DB_PATH, product_id: str = "PRODUCT_X", units: int = 100) -> None:
    """Initializes tables and seeds the product inventory."""
    print(f"Initializing database at: {db_path}...")
    init_db(db_path)
    
    repo = SqliteInventoryRepository(db_path=db_path)
    
    initial_inventory = Inventory(
        inventory_id=f"inv_{product_id.lower()}",
        product_id=product_id,
        total_stock=units,
        available_quantity=units,
        reserved_quantity=0,
        sold_quantity=0,
        version=0,
    )
    
    repo.upsert(initial_inventory)
    seeded = repo.get_by_product_id(product_id)
    print(f"Successfully seeded {product_id}:")
    print(f"  Total Stock: {seeded.total_stock}")
    print(f"  Available:   {seeded.available_quantity}")
    print(f"  Reserved:    {seeded.reserved_quantity}")
    print(f"  Sold:        {seeded.sold_quantity}")
    print(f"  Conservation invariant holds: {seeded.validate_conservation()}")


if __name__ == "__main__":
    target_db = os.getenv("SALESTORM_DB_PATH", DB_PATH)
    seed(db_path=target_db)
