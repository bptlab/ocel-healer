"""Event-side corruption injectors.

Covers issues rooted in the `event` table and its per-type sub-tables:
deleted events whose E2O rows survive (`missing_event`), missing or wrong
types (`missing_event_type`, `incorrect_event_type`), missing or
implausible times (`missing_event_timestamp`, `incorrect_event_time`),
duplicated events (`duplicate_events_on_ids`,
`duplicate_events_on_attributes`), and missing, mistyped or implausible
attribute values (`missing_event_attribute_value`,
`incorrect_event_attribute_datatype`, `incorrect_event_attribute_value`).

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

import re
import sqlite3
from datetime import datetime, timedelta
from typing import Callable

from src.order_management_corruption._common import (
    Injection,
    TargetPicker,
    _accept_value,
    _capture_and_update,
    _delete_event_fully,
    _remove_event_primary_key,
    _rows_as_dicts,
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


# ---------------------------------------------------------------------------
# incorrect_event_type (Easy/Medium/Hard) — only `event.ocel_type` changes;
# the attribute row stays in the original type's table, so restoring the
# type makes the event consistent again.
# ---------------------------------------------------------------------------


def _incorrect_event_type(
    conn: sqlite3.Connection, picker: TargetPicker, *,
    tier: str, flavor: str, from_type: str, to_type: str,
) -> list[Injection]:
    out = []
    for eid in picker.pick("SELECT ocel_id FROM event WHERE ocel_type = ?", (from_type,)):
        _capture_and_update(conn, "event", "ocel_type", eid, to_type)
        out.append(Injection(
            "incorrect_event_type", tier, flavor, "event", eid,
            original=from_type, corrupted=to_type,
            expected_rows=[{"issue": "incorrect_event_type", "ocel_id": eid, "ocel_type": to_type}],
            accept={"mode": "exact", "value": from_type},
        ))
    return out


def inject_incorrect_event_type_place_to_pick_easy(conn, picker):
    """incorrect_event_type Easy: a place order typed `pick item`; its id,
    customer and order links all contradict that."""
    return _incorrect_event_type(conn, picker, tier="easy", flavor="place_to_pick",
                                 from_type="place order", to_type="pick item")


def inject_incorrect_event_type_pay_to_confirm_medium(conn, picker):
    """incorrect_event_type Medium: a pay order typed `confirm order`; both
    are order events, but confirm order carries a `sales person` link."""
    return _incorrect_event_type(conn, picker, tier="medium", flavor="pay_to_confirm",
                                 from_type="pay order", to_type="confirm order")


def inject_incorrect_event_type_create_to_send_hard(conn, picker):
    """incorrect_event_type Hard: a create package typed `send package`;
    both touch the same package, items and an employee."""
    return _incorrect_event_type(conn, picker, tier="hard", flavor="create_to_send",
                                 from_type="create package", to_type="send package")


# ---------------------------------------------------------------------------
# incorrect_event_time (Easy/Medium/Hard) — a well-formed but implausible
# `ocel_time`. Scored against the same window as a missing timestamp.
# ---------------------------------------------------------------------------

_TIME_FMT = "%Y-%m-%d %H:%M:%S"


def _incorrect_event_time(
    conn: sqlite3.Connection, picker: TargetPicker, *,
    tier: str, flavor: str, ocel_type: str, qualifier: str,
    shift: Callable[[str, str | None], str],
) -> list[Injection]:
    """`shift(time, lo)` returns the corrupted time, given the original and
    the latest earlier neighbour on the anchor object."""
    table = _type_table(conn, "event", ocel_type)
    accepts = {
        eid: _time_window(conn, eid, _anchor(conn, eid, qualifier), _event_time(conn, eid))
        for eid in _pick_with_neighbour(picker, ocel_type, qualifier)
    }
    out = []
    for eid, accept in accepts.items():
        new = shift(accept["value"], accept["lo"])
        old = _capture_and_update(conn, table, "ocel_time", eid, new)
        out.append(Injection(
            "incorrect_event_time", tier, flavor, "event_time", eid,
            original={"event_type": ocel_type, "ocel_time": old}, corrupted=new,
            expected_rows=[{"issue": "incorrect_event_time", "ocel_id": eid}],
            accept=accept,
        ))
    return out


def _before(lo: str | None, rng, lo_h: float, hi_h: float) -> str:
    if lo is None:
        raise ValueError("No earlier neighbour to move the event before")
    delta = timedelta(seconds=rng.randint(int(lo_h * 3600), int(hi_h * 3600)))
    return (datetime.strptime(lo, _TIME_FMT) - delta).strftime(_TIME_FMT)


def inject_incorrect_event_time_future_year_easy(conn, picker):
    """incorrect_event_time Easy: a pay order's year set to 2099."""
    return _incorrect_event_time(conn, picker, tier="easy", flavor="future_year",
                                 ocel_type="pay order", qualifier="order",
                                 shift=lambda t, lo: "2099" + t[4:])


def inject_incorrect_event_time_confirm_before_place_medium(conn, picker):
    """incorrect_event_time Medium: a confirm order moved 1–5 days before
    the order's place order."""
    return _incorrect_event_time(conn, picker, tier="medium", flavor="confirm_before_place",
                                 ocel_type="confirm order", qualifier="order",
                                 shift=lambda t, lo: _before(lo, picker.rng, 24, 120))


def inject_incorrect_event_time_send_before_create_hard(conn, picker):
    """incorrect_event_time Hard: a send package moved 1–6 hours before the
    package's preceding event; a small shift that still breaks the order."""
    return _incorrect_event_time(conn, picker, tier="hard", flavor="send_before_create",
                                 ocel_type="send package", qualifier="shipped package",
                                 shift=lambda t, lo: _before(lo, picker.rng, 1, 6))


# ---------------------------------------------------------------------------
# duplicate_events_on_ids (Easy/Medium/Hard) — a second `event` row with an
# existing id.
# ---------------------------------------------------------------------------


def _duplicate_events_on_ids(
    conn: sqlite3.Connection, picker: TargetPicker, *,
    tier: str, flavor: str, ocel_type: str, dup_type: str, with_sub_row: bool = False,
) -> list[Injection]:
    _remove_event_primary_key(conn)
    out = []
    for eid in picker.pick("SELECT ocel_id FROM event WHERE ocel_type = ?", (ocel_type,)):
        conn.execute("INSERT INTO event VALUES (?, ?)", (eid, dup_type))
        sub = None
        if with_sub_row:
            # Give the copy its own attribute row in the duplicate type's table.
            src, dst = _type_table(conn, "event", ocel_type), _type_table(conn, "event", dup_type)
            (sub,) = _rows_as_dicts(conn.execute(f'SELECT * FROM "{src}" WHERE ocel_id = ?', (eid,)))
            cols = [c for c in sub if c in {r[1] for r in conn.execute(f'PRAGMA table_info("{dst}")')}]
            conn.execute(
                f'INSERT INTO "{dst}" ({", ".join(cols)}) VALUES ({", ".join("?" * len(cols))})',
                [sub[c] for c in cols],
            )
        out.append(Injection(
            "duplicate_events_on_ids", tier, flavor, "event", eid,
            original=ocel_type, corrupted={"ocel_type": dup_type, "sub_row": sub},
            expected_rows=[{"issue": "duplicate_events_on_ids", "ocel_id": eid}],
            accept={"mode": "delete_any_of", "keep": 1, "ocel_id": eid, "ocel_type": ocel_type},
        ))
    return out


def inject_duplicate_events_on_ids_identical_easy(conn, picker):
    """duplicate_events_on_ids Easy: a pay order's `event` row recorded twice."""
    return _duplicate_events_on_ids(conn, picker, tier="easy", flavor="identical",
                                    ocel_type="pay order", dup_type="pay order")


def inject_duplicate_events_on_ids_conflicting_type_medium(conn, picker):
    """duplicate_events_on_ids Medium: a confirm order's id recorded again as
    `pay order`; the copy to delete is the one with the wrong type."""
    return _duplicate_events_on_ids(conn, picker, tier="medium", flavor="conflicting_type",
                                    ocel_type="confirm order", dup_type="pay order")


def inject_duplicate_events_on_ids_sibling_type_hard(conn, picker):
    """duplicate_events_on_ids Hard: a create package's id recorded again as
    `send package`, with its own send package attribute row, so both
    copies look complete."""
    return _duplicate_events_on_ids(conn, picker, tier="hard", flavor="sibling_type",
                                    ocel_type="create package", dup_type="send package",
                                    with_sub_row=True)


# ---------------------------------------------------------------------------
# duplicate_events_on_attributes (Easy/Medium/Hard) — a clone under a new
# id in the log's own pattern, same time and attributes.
# ---------------------------------------------------------------------------


def _clone_id(picker: TargetPicker, event_id: str) -> str:
    """A new id in `event_id`'s pattern: the next free number for plain ids
    (`pay_o-990001` → `pay_o-992001`); for ids with a log-wide counter
    suffix, the same object with the next counter value
    (`fail_p-660006_1` → `fail_p-660006_312`)."""
    m = re.match(r"^((\w+-)\d+_)(\d+)$", event_id)
    if m is None:
        return picker.new_id(re.match(r"^(.*?)(\d+)$", event_id).group(1))
    counters = [
        int(c.rsplit("_", 1)[1])
        for (c,) in picker.conn.execute(
            "SELECT ocel_id FROM event WHERE substr(ocel_id, 1, ?) = ?",
            (len(m.group(2)), m.group(2)),
        )
        if re.match(r"^\w+-\d+_\d+$", c)
    ]
    return f"{m.group(1)}{max(counters) + 1}"


def _duplicate_events_on_attributes(
    conn: sqlite3.Connection, picker: TargetPicker, *,
    tier: str, flavor: str, ocel_type: str, with_e2o: bool,
) -> list[Injection]:
    table = _type_table(conn, "event", ocel_type)
    out = []
    for eid in picker.pick("SELECT ocel_id FROM event WHERE ocel_type = ?", (ocel_type,)):
        clone = _clone_id(picker, eid)
        picker.touch(clone)
        (sub,) = _rows_as_dicts(conn.execute(f'SELECT * FROM "{table}" WHERE ocel_id = ?', (eid,)))
        conn.execute("INSERT INTO event VALUES (?, ?)", (clone, ocel_type))
        conn.execute(
            f'INSERT INTO "{table}" ({", ".join(sub)}) VALUES ({", ".join("?" * len(sub))})',
            [clone, *list(sub.values())[1:]],
        )
        if with_e2o:
            conn.execute(
                "INSERT INTO event_object SELECT ?, ocel_object_id, ocel_qualifier "
                "FROM event_object WHERE ocel_event_id = ?",
                (clone, eid),
            )
        out.append(Injection(
            "duplicate_events_on_attributes", tier, flavor, "event", [eid, clone],
            original=eid, corrupted=clone,
            expected_rows=[{"issue": "duplicate_events_on_attributes", "ocel_ids": [eid, clone]}],
            accept={"mode": "delete_any_of", "keep": 1, "ocel_ids": [eid, clone]},
        ))
    return out


def inject_duplicate_events_on_attributes_clone_easy(conn, picker):
    """duplicate_events_on_attributes Easy: a confirm order cloned without
    links; the clone touches no objects at all."""
    return _duplicate_events_on_attributes(conn, picker, tier="easy", flavor="clone",
                                           ocel_type="confirm order", with_e2o=False)


def inject_duplicate_events_on_attributes_clone_with_refs_medium(conn, picker):
    """duplicate_events_on_attributes Medium: a place order cloned with its
    links, so both copies look equally real."""
    return _duplicate_events_on_attributes(conn, picker, tier="medium", flavor="clone_with_refs",
                                           ocel_type="place order", with_e2o=True)


def inject_duplicate_events_on_attributes_failed_delivery_hard(conn, picker):
    """duplicate_events_on_attributes Hard: a failed delivery cloned with
    its links under the next attempt number; packages legitimately fail
    more than once."""
    return _duplicate_events_on_attributes(conn, picker, tier="hard", flavor="failed_delivery",
                                           ocel_type="failed delivery", with_e2o=True)


# ---------------------------------------------------------------------------
# Event attribute values: missing_event_attribute_value,
# incorrect_event_attribute_datatype, incorrect_event_attribute_value
# (Easy/Medium/Hard). `corrupt(old, event_id)` returns the new value.
# ---------------------------------------------------------------------------


def _corrupt_event_attribute(
    conn: sqlite3.Connection, picker: TargetPicker, *,
    issue: str, tier: str, flavor: str, ocel_type: str, column: str,
    corrupt: Callable[[object, str], object],
) -> list[Injection]:
    table = _type_table(conn, "event", ocel_type)
    out = []
    for eid in picker.pick(f'SELECT ocel_id FROM "{table}" WHERE "{column}" IS NOT NULL'):
        old = conn.execute(f'SELECT "{column}" FROM "{table}" WHERE ocel_id = ?', (eid,)).fetchone()[0]
        new = corrupt(old, eid)
        _capture_and_update(conn, table, column, eid, new)
        out.append(Injection(
            issue, tier, flavor, "event_attr", eid,
            original={"event_type": ocel_type, "attribute": column, "value": old},
            corrupted=new,
            expected_rows=[{"issue": issue, "ocel_id": eid, "attribute": column}],
            accept=_accept_value(old),
        ))
    return out


def inject_missing_event_attribute_value_null_resource_easy(conn, picker):
    """missing_event_attribute_value Easy: NULL a send package's resource."""
    return _corrupt_event_attribute(conn, picker, issue="missing_event_attribute_value", tier="easy",
                                    flavor="null_resource", ocel_type="send package",
                                    column="resource", corrupt=lambda v, e: None)


def inject_missing_event_attribute_value_empty_resource_medium(conn, picker):
    """missing_event_attribute_value Medium: '' as a confirm order's resource."""
    return _corrupt_event_attribute(conn, picker, issue="missing_event_attribute_value", tier="medium",
                                    flavor="empty_resource", ocel_type="confirm order",
                                    column="resource", corrupt=lambda v, e: "")


def inject_missing_event_attribute_value_whitespace_resource_hard(conn, picker):
    """missing_event_attribute_value Hard: whitespace as a package
    delivered's resource; needs TRIM to detect."""
    return _corrupt_event_attribute(conn, picker, issue="missing_event_attribute_value", tier="hard",
                                    flavor="whitespace_resource", ocel_type="package delivered",
                                    column="resource", corrupt=lambda v, e: "   ")


def inject_incorrect_event_attribute_datatype_na_item_count_easy(conn, picker):
    """incorrect_event_attribute_datatype Easy: 'n/a' in a place order's
    item_count (INTEGER)."""
    return _corrupt_event_attribute(conn, picker, issue="incorrect_event_attribute_datatype", tier="easy",
                                    flavor="na_item_count", ocel_type="place order",
                                    column="item_count", corrupt=lambda v, e: "n/a")


def inject_incorrect_event_attribute_datatype_unit_item_count_medium(conn, picker):
    """incorrect_event_attribute_datatype Medium: '<n> items' in a place
    order's item_count; the number is recoverable from the text."""
    return _corrupt_event_attribute(conn, picker, issue="incorrect_event_attribute_datatype", tier="medium",
                                    flavor="unit_item_count", ocel_type="place order",
                                    column="item_count", corrupt=lambda v, e: f"{v} items")


def inject_incorrect_event_attribute_datatype_blob_resource_hard(conn, picker):
    """incorrect_event_attribute_datatype Hard: an item out of stock's
    resource stored as UTF-16 bytes (BLOB in a TEXT column)."""
    return _corrupt_event_attribute(conn, picker, issue="incorrect_event_attribute_datatype", tier="hard",
                                    flavor="blob_resource", ocel_type="item out of stock",
                                    column="resource", corrupt=lambda v, e: v.encode("utf-16-le"))


def _linked_items(conn: sqlite3.Connection, event_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM event_object WHERE ocel_event_id = ? AND ocel_qualifier = 'item'",
        (event_id,),
    ).fetchone()[0]


def inject_incorrect_event_attribute_value_negative_item_count_easy(conn, picker):
    """incorrect_event_attribute_value Easy: a negative item_count on a
    place order."""
    return _corrupt_event_attribute(conn, picker, issue="incorrect_event_attribute_value", tier="easy",
                                    flavor="negative_item_count", ocel_type="place order",
                                    column="item_count", corrupt=lambda v, e: -v)


def inject_incorrect_event_attribute_value_item_count_mismatch_medium(conn, picker):
    """incorrect_event_attribute_value Medium: a place order's item_count
    raised by 2–4, contradicting the number of linked items."""
    return _corrupt_event_attribute(conn, picker, issue="incorrect_event_attribute_value", tier="medium",
                                    flavor="item_count_mismatch", ocel_type="place order",
                                    column="item_count",
                                    corrupt=lambda v, e: _linked_items(conn, e) + picker.rng.randint(2, 4))


def inject_incorrect_event_attribute_value_foreign_resource_hard(conn, picker):
    """incorrect_event_attribute_value Hard: a pick item's resource set to
    an employee who never picks items (and isn't the linked employee)."""
    outsiders = [r for (r,) in conn.execute(
        "SELECT ocel_id FROM object WHERE ocel_type = 'employees' AND ocel_id NOT IN "
        "(SELECT resource FROM event_PickItem WHERE resource IS NOT NULL) ORDER BY ocel_id"
    )]
    return _corrupt_event_attribute(conn, picker, issue="incorrect_event_attribute_value", tier="hard",
                                    flavor="foreign_resource", ocel_type="pick item",
                                    column="resource", corrupt=lambda v, e: picker.rng.choice(outsiders))


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
    ("incorrect_event_type", "easy"):      inject_incorrect_event_type_place_to_pick_easy,
    ("incorrect_event_type", "medium"):    inject_incorrect_event_type_pay_to_confirm_medium,
    ("incorrect_event_type", "hard"):      inject_incorrect_event_type_create_to_send_hard,
    ("incorrect_event_time", "easy"):      inject_incorrect_event_time_future_year_easy,
    ("incorrect_event_time", "medium"):    inject_incorrect_event_time_confirm_before_place_medium,
    ("incorrect_event_time", "hard"):      inject_incorrect_event_time_send_before_create_hard,
    ("duplicate_events_on_ids", "easy"):   inject_duplicate_events_on_ids_identical_easy,
    ("duplicate_events_on_ids", "medium"): inject_duplicate_events_on_ids_conflicting_type_medium,
    ("duplicate_events_on_ids", "hard"):   inject_duplicate_events_on_ids_sibling_type_hard,
    ("duplicate_events_on_attributes", "easy"):   inject_duplicate_events_on_attributes_clone_easy,
    ("duplicate_events_on_attributes", "medium"): inject_duplicate_events_on_attributes_clone_with_refs_medium,
    ("duplicate_events_on_attributes", "hard"):   inject_duplicate_events_on_attributes_failed_delivery_hard,
    ("missing_event_attribute_value", "easy"):    inject_missing_event_attribute_value_null_resource_easy,
    ("missing_event_attribute_value", "medium"):  inject_missing_event_attribute_value_empty_resource_medium,
    ("missing_event_attribute_value", "hard"):    inject_missing_event_attribute_value_whitespace_resource_hard,
    ("incorrect_event_attribute_datatype", "easy"):   inject_incorrect_event_attribute_datatype_na_item_count_easy,
    ("incorrect_event_attribute_datatype", "medium"): inject_incorrect_event_attribute_datatype_unit_item_count_medium,
    ("incorrect_event_attribute_datatype", "hard"):   inject_incorrect_event_attribute_datatype_blob_resource_hard,
    ("incorrect_event_attribute_value", "easy"):      inject_incorrect_event_attribute_value_negative_item_count_easy,
    ("incorrect_event_attribute_value", "medium"):    inject_incorrect_event_attribute_value_item_count_mismatch_medium,
    ("incorrect_event_attribute_value", "hard"):      inject_incorrect_event_attribute_value_foreign_resource_hard,
}
