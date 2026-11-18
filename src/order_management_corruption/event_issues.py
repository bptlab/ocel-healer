"""Event-side corruption injectors.

Covers issues rooted in the `event` table and its per-type sub-tables:
deleted events whose E2O rows survive (`missing_event`), `event.ocel_type`
values that are NULL / empty / whitespace (`missing_event_type`), and
NULL / empty `ocel_time` values on `event_<Type>` rows
(`missing_event_timestamp`).

Each `inject_<issue_key>_<flavor>(conn, picker)` corrupts `picker.n`
seeded, non-overlapping targets and returns one `Injection` per target
(never an empty list — the picker raises instead). `EVENT_INJECTORS`
maps ``(issue_key, tier)`` to the injector.

Time repairs are scored against a window bracketed by the other events
on the event's anchor object (the order, item or package it is about):
`lo` is the latest earlier event, `hi` the earliest later one, either may
be None. Targets are only drawn from events with at least one such
neighbour, so every window is bounded on at least one side.
"""

from __future__ import annotations

import sqlite3
from typing import Callable

from src.order_management_corruption._common import (
    Injection,
    TargetPicker,
    _capture_and_update,
    _delete_event_fully,
    _touch_low_fanout,
    _type_table,
)

# Cap on E2O rows per deleted event, so a missing_event cascade stays a
# handful of rows (place order reaches 27, failed delivery 36).
MAX_EVENT_FANOUT = 12


def _event_time(conn: sqlite3.Connection, event_id: str) -> str | None:
    row = conn.execute("SELECT ocel_type FROM event WHERE ocel_id = ?", (event_id,)).fetchone()
    table = _type_table(conn, "event", row[0]) if row else None
    if table is None:
        return None
    row = conn.execute(f'SELECT ocel_time FROM "{table}" WHERE ocel_id = ?', (event_id,)).fetchone()
    return row[0] if row else None


def _anchor(conn: sqlite3.Connection, event_id: str, qualifier: str) -> str:
    return conn.execute(
        "SELECT ocel_object_id FROM event_object WHERE ocel_event_id = ? AND ocel_qualifier = ?",
        (event_id, qualifier),
    ).fetchone()[0]


def _time_window(conn: sqlite3.Connection, event_id: str, anchor: str, time: str) -> dict:
    """Repair rule for an event time: within the anchor object's neighbours."""
    times = [
        t for (eid,) in conn.execute(
            "SELECT DISTINCT ocel_event_id FROM event_object "
            "WHERE ocel_object_id = ? AND ocel_event_id != ?",
            (anchor, event_id),
        )
        if isinstance(t := _event_time(conn, eid), str) and t.strip()
    ]
    return {
        "mode": "time_window", "value": time, "anchor": anchor,
        "lo": max((t for t in times if t <= time), default=None),
        "hi": min((t for t in times if t >= time), default=None),
    }


def _pick_with_neighbour(
    picker: TargetPicker, ocel_type: str, qualifier: str, *, max_fanout: int | None = None,
) -> list[str]:
    """Events of `ocel_type` whose anchor (linked via `qualifier`) has at
    least one other event, optionally with at most `max_fanout` E2O rows."""
    sql = """
        SELECT e.ocel_id FROM event e
        JOIN event_object a ON a.ocel_event_id = e.ocel_id AND a.ocel_qualifier = ?
        WHERE e.ocel_type = ?
          AND EXISTS (SELECT 1 FROM event_object b
                      WHERE b.ocel_object_id = a.ocel_object_id
                        AND b.ocel_event_id != e.ocel_id)
    """
    params: tuple = (qualifier, ocel_type)
    if max_fanout is not None:
        sql += " AND (SELECT COUNT(*) FROM event_object c WHERE c.ocel_event_id = e.ocel_id) <= ?"
        params += (max_fanout,)
    ids = picker.pick(sql, params)
    picker.touch(*(_anchor(picker.conn, eid, qualifier) for eid in ids))
    return ids


# ---------------------------------------------------------------------------
# missing_event_timestamp (Easy/Medium/Hard)
# ---------------------------------------------------------------------------


def _missing_event_timestamp(
    conn: sqlite3.Connection, picker: TargetPicker, *,
    tier: str, flavor: str, ocel_type: str, qualifier: str, set_to: str | None,
) -> list[Injection]:
    table = _type_table(conn, "event", ocel_type)
    # Windows first: two targets may share an anchor.
    accepts = {
        eid: _time_window(conn, eid, _anchor(conn, eid, qualifier), _event_time(conn, eid))
        for eid in _pick_with_neighbour(picker, ocel_type, qualifier)
    }
    out = []
    for eid, accept in accepts.items():
        old = _capture_and_update(conn, table, "ocel_time", eid, set_to)
        out.append(Injection(
            "missing_event_timestamp", tier, flavor, "event_time", eid,
            original={"event_type": ocel_type, "ocel_time": old},
            corrupted=set_to,
            expected_rows=[{"issue": "missing_event_timestamp", "ocel_id": eid}],
            accept=accept,
        ))
    return out


def inject_missing_event_timestamp_null_place_order_easy(conn, picker):
    """missing_event_timestamp Easy: NULL a place order's time. It opens the
    order's lifecycle, so the order's later events give an upper bound."""
    return _missing_event_timestamp(conn, picker, tier="easy", flavor="null_place_order",
                                    ocel_type="place order", qualifier="order", set_to=None)


def inject_missing_event_timestamp_empty_pick_item_medium(conn, picker):
    """missing_event_timestamp Medium: '' as a pick item's time. Empty string
    slips past a naive IS NULL check; the item's events bracket it."""
    return _missing_event_timestamp(conn, picker, tier="medium", flavor="empty_pick_item",
                                    ocel_type="pick item", qualifier="item", set_to="")


def inject_missing_event_timestamp_null_item_out_of_stock_hard(conn, picker):
    """missing_event_timestamp Hard: NULL an item out of stock's time. A rare,
    off-happy-path event bracketed only by its item's own history."""
    return _missing_event_timestamp(conn, picker, tier="hard", flavor="null_item_out_of_stock",
                                    ocel_type="item out of stock", qualifier="item", set_to=None)


# ---------------------------------------------------------------------------
# missing_event (Easy/Medium/Hard) — the event and its event_<Type> row are
# deleted; its E2O rows survive and trip both `missing_event` and
# `dangling_e2o_relationship` (missing_side = event).
# ---------------------------------------------------------------------------


def _missing_event(
    conn: sqlite3.Connection, picker: TargetPicker, *,
    tier: str, flavor: str, ocel_type: str, qualifier: str,
) -> list[Injection]:
    # Windows first: two targets may share an anchor.
    accepts = {
        eid: {**_time_window(conn, eid, _anchor(conn, eid, qualifier), _event_time(conn, eid)),
              "ocel_type": ocel_type}
        for eid in _pick_with_neighbour(picker, ocel_type, qualifier, max_fanout=MAX_EVENT_FANOUT)
    }
    out = []
    for eid, accept in accepts.items():
        removed = _delete_event_fully(conn, eid)
        _touch_low_fanout(picker, [r["ocel_object_id"] for r in removed["e2o"]])
        expected = []
        for r in removed["e2o"]:
            rel = {"ocel_event_id": eid, "ocel_object_id": r["ocel_object_id"],
                   "ocel_qualifier": r["ocel_qualifier"]}
            expected.append({"issue": "missing_event", **rel})
            expected.append({"issue": "dangling_e2o_relationship", **rel, "missing_side": "event"})
        out.append(Injection(
            "missing_event", tier, flavor, "event", eid,
            original=removed, corrupted=None,
            expected_rows=expected, accept=accept,
        ))
    return out


def inject_missing_event_place_order_easy(conn, picker):
    """missing_event Easy: delete a place order. Its id and the surviving
    customer/order links make type and (first-in-lifecycle) time obvious."""
    return _missing_event(conn, picker, tier="easy", flavor="place_order",
                          ocel_type="place order", qualifier="order")


def inject_missing_event_pick_item_medium(conn, picker):
    """missing_event Medium: delete a pick item. Mid-lifecycle, so the time
    must be interpolated between the item's neighbouring events."""
    return _missing_event(conn, picker, tier="medium", flavor="pick_item",
                          ocel_type="pick item", qualifier="item")


def inject_missing_event_failed_delivery_hard(conn, picker):
    """missing_event Hard: delete a failed delivery. The rarest event type;
    its `shipped package` link is shared with send package and package
    delivered, so the type must be told apart from the package's history."""
    return _missing_event(conn, picker, tier="hard", flavor="failed_delivery",
                          ocel_type="failed delivery", qualifier="shipped package")


# ---------------------------------------------------------------------------
# missing_event_type (Easy/Medium/Hard)
# ---------------------------------------------------------------------------


def _missing_event_type(
    conn: sqlite3.Connection, picker: TargetPicker, *,
    tier: str, flavor: str, ocel_type: str, set_to: str | None,
) -> list[Injection]:
    out = []
    for eid in picker.pick("SELECT ocel_id FROM event WHERE ocel_type = ?", (ocel_type,)):
        old = _capture_and_update(conn, "event", "ocel_type", eid, set_to)
        out.append(Injection(
            "missing_event_type", tier, flavor, "event", eid,
            original=old, corrupted=set_to,
            expected_rows=[{"issue": "missing_event_type", "ocel_id": eid}],
            accept={"mode": "exact", "value": old},
        ))
    return out


def inject_missing_event_type_null_confirm_easy(conn, picker):
    """missing_event_type Easy: NULL a confirm order's type; the id and the
    `sales person` link point straight at it."""
    return _missing_event_type(conn, picker, tier="easy", flavor="null_confirm",
                               ocel_type="confirm order", set_to=None)


def inject_missing_event_type_empty_pay_medium(conn, picker):
    """missing_event_type Medium: '' as a pay order's type; slips past a
    naive IS NULL check."""
    return _missing_event_type(conn, picker, tier="medium", flavor="empty_pay",
                               ocel_type="pay order", set_to="")


def inject_missing_event_type_whitespace_package_hard(conn, picker):
    """missing_event_type Hard: whitespace as a create package's type; needs
    TRIM to detect, and must be told apart from the other package events."""
    return _missing_event_type(conn, picker, tier="hard", flavor="whitespace_package",
                               ocel_type="create package", set_to="   ")


EVENT_INJECTORS: dict[tuple[str, str], Callable[[sqlite3.Connection, TargetPicker], list[Injection]]] = {
    ("missing_event_timestamp", "easy"):   inject_missing_event_timestamp_null_place_order_easy,
    ("missing_event_timestamp", "medium"): inject_missing_event_timestamp_empty_pick_item_medium,
    ("missing_event_timestamp", "hard"):   inject_missing_event_timestamp_null_item_out_of_stock_hard,
    ("missing_event", "easy"):             inject_missing_event_place_order_easy,
    ("missing_event", "medium"):           inject_missing_event_pick_item_medium,
    ("missing_event", "hard"):             inject_missing_event_failed_delivery_hard,
    ("missing_event_type", "easy"):        inject_missing_event_type_null_confirm_easy,
    ("missing_event_type", "medium"):      inject_missing_event_type_empty_pay_medium,
    ("missing_event_type", "hard"):        inject_missing_event_type_whitespace_package_hard,
}
