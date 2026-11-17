"""Enrich the Order Management baseline with event attributes.

The OM log ships with events that only carry `ocel_time`, so the
event-attribute issue types (missing value, wrong datatype, wrong value,
missing column) have nothing to corrupt. This script derives attributes
from data already in the log:

- `resource TEXT`: the single employee linked to the event
  (pick item / item out of stock / reorder item via `employee`,
  confirm order via `sales person`, create package via `packer`,
  failed delivery / package delivered / send package via `shipper`)
- `item_count INTEGER` on place order: number of `item` E2O links
- `amount REAL` on pay order: the paid order's initial `price`

Payment reminder gets nothing. Idempotent: columns that already exist are
skipped.

    python -m src.order_management_corruption.enrich_baseline [path]
"""

from __future__ import annotations

import sqlite3
import sys

from ._common import DEFAULT_CLEAN_PATH

# event table -> E2O qualifier of the linked employee
RESOURCE_QUALIFIERS = {
    "event_PickItem": "employee",
    "event_ItemOutOfStock": "employee",
    "event_ReorderItem": "employee",
    "event_ConfirmOrder": "sales person",
    "event_CreatePackage": "packer",
    "event_FailedDelivery": "shipper",
    "event_PackageDelivered": "shipper",
    "event_SendPackage": "shipper",
}


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f'PRAGMA table_info("{table}")'))


def enrich_baseline(path: str = DEFAULT_CLEAN_PATH) -> list[str]:
    """Add the derived event attributes to `path`. Returns the
    `table.column` names that were added (empty if already enriched)."""
    added: list[str] = []
    conn = sqlite3.connect(path)
    try:
        with conn:
            for table, qualifier in RESOURCE_QUALIFIERS.items():
                if _has_column(conn, table, "resource"):
                    continue
                conn.execute(f'ALTER TABLE "{table}" ADD COLUMN resource TEXT')
                conn.execute(
                    f"""UPDATE "{table}" SET resource = (
                            SELECT eo.ocel_object_id FROM event_object eo
                            WHERE eo.ocel_event_id = "{table}".ocel_id
                              AND eo.ocel_qualifier = ?)""",
                    (qualifier,),
                )
                added.append(f"{table}.resource")

            if not _has_column(conn, "event_PlaceOrder", "item_count"):
                conn.execute('ALTER TABLE "event_PlaceOrder" ADD COLUMN item_count INTEGER')
                conn.execute(
                    """UPDATE "event_PlaceOrder" SET item_count = (
                           SELECT COUNT(*) FROM event_object eo
                           WHERE eo.ocel_event_id = "event_PlaceOrder".ocel_id
                             AND eo.ocel_qualifier = 'item')"""
                )
                added.append("event_PlaceOrder.item_count")

            if not _has_column(conn, "event_PayOrder", "amount"):
                conn.execute('ALTER TABLE "event_PayOrder" ADD COLUMN amount REAL')
                conn.execute(
                    """UPDATE "event_PayOrder" SET amount = (
                           SELECT o.price FROM event_object eo
                           JOIN "object_Orders" o ON o.ocel_id = eo.ocel_object_id
                           WHERE eo.ocel_event_id = "event_PayOrder".ocel_id
                             AND eo.ocel_qualifier = 'order'
                             AND o.ocel_changed_field IS NULL)"""
                )
                added.append("event_PayOrder.amount")
    finally:
        conn.close()
    return added


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CLEAN_PATH
    added = enrich_baseline(target)
    print(f"Added {len(added)} column(s) to {target}" + (":\n  " + "\n  ".join(added) if added else " (already enriched)"))
