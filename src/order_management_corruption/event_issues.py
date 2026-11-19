"""Event-side corruption injectors.

Covers issues rooted in the `event` table and its per-type sub-tables:
E2O rows referencing non-existent events (`missing_event`), `event.ocel_type`
values that are NULL / empty / whitespace (`missing_event_type`), and
NULL / empty `ocel_time` values on `event_<Type>` rows
(`missing_event_timestamp`).

Each `inject_*` function introduces exactly one flavor and returns the
affected id(s) so callers can log or reference them.
"""

from __future__ import annotations

import sqlite3

from src.order_management_corruption._common import _capture_and_update, _drop_column
from .om_schema import (
    get_om_event_table,
    get_om_event_type,
    get_om_object_type,
    get_om_e2o_qualifier,
)


# ---------------------------------------------------------------------------
# missing_event_timestamp — a row in `event_<Type>` has NULL or empty
# ocel_time. Rule detector via UNION over event sub-tables; LLM resolver
# interpolates from neighbor events touching the same object(s).
# ---------------------------------------------------------------------------


def _pick_event_with_relations(
    conn: sqlite3.Connection, table: str, *, require_neighbor: bool = False
) -> str | None:
    """Pick an `ocel_id` from `table` that's actually resolvable, instead of
    blindly grabbing the first row.

    Without this, `SELECT ocel_id FROM {table} LIMIT 1` (no ORDER BY, no
    relational check) deterministically returns whichever row SQLite
    happens to store first -- which can easily be an event with zero
    `event_object` rows at all, making the resulting "infer the missing
    timestamp from neighbor events" task impossible by construction, not
    merely hard.

    `require_neighbor=True` (used for "easy") additionally requires at
    least one OTHER event to share an object with the candidate, so the
    tier's own premise -- "clear bracketing signal" -- actually holds.
    "medium"/"hard" only require the event isn't fully orphaned, preserving
    their intended sparse-signal difficulty rather than guaranteeing a
    clean bracket.
    """
    if require_neighbor:
        query = f"""
            SELECT DISTINCT e.ocel_id
            FROM "{table}" e
            JOIN event_object eo ON eo.ocel_event_id = e.ocel_id
            JOIN event_object eo2
                ON eo2.ocel_object_id = eo.ocel_object_id
               AND eo2.ocel_event_id != e.ocel_id
            ORDER BY e.ocel_id
            LIMIT 1
        """
    else:
        query = f"""
            SELECT e.ocel_id
            FROM "{table}" e
            WHERE EXISTS (
                SELECT 1 FROM event_object eo WHERE eo.ocel_event_id = e.ocel_id
            )
            ORDER BY e.ocel_id
            LIMIT 1
        """
    row = conn.execute(query).fetchone()
    return row[0] if row else None


def inject_missing_event_timestamp_null_place_order_easy(conn: sqlite3.Connection) -> dict | None:
    """missing_event_timestamp Easy: NULL the ocel_time on one `event_PlaceOrder` row.

    Easy because `place order` is the first event in the lifecycle, so its
    neighbors are all upper bounds — the resolver has a clear "must be
    before X" signal from the order's later events. The candidate is
    required to actually have such a neighbor (see
    `_pick_event_with_relations`) so that promise holds in practice.
    """
    table = get_om_event_table("event_PlaceOrder")
    ocel_id = _pick_event_with_relations(conn, table, require_neighbor=True)
    if ocel_id is None:
        return None
    old_value = _capture_and_update(conn, table, "ocel_time", ocel_id, None)
    return {"affected_ids": [ocel_id], "original_values": {ocel_id: old_value}}


def inject_missing_event_timestamp_null_item_out_of_stock_hard(conn: sqlite3.Connection) -> dict | None:
    """missing_event_timestamp Hard: NULL the ocel_time on one `event_ItemOutOfStock` row.

    Hard because `item out of stock` is a rare, off-happy-path event with
    sparse peer signal — bracketing must come from the item's own history
    rather than a typical order lifecycle. Still requires at least one
    `event_object` relation to exist so the task is merely hard, not
    unsolvable: a fully orphaned event isn't a harder reasoning case, it's
    a degenerate one no method could bracket.
    """
    table = get_om_event_table("event_ItemOutOfStock")
    ocel_id = _pick_event_with_relations(conn, table, require_neighbor=False)
    if ocel_id is None:
        return None
    old_value = _capture_and_update(conn, table, "ocel_time", ocel_id, None)
    return {"affected_ids": [ocel_id], "original_values": {ocel_id: old_value}}


# ---------------------------------------------------------------------------
# missing_event — an event_object row references an event id that has no
# row in the `event` table. Rule detector via LEFT JOIN; resolver INSERTs
# the event row (plus an initial-state row in the per-type sub-table).
# ---------------------------------------------------------------------------


def _delete_event(conn: sqlite3.Connection, ocel_id: str, ocel_type: str) -> None:
    """Remove an event from `event` and from its per-type table, so it is
    gone entirely and only its E2O rows remain."""
    conn.execute("DELETE FROM event WHERE ocel_id = ?", (ocel_id,))
    row = conn.execute(
        "SELECT ocel_type_map FROM event_map_type WHERE ocel_type = ?", (ocel_type,)
    ).fetchone()
    if row:
        conn.execute(f'DELETE FROM "event_{row[0]}" WHERE ocel_id = ?', (ocel_id,))


def inject_missing_event_place_order_easy(conn: sqlite3.Connection) -> dict | None:
    """Missing event, Easy: delete a real 'place order' event that is still
    referenced by its E2O row (qualifier `order`), leaving that row dangling.

    Easy because `place order` is the first event in the lifecycle, so the
    linked order object is still intact, and the surviving E2O qualifier
    gives a clear signal for what event type belongs there.
    """
    place_type = get_om_event_type("place order")
    order_qual = get_om_e2o_qualifier("order")
    row = conn.execute(
        "SELECT e.ocel_id, e.ocel_type FROM event e "
        "JOIN event_object eo ON eo.ocel_event_id = e.ocel_id "
        "WHERE e.ocel_type = ? AND eo.ocel_qualifier = ? "
        "ORDER BY e.ocel_id LIMIT 1 OFFSET 1",  # not the event missing_event_timestamp uses
        (place_type, order_qual),
    ).fetchone()
    if row is None:
        return None
    ocel_id, ocel_type = row
    _delete_event(conn, ocel_id, ocel_type)
    return {"affected_ids": [ocel_id], "original_values": {ocel_id: ocel_type}}


def inject_missing_event_bare_id_hard(conn: sqlite3.Connection) -> dict | None:
    """Missing event, Hard: delete a real 'create package' event still
    referenced by E2O rows, leaving them dangling.

    Hard because package ids are opaque (`p-…`) and `create package`,
    `send package` and `package delivered` all touch the same package —
    the resolver must infer the event type from the qualifiers (`creates`,
    `packer`) rather than from a lifecycle-anchoring object like an order.
    """
    event_type = get_om_event_type("create package")
    qualifier = get_om_e2o_qualifier("creates")
    row = conn.execute(
        "SELECT e.ocel_id, e.ocel_type FROM event e "
        "JOIN event_object eo ON eo.ocel_event_id = e.ocel_id "
        "WHERE e.ocel_type = ? AND eo.ocel_qualifier = ? ORDER BY e.ocel_id LIMIT 1",
        (event_type, qualifier),
    ).fetchone()
    if row is None:
        return None
    ocel_id, ocel_type = row
    _delete_event(conn, ocel_id, ocel_type)
    return {"affected_ids": [ocel_id], "original_values": {ocel_id: ocel_type}}


# ---------------------------------------------------------------------------
# missing_event_type — a row in `event` has NULL or empty ocel_type.
# Rule detector; LLM resolver UPDATEs `event.ocel_type` from the objects
# the event touches, its id, and its timestamp.
# ---------------------------------------------------------------------------


def inject_missing_event_type_null_confirm_easy(conn: sqlite3.Connection) -> str | None:
    """Missing event type, Easy: NULL the ocel_type of one `confirm order` event.

    Easy because the event's id embeds the activity keyword
    (`confirm order:…`), giving the LLM a strong single-signal hint at
    the type. Also linked to one `orders` object with qualifier `order`,
    which corroborates.
    """
    event_type = get_om_event_type("confirm order")
    row = conn.execute(
        "SELECT ocel_id FROM event WHERE ocel_type = ? ORDER BY ocel_id LIMIT 1", (event_type,)
    ).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE event SET ocel_type = NULL WHERE ocel_id = ?", row)
    return row[0]


def inject_missing_event_type_whitespace_package_hard(conn: sqlite3.Connection) -> str | None:
    """Missing event type, Hard: Whitespace-only ocel_type on one `create
    package` event.

    Hard because whitespace passes `NOT NULL AND != ''` naive checks;
    detection requires TRIM. Also because `create package` and
    `send package` both touch package objects — the LLM must
    disambiguate via the id keyword or the qualifier of the packer
    object (`packer` for create, different for send).
    """
    event_type = get_om_event_type("create package")
    row = conn.execute(
        "SELECT ocel_id FROM event WHERE ocel_type = ? ORDER BY ocel_id LIMIT 1", (event_type,)
    ).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE event SET ocel_type = '   ' WHERE ocel_id = ?", row)
    return row[0]


# ---------------------------------------------------------------------------
# Event-attribute issues. The order-management baseline has no event
# attributes; these injectors need the evaluation log built by
# `src/evaluation/build_eval_log.py`, which adds `resource` (TEXT, the
# employee linked to the event) to every event table and `amount` (REAL,
# the order's price) to `event_PayOrder`.
# ---------------------------------------------------------------------------


def _first_event_with(conn: sqlite3.Connection, table: str, column: str, offset: int = 0) -> str | None:
    row = conn.execute(
        f'SELECT ocel_id FROM "{table}" WHERE "{column}" IS NOT NULL '
        f"ORDER BY ocel_id LIMIT 1 OFFSET ?",
        (offset,),
    ).fetchone()
    return row[0] if row else None


# missing_event_attribute_value (Easy/Hard)


def inject_missing_event_attribute_value_null_resource_easy(conn: sqlite3.Connection) -> dict | None:
    """missing_event_attribute_value Easy: NULL the `resource` of a pick item
    event. Easy because the employee is still linked via E2O."""
    table = get_om_event_table("event_PickItem")
    ocel_id = _first_event_with(conn, table, "resource")
    if ocel_id is None:
        return None
    old_value = _capture_and_update(conn, table, "resource", ocel_id, None)
    return {"affected_ids": [ocel_id], "original_values": {ocel_id: old_value}}


def inject_missing_event_attribute_value_null_amount_hard(conn: sqlite3.Connection) -> dict | None:
    """missing_event_attribute_value Hard: NULL the `amount` of a pay order
    event. Hard because the value must be recovered from the linked
    order's price, not from the event itself."""
    table = get_om_event_table("event_PayOrder")
    ocel_id = _first_event_with(conn, table, "amount")
    if ocel_id is None:
        return None
    old_value = _capture_and_update(conn, table, "amount", ocel_id, None)
    return {"affected_ids": [ocel_id], "original_values": {ocel_id: old_value}}


# incorrect_event_attribute_datatype (Easy/Hard)


def inject_incorrect_event_attribute_datatype_string_in_amount_easy(conn: sqlite3.Connection) -> dict | None:
    """incorrect_event_attribute_datatype Easy: Put 'unknown' into a pay
    order's `amount` (REAL)."""
    table = get_om_event_table("event_PayOrder")
    ocel_id = _first_event_with(conn, table, "amount", offset=1)
    if ocel_id is None:
        return None
    old_value = _capture_and_update(conn, table, "amount", ocel_id, "unknown")
    return {"affected_ids": [ocel_id], "original_values": {ocel_id: old_value}}


def inject_incorrect_event_attribute_datatype_blob_in_resource_hard(conn: sqlite3.Connection) -> dict | None:
    """incorrect_event_attribute_datatype Hard: Store a pick item's
    `resource` as UTF-16-LE bytes (BLOB in a TEXT column). Hard because
    the decoded text is the right employee name."""
    table = get_om_event_table("event_PickItem")
    ocel_id = _first_event_with(conn, table, "resource", offset=1)
    if ocel_id is None:
        return None
    current = conn.execute(
        f'SELECT resource FROM "{table}" WHERE ocel_id = ?', (ocel_id,)
    ).fetchone()[0]
    old_value = _capture_and_update(
        conn, table, "resource", ocel_id, str(current).encode("utf-16-le")
    )
    return {"affected_ids": [ocel_id], "original_values": {ocel_id: old_value}}


# incorrect_event_attribute_value (LLM detection, Easy/Hard)


def inject_incorrect_event_attribute_value_negative_amount_easy(conn: sqlite3.Connection) -> dict | None:
    """incorrect_event_attribute_value Easy: Set a pay order's `amount` to
    -999 (obviously wrong)."""
    table = get_om_event_table("event_PayOrder")
    ocel_id = _first_event_with(conn, table, "amount", offset=2)
    if ocel_id is None:
        return None
    old_value = _capture_and_update(conn, table, "amount", ocel_id, -999.0)
    return {"affected_ids": [ocel_id], "original_values": {ocel_id: old_value}}


def inject_incorrect_event_attribute_value_wrong_resource_hard(conn: sqlite3.Connection) -> dict | None:
    """incorrect_event_attribute_value Hard: Set a pick item's `resource` to
    a real employee who is not linked to the event. Hard because the value
    is a valid employee name; only the E2O `employee` relation shows it is
    the wrong one."""
    table = get_om_event_table("event_PickItem")
    ocel_id = _first_event_with(conn, table, "resource", offset=2)
    if ocel_id is None:
        return None
    current = conn.execute(
        f'SELECT resource FROM "{table}" WHERE ocel_id = ?', (ocel_id,)
    ).fetchone()[0]
    other = conn.execute(
        "SELECT ocel_id FROM object WHERE ocel_type = ? AND ocel_id != ? "
        "ORDER BY ocel_id LIMIT 1",
        (get_om_object_type("employees"), current),
    ).fetchone()
    if other is None:
        return None
    old_value = _capture_and_update(conn, table, "resource", ocel_id, other[0])
    return {"affected_ids": [ocel_id], "original_values": {ocel_id: old_value}}


# ---------------------------------------------------------------------------
# incorrect_event_type (Easy/Hard)
# ---------------------------------------------------------------------------


def _retype_event(conn: sqlite3.Connection, ocel_id: str, new_type: str) -> str:
    """Give an event a wrong type the way a real log would hold it: change
    `event.ocel_type` AND move its attribute row into the new type's table
    (shared columns are copied; the rest are left NULL). Without the move,
    the event would sit in its old type's table, a giveaway no real wrong
    label has. Returns the old type."""
    old_type = conn.execute("SELECT ocel_type FROM event WHERE ocel_id = ?", (ocel_id,)).fetchone()[0]
    tables = dict(conn.execute("SELECT ocel_type, ocel_type_map FROM event_map_type"))
    old_t, new_t = f"event_{tables[old_type]}", f"event_{tables[new_type]}"
    old_cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{old_t}")')]
    shared = [c for c in (r[1] for r in conn.execute(f'PRAGMA table_info("{new_t}")')) if c in old_cols]
    cols = ", ".join(f'"{c}"' for c in shared)
    conn.execute(f'INSERT INTO "{new_t}" ({cols}) SELECT {cols} FROM "{old_t}" WHERE ocel_id = ?', (ocel_id,))
    conn.execute(f'DELETE FROM "{old_t}" WHERE ocel_id = ?', (ocel_id,))
    conn.execute("UPDATE event SET ocel_type = ? WHERE ocel_id = ?", (new_type, ocel_id))
    return old_type


def inject_incorrect_event_type_swap_easy(conn: sqlite3.Connection) -> dict | None:
    """incorrect_event_type Easy: Change a place order event to pick item (completely wrong)."""
    place_type = get_om_event_type("place order")
    pick_type = get_om_event_type("pick item")
    row = conn.execute(
        "SELECT ocel_id FROM event WHERE ocel_type = ? ORDER BY ocel_id LIMIT 1", (place_type,)
    ).fetchone()
    if row is None:
        return None
    ocel_id = row[0]
    old_value = _retype_event(conn, ocel_id, pick_type)
    return {"affected_ids": [ocel_id], "original_values": {ocel_id: old_value}}


def inject_incorrect_event_type_swap_related_hard(conn: sqlite3.Connection) -> dict | None:
    """incorrect_event_type Hard: Change a confirm order event to pay order.

    Hard because both events touch the same order, items and products;
    only the `customer` / `sales person` relations and the id prefix
    (`confirm_…`) show the type is wrong."""
    confirm_type = get_om_event_type("confirm order")
    pay_type = get_om_event_type("pay order")
    row = conn.execute(
        "SELECT ocel_id FROM event WHERE ocel_type = ? ORDER BY ocel_id LIMIT 1", (confirm_type,)
    ).fetchone()
    if row is None:
        return None
    ocel_id = row[0]
    old_value = _retype_event(conn, ocel_id, pay_type)
    return {"affected_ids": [ocel_id], "original_values": {ocel_id: old_value}}


# ---------------------------------------------------------------------------
# incorrect_event_time (Easy/Hard)
# ---------------------------------------------------------------------------


def inject_incorrect_event_time_future_easy(conn: sqlite3.Connection) -> dict | None:
    """incorrect_event_time Easy: Set event time to year 2099."""
    table = get_om_event_table("event_PlaceOrder")
    row = conn.execute(
        f"SELECT ocel_id FROM {table} ORDER BY ocel_id LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    ocel_id = row[0]
    old_value = _capture_and_update(
        conn, table, "ocel_time", ocel_id, "2099-01-01 00:00:00"
    )
    return {"affected_ids": [ocel_id], "original_values": {ocel_id: old_value}}


def inject_incorrect_event_time_past_hard(conn: sqlite3.Connection) -> dict | None:
    """incorrect_event_time Hard: Set event time to year 1900."""
    table = get_om_event_table("event_ConfirmOrder")
    row = conn.execute(
        f"SELECT ocel_id FROM {table} ORDER BY ocel_id LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    ocel_id = row[0]
    old_value = _capture_and_update(
        conn, table, "ocel_time", ocel_id, "1900-01-01 00:00:00"
    )
    return {"affected_ids": [ocel_id], "original_values": {ocel_id: old_value}}


# ---------------------------------------------------------------------------
# duplicate_events_on_ids (Easy/Hard)
# ---------------------------------------------------------------------------


def inject_duplicate_events_on_ids_easy(conn: sqlite3.Connection) -> str | None:
    """duplicate_events_on_ids Easy: Duplicate an event row identically."""
    event_type = get_om_event_type("place order")
    row = conn.execute(
        "SELECT ocel_id, ocel_type FROM event WHERE ocel_type = ? ORDER BY ocel_id LIMIT 1", (event_type,)
    ).fetchone()
    if row is None:
        return None
    conn.execute("INSERT INTO event VALUES (?, ?)", row)
    return row[0]


def inject_duplicate_events_on_ids_conflicting_types_hard(conn: sqlite3.Connection) -> str | None:
    """duplicate_events_on_ids Hard: Duplicate event with conflicting type."""
    place_type = get_om_event_type("place order")
    pick_type = get_om_event_type("pick item")
    row = conn.execute(
        "SELECT ocel_id FROM event WHERE ocel_type = ? ORDER BY ocel_id LIMIT 1 OFFSET 2", (place_type,)
    ).fetchone()
    if row is None:
        return None
    # Insert duplicate with different type
    conn.execute("INSERT INTO event VALUES (?, ?)", (row[0], pick_type))
    return row[0]


# ---------------------------------------------------------------------------
# duplicate_events_on_attributes (Easy/Hard)
# ---------------------------------------------------------------------------


def _clone_event_row(conn: sqlite3.Connection, table: str, event_type: str, offset: int) -> tuple[str, str] | None:
    """Copy one row of `table` (all columns) under a fabricated id and
    register it in `event`. Returns (original_id, clone_id)."""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info('{table}')")]
    row = conn.execute(
        f'SELECT * FROM "{table}" ORDER BY ocel_id LIMIT 1 OFFSET ?', (offset,)
    ).fetchone()
    if row is None:
        return None
    orig_id = row[0]
    clone_id = f"{orig_id}-CLONE"
    conn.execute("INSERT INTO event VALUES (?, ?)", (clone_id, event_type))
    conn.execute(
        f'INSERT INTO "{table}" ({", ".join(cols)}) VALUES ({", ".join("?" * len(cols))})',
        (clone_id, *row[1:]),
    )
    return orig_id, clone_id


def inject_duplicate_events_on_attributes_clone_easy(conn: sqlite3.Connection) -> str | None:
    """duplicate_events_on_attributes Easy: Clone a place order event (same
    time and attributes) under a fabricated id, without relations."""
    ids = _clone_event_row(
        conn, get_om_event_table("event_PlaceOrder"), get_om_event_type("place order"), 0
    )
    return ids[1] if ids else None


def inject_duplicate_events_on_attributes_clone_with_refs_hard(conn: sqlite3.Connection) -> str | None:
    """duplicate_events_on_attributes Hard: Clone a place order event AND
    its event_object refs, so the clone looks like a fully connected event."""
    ids = _clone_event_row(
        conn, get_om_event_table("event_PlaceOrder"), get_om_event_type("place order"), 1
    )
    if ids is None:
        return None
    orig_id, clone_id = ids
    conn.execute(
        "INSERT INTO event_object (ocel_event_id, ocel_object_id, ocel_qualifier) "
        "SELECT ?, ocel_object_id, ocel_qualifier FROM event_object WHERE ocel_event_id = ?",
        (clone_id, orig_id),
    )
    return clone_id


# ---------------------------------------------------------------------------
# missing_event_attribute (LLM detection, Easy/Hard)
#
# Schema-level: a whole attribute column is dropped from an event type
# table. Needs the evaluation log (see above).
# ---------------------------------------------------------------------------


def inject_missing_event_attribute_drop_amount_easy(conn: sqlite3.Connection) -> dict:
    """missing_event_attribute Easy: Drop `amount` from pay order events.
    Easy because a payment without an amount is an obvious gap."""
    table = get_om_event_table("event_PayOrder")
    return {"table": table, "column": "amount",
            "original_values": _drop_column(conn, table, "amount")}


def inject_missing_event_attribute_drop_resource_hard(conn: sqlite3.Connection) -> dict:
    """missing_event_attribute Hard: Drop `resource` from pick item events.
    Hard because the employee is still visible via E2O, so the gap is
    only apparent from the other event types that keep `resource`."""
    table = get_om_event_table("event_PickItem")
    return {"table": table, "column": "resource",
            "original_values": _drop_column(conn, table, "resource")}
