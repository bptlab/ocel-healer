"""Object-side corruption injectors.

Covers all issues rooted in the `object` table and its per-type
sub-tables (`object_<Type>`): missing/incorrect object types, missing or
type-invalid attribute values, and duplicate objects.

Each `inject_<issue_key>_<flavor>(conn, picker)` corrupts `picker.n`
seeded, non-overlapping targets and returns one `Injection` per target
(never an empty list — the picker raises instead). `OBJECT_INJECTORS`
maps ``(issue_key, tier)`` to the injector.
"""

from __future__ import annotations

import sqlite3
from typing import Callable

from src.order_management_corruption._common import (
    NUMERIC_TOLERANCE,
    Injection,
    TargetPicker,
    _capture_and_update,
    _clone_object_row,
    _remove_object_primary_key,
)


def _accept_value(value: object) -> dict:
    """Repair rule for restoring a single attribute value."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {"mode": "numeric_tol", "value": value, "tol": NUMERIC_TOLERANCE}
    return {"mode": "exact", "value": value}


# ---------------------------------------------------------------------------
# missing_object_type (Easy/Medium/Hard)
# ---------------------------------------------------------------------------


def _missing_object_type(
    conn: sqlite3.Connection, picker: TargetPicker, *,
    tier: str, flavor: str, ocel_type: str, set_to: str | None,
) -> list[Injection]:
    out = []
    for oid in picker.pick("SELECT ocel_id FROM object WHERE ocel_type = ?", (ocel_type,)):
        old = _capture_and_update(conn, "object", "ocel_type", oid, set_to)
        out.append(Injection(
            "missing_object_type", tier, flavor, "object", oid,
            original=old, corrupted=set_to,
            expected_rows=[{"issue": "missing_object_type", "ocel_id": oid}],
            accept={"mode": "exact", "value": old},
        ))
    return out


def inject_missing_object_type_null_employee(conn, picker):
    """missing_object_type Easy: NULL the ocel_type of an employee."""
    return _missing_object_type(conn, picker, tier="easy", flavor="null_employee",
                                ocel_type="employees", set_to=None)


def inject_missing_object_type_empty_string_order(conn, picker):
    """missing_object_type Medium: set an order's ocel_type to ''."""
    return _missing_object_type(conn, picker, tier="medium", flavor="empty_string_order",
                                ocel_type="orders", set_to="")


def inject_missing_object_type_whitespace_product(conn, picker):
    """missing_object_type Hard: set a product's ocel_type to whitespace."""
    return _missing_object_type(conn, picker, tier="hard", flavor="whitespace_product",
                                ocel_type="products", set_to="   ")


# ---------------------------------------------------------------------------
# Attribute-value corruptions on initial-state rows:
# missing_attribute_value and incorrect_attribute_datatype (Easy/Medium/Hard)
#
# SQLite type affinity coerces most naive datatype mismatches (e.g. '42' in
# an INTEGER column becomes int 42). The reliable recipes are:
#   * non-numeric strings in REAL/INT columns  -> stored as TEXT
#   * bytes objects in TEXT columns            -> stored as BLOB
# ---------------------------------------------------------------------------


def _corrupt_attribute(
    conn: sqlite3.Connection, picker: TargetPicker, *,
    issue: str, tier: str, flavor: str, object_type: str, table: str, column: str,
    corrupt: Callable[[object], object],
) -> list[Injection]:
    out = []
    ids = picker.pick(
        f'SELECT ocel_id FROM "{table}" '
        f'WHERE ocel_changed_field IS NULL AND "{column}" IS NOT NULL'
    )
    for oid in ids:
        old = conn.execute(
            f'SELECT "{column}" FROM "{table}" WHERE ocel_id = ? AND ocel_changed_field IS NULL',
            (oid,),
        ).fetchone()[0]
        new = corrupt(old)
        _capture_and_update(conn, table, column, oid, new, where_initial=True)
        out.append(Injection(
            issue, tier, flavor, "object_attr", oid,
            original={"object_type": object_type, "attribute": column, "value": old},
            corrupted=new,
            expected_rows=[{"issue": issue, "ocel_id": oid, "attribute": column}],
            accept=_accept_value(old),
        ))
    return out


def inject_missing_attribute_value_null_product_weight(conn, picker):
    """missing_attribute_value Easy: NULL a product's weight."""
    return _corrupt_attribute(conn, picker, issue="missing_attribute_value", tier="easy",
                              flavor="null_product_weight", object_type="products",
                              table="object_Products", column="weight", corrupt=lambda v: None)


def inject_missing_attribute_value_empty_string_role(conn, picker):
    """missing_attribute_value Medium: set an employee's role to ''."""
    return _corrupt_attribute(conn, picker, issue="missing_attribute_value", tier="medium",
                              flavor="empty_string_role", object_type="employees",
                              table="object_Employees", column="role", corrupt=lambda v: "")


def inject_missing_attribute_value_null_order_price(conn, picker):
    """missing_attribute_value Hard: NULL an order's price."""
    return _corrupt_attribute(conn, picker, issue="missing_attribute_value", tier="hard",
                              flavor="null_order_price", object_type="orders",
                              table="object_Orders", column="price", corrupt=lambda v: None)


def inject_incorrect_attribute_datatype_string_in_weight(conn, picker):
    """incorrect_attribute_datatype Easy: 'unknown' in a product's weight (REAL)."""
    return _corrupt_attribute(conn, picker, issue="incorrect_attribute_datatype", tier="easy",
                              flavor="string_in_weight", object_type="products",
                              table="object_Products", column="weight", corrupt=lambda v: "unknown")


def inject_incorrect_attribute_datatype_string_in_order_price(conn, picker):
    """incorrect_attribute_datatype Medium: 'TBD' in an order's price (REAL)."""
    return _corrupt_attribute(conn, picker, issue="incorrect_attribute_datatype", tier="medium",
                              flavor="string_in_order_price", object_type="orders",
                              table="object_Orders", column="price", corrupt=lambda v: "TBD")


def inject_incorrect_attribute_datatype_blob_in_role(conn, picker):
    """incorrect_attribute_datatype Hard: an employee's own role stored as
    UTF-16-LE bytes (BLOB) in the TEXT column."""
    return _corrupt_attribute(conn, picker, issue="incorrect_attribute_datatype", tier="hard",
                              flavor="blob_in_role", object_type="employees",
                              table="object_Employees", column="role",
                              corrupt=lambda v: v.encode("utf-16-le"))


# ---------------------------------------------------------------------------
# incorrect_object_type (LLM-detected, Easy/Medium/Hard)
#
# Only OM types that have a sub-table are used, so no rule detector fires.
# The LLM sweep is scoped by type: the harness sweeps the type the object
# was moved *to*, recorded as `ocel_type` in `expected_rows`.
# ---------------------------------------------------------------------------


def _incorrect_object_type(
    conn: sqlite3.Connection, picker: TargetPicker, *,
    tier: str, flavor: str, from_type: str, to_type: str,
) -> list[Injection]:
    out = []
    for oid in picker.pick("SELECT ocel_id FROM object WHERE ocel_type = ?", (from_type,)):
        _capture_and_update(conn, "object", "ocel_type", oid, to_type)
        out.append(Injection(
            "incorrect_object_type", tier, flavor, "object", oid,
            original=from_type, corrupted=to_type,
            expected_rows=[{"issue": "incorrect_object_type", "ocel_id": oid, "ocel_type": to_type}],
            accept={"mode": "exact", "value": from_type},
        ))
    return out


def inject_incorrect_object_type_order_to_employee(conn, picker):
    """incorrect_object_type Easy: retype an order as an employee."""
    return _incorrect_object_type(conn, picker, tier="easy", flavor="order_to_employee",
                                  from_type="orders", to_type="employees")


def inject_incorrect_object_type_item_to_product(conn, picker):
    """incorrect_object_type Medium: retype an item as a product."""
    return _incorrect_object_type(conn, picker, tier="medium", flavor="item_to_product",
                                  from_type="items", to_type="products")


def inject_incorrect_object_type_package_to_item(conn, picker):
    """incorrect_object_type Hard: retype a package as an item — an existing
    type whose E2O/O2O pattern contradicts it."""
    return _incorrect_object_type(conn, picker, tier="hard", flavor="package_to_item",
                                  from_type="packages", to_type="items")


# ---------------------------------------------------------------------------
# duplicate_objects_on_ids (Easy/Medium/Hard)
# ---------------------------------------------------------------------------


def _duplicate_object_ids(
    conn: sqlite3.Connection, picker: TargetPicker, *,
    tier: str, flavor: str, ocel_type: str, extra_types: list[str | None],
) -> list[Injection]:
    _remove_object_primary_key(conn)
    out = []
    for oid in picker.pick("SELECT ocel_id FROM object WHERE ocel_type = ?", (ocel_type,)):
        conn.executemany("INSERT INTO object VALUES (?, ?)", [(oid, t) for t in extra_types])
        expected = [{"issue": "duplicate_objects_on_ids", "ocel_id": oid}]
        if None in extra_types:
            expected.append({"issue": "missing_object_type", "ocel_id": oid})
        out.append(Injection(
            "duplicate_objects_on_ids", tier, flavor, "object", oid,
            original={"ocel_type": ocel_type},
            corrupted={"added_types": extra_types},
            expected_rows=expected,
            accept={"mode": "delete_any_of", "ids": [oid], "keep": {"ocel_type": ocel_type}},
        ))
    return out


def inject_duplicate_objects_on_ids_product(conn, picker):
    """duplicate_objects_on_ids Easy: an identical copy of a product's object row."""
    return _duplicate_object_ids(conn, picker, tier="easy", flavor="product",
                                 ocel_type="products", extra_types=["products"])


def inject_duplicate_objects_on_ids_conflicting_types(conn, picker):
    """duplicate_objects_on_ids Medium: a copy of an order row typed as items."""
    return _duplicate_object_ids(conn, picker, tier="medium", flavor="conflicting_types",
                                 ocel_type="orders", extra_types=["items"])


def inject_duplicate_objects_on_ids_triple_null_type(conn, picker):
    """duplicate_objects_on_ids Hard: two extra rows for a customer, one with
    a NULL type (also a declared `missing_object_type` hit)."""
    return _duplicate_object_ids(conn, picker, tier="hard", flavor="triple_null_type",
                                 ocel_type="customers", extra_types=[None, "customers"])


# ---------------------------------------------------------------------------
# duplicate_objects_on_attributes (Easy/Medium/Hard)
# ---------------------------------------------------------------------------


def _duplicate_object_attributes(
    conn: sqlite3.Connection, picker: TargetPicker, *,
    tier: str, flavor: str, ocel_type: str, table: str,
    clone_id: Callable[[str], str], copy_e2o: bool = False,
) -> list[Injection]:
    out = []
    for oid in picker.pick("SELECT ocel_id FROM object WHERE ocel_type = ?", (ocel_type,)):
        clone = clone_id(oid)
        picker.touch(clone)
        _clone_object_row(conn, source_id=oid, clone_id=clone, ocel_type=ocel_type, table=table)
        e2o = []
        if copy_e2o:
            e2o = conn.execute(
                "SELECT ocel_event_id, ocel_qualifier FROM event_object WHERE ocel_object_id = ?",
                (oid,),
            ).fetchall()
            conn.executemany(
                "INSERT INTO event_object (ocel_event_id, ocel_object_id, ocel_qualifier) "
                "VALUES (?, ?, ?)",
                [(ev, clone, q) for ev, q in e2o],
            )
        out.append(Injection(
            "duplicate_objects_on_attributes", tier, flavor, "object", [oid, clone],
            original={"ocel_id": oid},
            corrupted={"clone_id": clone, "copied_e2o": len(e2o)},
            expected_rows=[{"issue": "duplicate_objects_on_attributes", "ocel_ids": [oid, clone]}],
            accept={"mode": "delete_any_of", "ids": [oid, clone]},
        ))
    return out


def inject_duplicate_objects_on_attributes_clone_product(conn, picker):
    """duplicate_objects_on_attributes Easy: clone a product's initial-state
    row under a near-miss name."""
    return _duplicate_object_attributes(conn, picker, tier="easy", flavor="clone_product",
                                        ocel_type="products", table="object_Products",
                                        clone_id=picker.near_miss)


def inject_duplicate_objects_on_attributes_clone_employee(conn, picker):
    """duplicate_objects_on_attributes Medium: clone an employee under a
    near-miss name — duplicate master data."""
    return _duplicate_object_attributes(conn, picker, tier="medium", flavor="clone_employee",
                                        ocel_type="employees", table="object_Employees",
                                        clone_id=picker.near_miss)


def inject_duplicate_objects_on_attributes_clone_order_and_referenced(conn, picker):
    """duplicate_objects_on_attributes Hard: clone an order under the next
    free order id and copy its E2O rows."""
    return _duplicate_object_attributes(conn, picker, tier="hard", flavor="clone_order_and_referenced",
                                        ocel_type="orders", table="object_Orders",
                                        clone_id=lambda _: picker.new_id("o-"), copy_e2o=True)


OBJECT_INJECTORS: dict[tuple[str, str], Callable[[sqlite3.Connection, TargetPicker], list[Injection]]] = {
    ("missing_object_type", "easy"):              inject_missing_object_type_null_employee,
    ("missing_object_type", "medium"):            inject_missing_object_type_empty_string_order,
    ("missing_object_type", "hard"):              inject_missing_object_type_whitespace_product,
    ("missing_attribute_value", "easy"):          inject_missing_attribute_value_null_product_weight,
    ("missing_attribute_value", "medium"):        inject_missing_attribute_value_empty_string_role,
    ("missing_attribute_value", "hard"):          inject_missing_attribute_value_null_order_price,
    ("incorrect_attribute_datatype", "easy"):     inject_incorrect_attribute_datatype_string_in_weight,
    ("incorrect_attribute_datatype", "medium"):   inject_incorrect_attribute_datatype_string_in_order_price,
    ("incorrect_attribute_datatype", "hard"):     inject_incorrect_attribute_datatype_blob_in_role,
    ("incorrect_object_type", "easy"):            inject_incorrect_object_type_order_to_employee,
    ("incorrect_object_type", "medium"):          inject_incorrect_object_type_item_to_product,
    ("incorrect_object_type", "hard"):            inject_incorrect_object_type_package_to_item,
    ("duplicate_objects_on_ids", "easy"):         inject_duplicate_objects_on_ids_product,
    ("duplicate_objects_on_ids", "medium"):       inject_duplicate_objects_on_ids_conflicting_types,
    ("duplicate_objects_on_ids", "hard"):         inject_duplicate_objects_on_ids_triple_null_type,
    ("duplicate_objects_on_attributes", "easy"):  inject_duplicate_objects_on_attributes_clone_product,
    ("duplicate_objects_on_attributes", "medium"): inject_duplicate_objects_on_attributes_clone_employee,
    ("duplicate_objects_on_attributes", "hard"):  inject_duplicate_objects_on_attributes_clone_order_and_referenced,
}
