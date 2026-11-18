"""Schema-level corruption injectors.

Covers attributes dropped from a per-type sub-table with
``ALTER TABLE ... DROP COLUMN`` (SQLite ≥ 3.35): `missing_object_attribute`
and `missing_event_attribute`. A dropped column exists once per log, so
these injectors ignore `picker.n` and always return a single `Injection`.
Only columns no other injector corrupts are dropped, so a combined log
keeps every other injection intact. `SCHEMA_INJECTORS` maps
``(issue_key, tier)`` to the injector.
"""

from __future__ import annotations

import sqlite3
from typing import Callable

from src.order_management_corruption._common import (
    Injection,
    TargetPicker,
    _type_table,
)


def _drop_column(
    conn: sqlite3.Connection, *,
    issue: str, tier: str, flavor: str, kind: str, ocel_type: str, column: str,
) -> list[Injection]:
    table = _type_table(conn, kind, ocel_type)
    declared = {r[1]: r[2] for r in conn.execute(f'PRAGMA table_info("{table}")')}[column]
    values = [list(r) for r in conn.execute(f'SELECT ocel_id, "{column}" FROM "{table}" ORDER BY rowid')]
    conn.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
    type_key = f"{kind}_type"
    return [Injection(
        issue, tier, flavor, "schema", {"table": table, "attribute": column},
        original={"declared_type": declared, "values": values}, corrupted=None,
        expected_rows=[{"issue": issue, type_key: ocel_type, "attribute": column}],
        accept={"mode": "exact", "value": column, "declared_type": declared},
    )]


def inject_missing_object_attribute_item_weight_easy(conn, picker):
    """missing_object_attribute Easy: drop `weight` from items; the product
    each item `is a` has one, and packages do too."""
    return _drop_column(conn, issue="missing_object_attribute", tier="easy", flavor="item_weight",
                        kind="object", ocel_type="items", column="weight")


def inject_missing_object_attribute_package_weight_hard(conn, picker):
    """missing_object_attribute Hard: drop `weight` from packages; no
    sibling type has the same shape, so it must come from domain knowledge
    (a package weighs roughly its items)."""
    return _drop_column(conn, issue="missing_object_attribute", tier="hard", flavor="package_weight",
                        kind="object", ocel_type="packages", column="weight")


def inject_missing_event_attribute_reorder_resource_easy(conn, picker):
    """missing_event_attribute Easy: drop `resource` from reorder item; its
    sibling item out of stock has one, and the `employee` link names it."""
    return _drop_column(conn, issue="missing_event_attribute", tier="easy",
                        flavor="reorder_resource",
                        kind="event", ocel_type="reorder item", column="resource")


def inject_missing_event_attribute_pay_amount_hard(conn, picker):
    """missing_event_attribute Hard: drop `amount` from pay order; no other
    event type carries it, so it must be inferred from the domain."""
    return _drop_column(conn, issue="missing_event_attribute", tier="hard", flavor="pay_amount",
                        kind="event", ocel_type="pay order", column="amount")


SCHEMA_INJECTORS: dict[tuple[str, str], Callable[[sqlite3.Connection, TargetPicker], list[Injection]]] = {
    ("missing_object_attribute", "easy"): inject_missing_object_attribute_item_weight_easy,
    ("missing_object_attribute", "hard"): inject_missing_object_attribute_package_weight_hard,
    ("missing_event_attribute", "easy"):  inject_missing_event_attribute_reorder_resource_easy,
    ("missing_event_attribute", "hard"):  inject_missing_event_attribute_pay_amount_hard,
}
