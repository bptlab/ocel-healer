"""Object-side corruption injectors.

Covers all issues rooted in the `object` table and its per-type
sub-tables (`object_<Type>`): missing/incorrect object types, missing or
type-invalid attribute values, and duplicate objects.

Each `inject_*` function introduces exactly one flavor and returns the
affected id(s) so callers can log or reference them. Naming convention:
`inject_<issue_key>_<flavor>`, where `<issue_key>` is the canonical
detector name (e.g. `missing_object_type`, `duplicate_objects_on_ids`).
"""

from __future__ import annotations

import sqlite3

from src.order_management_corruption._common import (
    _capture_and_update,
    _clone_object_row,
    _drop_column,
    _null_type_for,
    inject_missing_attribute_value,
)
from .om_schema import (
    get_om_object_type,
    get_om_object_table,
)


# ---------------------------------------------------------------------------
# Base injectors — building blocks reused by the tiered flavors below.
# ---------------------------------------------------------------------------


def inject_duplicate_objects_on_ids(conn: sqlite3.Connection, ocel_id: str | None = None) -> str | None:
    """duplicate_objects_on_ids: Insert a fully duplicated object row
    (same ocel_id + ocel_type)."""
    if ocel_id:
        row = conn.execute(
            "SELECT ocel_id, ocel_type FROM object WHERE ocel_id = ? LIMIT 1", (ocel_id,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT ocel_id, ocel_type FROM object WHERE ocel_type IS NOT NULL ORDER BY ocel_id LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    conn.execute("INSERT INTO object VALUES (?, ?)", row)
    return row[0]


def inject_incorrect_object_type(
    conn: sqlite3.Connection,
    ocel_id: str,
    incorrect_type: str,
) -> dict | None:
    """incorrect_object_type: Overwrite an object's ocel_type with an
    incorrect-but-non-null value."""
    row = conn.execute(
        "SELECT ocel_type FROM object WHERE ocel_id = ?", (ocel_id,)
    ).fetchone()
    if row is None:
        return None
    old_type = row[0]
    conn.execute(
        "UPDATE object SET ocel_type = ? WHERE ocel_id = ?",
        (incorrect_type, ocel_id),
    )
    return {"affected_ids": [ocel_id], "original_values": {ocel_id: old_type}}


# ---------------------------------------------------------------------------
# missing_object_type (Easy/Hard)
# ---------------------------------------------------------------------------


def inject_missing_object_type_null_employee(conn: sqlite3.Connection) -> dict | None:
    """missing_object_type Easy: NULL the ocel_type of an employee object."""
    obj_type = get_om_object_type("employees")
    row = conn.execute(
        "SELECT ocel_id FROM object WHERE ocel_type = ? ORDER BY ocel_id LIMIT 1", (obj_type,)
    ).fetchone()
    if row is None:
        return None
    ocel_id = row[0]
    old_value = _capture_and_update(conn, "object", "ocel_type", ocel_id, None)
    return {"affected_ids": [ocel_id], "original_values": {ocel_id: old_value}}


def inject_missing_object_type_whitespace_product(conn: sqlite3.Connection) -> dict | None:
    """missing_object_type Hard: Set ocel_type to a whitespace-only string
    on a product."""
    obj_type = get_om_object_type("products")
    row = conn.execute(
        "SELECT ocel_id FROM object WHERE ocel_type = ? ORDER BY ocel_id LIMIT 1", (obj_type,)
    ).fetchone()
    if row is None:
        return None
    ocel_id = row[0]
    old_value = _capture_and_update(conn, "object", "ocel_type", ocel_id, "   ")
    return {"affected_ids": [ocel_id], "original_values": {ocel_id: old_value}}


# ---------------------------------------------------------------------------
# missing_attribute_value (Easy/Hard)
# ---------------------------------------------------------------------------


def inject_missing_attribute_value_null_product_weight(conn: sqlite3.Connection) -> dict:
    """missing_attribute_value Easy: NULL the weight of a product."""
    table = get_om_object_table("object_products")
    row = conn.execute(
        f"SELECT ocel_id FROM {table} WHERE weight IS NOT NULL ORDER BY ocel_id LIMIT 1"
    ).fetchone()
    if row is None:
        return {"affected_ids": [], "original_values": {}}
    ocel_id = row[0]
    old_value = _capture_and_update(conn, table, "weight", ocel_id, None)
    return {"affected_ids": [ocel_id], "original_values": {ocel_id: old_value}}


def inject_missing_attribute_value_null_order_price(conn: sqlite3.Connection) -> dict:
    """missing_attribute_value Hard: NULL price field in order."""
    table = get_om_object_table("object_orders")
    row = conn.execute(
        f"SELECT ocel_id FROM {table} WHERE price IS NOT NULL ORDER BY ocel_id LIMIT 1"
    ).fetchone()
    if row is None:
        return {"affected_ids": [], "original_values": {}}
    ocel_id = row[0]
    old_value = _capture_and_update(conn, table, "price", ocel_id, None)
    return {"affected_ids": [ocel_id], "original_values": {ocel_id: old_value}}


# ---------------------------------------------------------------------------
# incorrect_object_type (LLM-detected, Easy/Hard)
# ---------------------------------------------------------------------------


def inject_incorrect_object_type_swap_order_to_employee(conn: sqlite3.Connection) -> dict | None:
    """incorrect_object_type Easy: Retype an order as an employee."""
    orders_type = get_om_object_type("orders")
    employee_type = get_om_object_type("employees")
    row = conn.execute(
        "SELECT ocel_id FROM object WHERE ocel_type = ? ORDER BY ocel_id LIMIT 1", (orders_type,)
    ).fetchone()
    if row is None:
        return None
    return inject_incorrect_object_type(conn, row[0], employee_type)


def inject_incorrect_object_type_swap_item_to_product(conn: sqlite3.Connection) -> dict | None:
    """incorrect_object_type Hard: Retype an item as a product.

    Hard because items and products are closely related (every item `is a`
    product), so the wrong type is plausible; only the `i-` id shape and
    the item-style relations give it away."""
    items_type = get_om_object_type("items")
    products_type = get_om_object_type("products")
    row = conn.execute(
        "SELECT ocel_id FROM object WHERE ocel_type = ? ORDER BY ocel_id LIMIT 1", (items_type,)
    ).fetchone()
    if row is None:
        return None
    return inject_incorrect_object_type(conn, row[0], products_type)


# ---------------------------------------------------------------------------
# incorrect_attribute_datatype (Easy/Hard)
#
# SQLite type affinity coerces most naive datatype mismatches (e.g. '42' in
# an INTEGER column becomes int 42). The reliable recipes are:
#   * non-numeric strings in REAL/INT columns  -> stored as TEXT
#   * bytes objects in TEXT columns            -> stored as BLOB
# ---------------------------------------------------------------------------


def inject_incorrect_attribute_datatype_string_in_weight(conn: sqlite3.Connection) -> dict | None:
    """incorrect_attribute_datatype Easy: Put `'unknown'` into
    product weight (REAL)."""
    table = get_om_object_table("object_products")
    row = conn.execute(
        f"SELECT ocel_id FROM {table} WHERE weight IS NOT NULL ORDER BY ocel_id LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    ocel_id = row[0]
    old_value = _capture_and_update(conn, table, "weight", ocel_id, "unknown")
    return {"affected_ids": [ocel_id], "original_values": {ocel_id: old_value}}


def inject_incorrect_attribute_datatype_blob_in_role(conn: sqlite3.Connection) -> dict | None:
    """incorrect_attribute_datatype Hard: Store an employee's role as
    UTF-16-LE bytes (BLOB in a TEXT column). Hard because the decoded text
    is the right role."""
    table = get_om_object_table("object_employees")
    row = conn.execute(
        f"SELECT ocel_id, role FROM {table} "
        "WHERE role IS NOT NULL AND ocel_changed_field IS NULL ORDER BY ocel_id LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    ocel_id, role = row
    old_value = _capture_and_update(
        conn, table, "role", ocel_id, str(role).encode("utf-16-le")
    )
    return {"affected_ids": [ocel_id], "original_values": {ocel_id: old_value}}


# ---------------------------------------------------------------------------
# duplicate_objects_on_ids (Easy/Hard)
# ---------------------------------------------------------------------------


def inject_duplicate_objects_on_ids_product(conn: sqlite3.Connection) -> str | None:
    """duplicate_objects_on_ids Easy: Duplicate a product row in object."""
    products_type = get_om_object_type("products")
    row = conn.execute(
        "SELECT ocel_id FROM object WHERE ocel_type = ? ORDER BY ocel_id LIMIT 1", (products_type,)
    ).fetchone()
    if row is None:
        return None
    return inject_duplicate_objects_on_ids(conn, ocel_id=row[0])


def inject_duplicate_objects_on_ids_triple_conflicting_type(conn: sqlite3.Connection) -> str | None:
    """duplicate_objects_on_ids Hard: Add two extra rows for one customer,
    one of them under a conflicting type (`orders`), so a resolver can't
    just drop identical copies."""
    customers_type = get_om_object_type("customers")
    orders_type = get_om_object_type("orders")
    row = conn.execute(
        "SELECT ocel_id FROM object WHERE ocel_type = ? ORDER BY ocel_id LIMIT 1", (customers_type,)
    ).fetchone()
    if row is None:
        return None
    ocel_id = row[0]
    conn.execute("INSERT INTO object VALUES (?, ?)", (ocel_id, orders_type))
    conn.execute("INSERT INTO object VALUES (?, ?)", (ocel_id, customers_type))
    return ocel_id


# ---------------------------------------------------------------------------
# duplicate_objects_on_attributes (Easy/Hard)
# ---------------------------------------------------------------------------


def inject_duplicate_objects_on_attributes_clone_product(conn: sqlite3.Connection) -> str | None:
    """duplicate_objects_on_attributes Easy: Clone a product's initial-state
    row under a fabricated id."""
    products_type = get_om_object_type("products")
    table = get_om_object_table("object_products")
    row = conn.execute(
        f"SELECT ocel_id FROM {table} ORDER BY ocel_id LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return _clone_object_row(
        conn,
        source_id=row[0],
        clone_id=f"{row[0]}-CLONE",
        ocel_type=products_type,
        table=table,
    )


def inject_duplicate_objects_on_attributes_clone_order_and_referenced(conn: sqlite3.Connection) -> list[str]:
    """duplicate_objects_on_attributes Hard: Clone an order AND copy its
    event_object rows."""
    orders_type = get_om_object_type("orders")
    table = get_om_object_table("object_orders")

    # An order whose price no other order shares, so the clone forms a new
    # duplicate group instead of growing an existing one; OFFSET 1 skips the
    # order missing_attribute_value nulls in the same tier.
    row = conn.execute(
        f"SELECT ocel_id FROM {table} WHERE price IN ("
        f"  SELECT price FROM {table} WHERE price IS NOT NULL "
        f"  GROUP BY price HAVING COUNT(*) = 1"
        f") ORDER BY ocel_id LIMIT 1 OFFSET 1"
    ).fetchone()
    if row is None:
        return []

    original = row[0]
    clone = f"{original}-DUP"

    if not _clone_object_row(
        conn,
        source_id=original,
        clone_id=clone,
        ocel_type=orders_type,
        table=table,
    ):
        return []
    conn.execute(
        "INSERT INTO event_object (ocel_event_id, ocel_object_id, ocel_qualifier) "
        "SELECT ocel_event_id, ?, ocel_qualifier FROM event_object "
        "WHERE ocel_object_id = ?",
        (clone, original),
    )
    return [original, clone]


# ---------------------------------------------------------------------------
# incorrect_attribute_value (LLM detection, Easy/Hard)
# ---------------------------------------------------------------------------


def inject_incorrect_object_attribute_value_negative_weight_easy(conn: sqlite3.Connection) -> dict | None:
    """incorrect_attribute_value Easy: Set a product's weight to -999 (obviously wrong)."""
    table = get_om_object_table("object_products")
    row = conn.execute(
        f"SELECT ocel_id FROM {table} WHERE weight > 0 AND ocel_changed_field IS NULL ORDER BY ocel_id LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    ocel_id = row[0]
    old_value = _capture_and_update(conn, table, "weight", ocel_id, -999.0)
    return {"affected_ids": [ocel_id], "original_values": {ocel_id: old_value}}


def inject_incorrect_object_attribute_value_implausible_price_hard(conn: sqlite3.Connection) -> dict | None:
    """incorrect_attribute_value Hard: Multiply a product's price by 1000
    (right sign and format, implausible magnitude)."""
    table = get_om_object_table("object_products")
    row = conn.execute(
        f"SELECT ocel_id, price FROM {table} "
        "WHERE price > 0 AND ocel_changed_field IS NULL ORDER BY ocel_id LIMIT 1 OFFSET 1"
    ).fetchone()
    if row is None:
        return None
    ocel_id, price = row
    old_value = _capture_and_update(conn, table, "price", ocel_id, round(price * 1000, 2))
    return {"affected_ids": [ocel_id], "original_values": {ocel_id: old_value}}


# ---------------------------------------------------------------------------
# missing_object_attribute (LLM detection, Easy/Hard)
#
# Schema-level: a whole attribute column is dropped from a type table.
# ---------------------------------------------------------------------------


def inject_missing_object_attribute_drop_price_easy(conn: sqlite3.Connection) -> dict:
    """missing_object_attribute Easy: Drop `price` from Orders. Easy because
    an order without a price is an obvious gap and Items/Products still
    carry `price`."""
    table = get_om_object_table("object_orders")
    return {"table": table, "column": "price",
            "original_values": _drop_column(conn, table, "price")}


def inject_missing_object_attribute_drop_weight_hard(conn: sqlite3.Connection) -> dict:
    """missing_object_attribute Hard: Drop `weight` from Packages, leaving
    the table with no attributes at all. Hard because whether a package
    should record its weight is domain knowledge, not visible from peers."""
    table = get_om_object_table("object_packages")
    return {"table": table, "column": "weight",
            "original_values": _drop_column(conn, table, "weight")}
