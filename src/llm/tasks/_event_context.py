"""Shared context builder for event-anchored temporal tasks.

`missing_event_timestamp` needs the objects touched by the anchor event and
the timestamps of every OTHER event touching those same objects — a
lifecycle window the LLM can use to bracket the anchor's missing time.

`ocel_time` lives on per-type event sub-tables (`event_<CamelCase>`), so
the sampler joins through `event_map_type` to reach them.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any

from src.detection.error_detection import _event_type_tables


# Sort key that keeps None values last without crashing on string comparison.
def _ts_key(v: Any) -> tuple[int, str]:
    if v is None:
        return (1, "")
    return (0, str(v))


# (db file, mtime) -> event_type_order result: a detection sweep asks once
# per candidate, and the query scans every event.
_ORDER_CACHE: dict[tuple[str, int], list[dict[str, Any]]] = {}


def event_type_order(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The usual order of event types in this log, learned from the data:
    each event's position within its object's lifecycle (0 = first event,
    1 = last), averaged per event type. Every object weighs the same, so a
    hub touched by thousands of events doesn't drown out the case objects.
    Sorted by position; types without a timestamped lifecycle are left out.
    Cached per database file until the file changes."""
    db_file = next((f for _, name, f in conn.execute("PRAGMA database_list") if name == "main"), "")
    key = (db_file, os.stat(db_file).st_mtime_ns) if db_file and os.path.exists(db_file) else None
    if key is not None and key in _ORDER_CACHE:
        return _ORDER_CACHE[key]
    order = _event_type_order(conn)
    if key is not None:
        _ORDER_CACHE[key] = order
    return order


def _event_type_order(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    type_map = dict(_event_type_tables(conn))
    if not type_map:
        return []
    times = " UNION ALL ".join(f'SELECT ocel_id, ocel_time FROM "{t}"' for t in type_map.values())
    rows = conn.execute(
        f"""
        WITH ev AS (
            SELECT e.ocel_type AS t, eo.ocel_object_id AS o, ti.ocel_time AS ts
            FROM event_object eo
            JOIN ({times}) ti ON ti.ocel_id = eo.ocel_event_id
            JOIN event e ON e.ocel_id = eo.ocel_event_id
            WHERE ti.ocel_time IS NOT NULL AND ti.ocel_time != ''
        ), ranked AS (
            SELECT t,
                   ROW_NUMBER() OVER (PARTITION BY o ORDER BY ts) - 1 AS r,
                   COUNT(*) OVER (PARTITION BY o) AS n
            FROM ev
        )
        SELECT t, SUM(r * 1.0 / (n - 1) / n) / SUM(1.0 / n)
        FROM ranked WHERE n > 1 AND t IS NOT NULL AND TRIM(t) != ''
        GROUP BY t ORDER BY 2
        """
    ).fetchall()
    return [{"event_type": t, "position": round(p, 2)} for t, p in rows]


# A linked object with more events than this many times the event's least
# busy object is a hub shared by unrelated cases (a product, a customer).
CASE_OBJECT_FACTOR = 10


def case_objects(conn: sqlite3.Connection, object_ids: list[str]) -> list[str]:
    """The objects that make up the event's own case: those with at most
    `CASE_OBJECT_FACTOR` times as many events as the least busy one. A hub's
    neighboring events belong to other cases, so their order says nothing
    about this event."""
    ph = ", ".join("?" * len(object_ids))
    n = dict(conn.execute(
        f"SELECT ocel_object_id, COUNT(*) FROM event_object "
        f"WHERE ocel_object_id IN ({ph}) GROUP BY ocel_object_id", object_ids,
    ).fetchall())
    least = min((n.get(o, 0) for o in object_ids), default=0) or 1
    return [o for o in object_ids if n.get(o, 0) <= CASE_OBJECT_FACTOR * least]


def neighbor_events_ctx(
    conn: sqlite3.Connection,
    event_id: str,
    *,
    max_neighbors: int = 10,
) -> dict[str, Any]:
    """Gather the temporal context around one event.

    Returns a dict with:
      - related_objects: list of {ocel_object_id, ocel_type, ocel_qualifier}
      - neighbor_events: list of {ocel_id, ocel_type, ocel_time, qualifier,
                                  ocel_object_id} of the anchor's case objects
                        (`case_objects`), sorted by parsed timestamp
                        (nulls last). Excludes the anchor event.
      - expected_format: first non-null neighbor `ocel_time` verbatim (or None).
      - event_type_order: the usual order of event types in this log
                        (see `event_type_order`).

    The neighbor list is capped at `max_neighbors`. When more neighbors are
    available, we prefer a balanced slice around the anchor's own timestamp
    (if known) — half preceding, half following — otherwise the events of
    the anchor's most specific objects (fewest events).
    """
    ctx: dict[str, Any] = {"event_type_order": event_type_order(conn)}

    # 1. Related objects for the anchor event.
    related = conn.execute(
        "SELECT ocel_object_id, ocel_qualifier FROM event_object "
        "WHERE ocel_event_id = ?",
        (event_id,),
    ).fetchall()
    if not related:
        return ctx

    object_ids = [r[0] for r in related]
    qualifier_by_object = {r[0]: r[1] for r in related}
    case_ids = case_objects(conn, object_ids)

    # Object types (for display only).
    placeholders = ", ".join("?" * len(object_ids))
    obj_types = dict(conn.execute(
        f"SELECT ocel_id, ocel_type FROM object WHERE ocel_id IN ({placeholders})",
        object_ids,
    ).fetchall())

    ctx["related_objects"] = [
        {
            "ocel_object_id": oid,
            "ocel_type": obj_types.get(oid),
            "ocel_qualifier": qualifier_by_object.get(oid),
        }
        for oid in object_ids
    ]

    # 2. Neighbor events: the other events of the anchor's case objects
    # (see `case_objects`), not of hubs shared with unrelated cases.
    # ocel_time comes from the per-type tables via event_map_type.
    type_map = dict(_event_type_tables(conn))  # ocel_type -> event_<Type> table

    # Fetch every event touching any related object, plus its type (from
    # `event` table) and qualifier (from event_object).
    neighbor_rows = conn.execute(
        f"""
        SELECT DISTINCT
            e.ocel_id,
            e.ocel_type,
            eo.ocel_qualifier,
            eo.ocel_object_id
        FROM event_object eo
        JOIN event e ON e.ocel_id = eo.ocel_event_id
        WHERE eo.ocel_object_id IN ({", ".join("?" * len(case_ids))})
          AND eo.ocel_event_id != ?
        """,
        (*case_ids, event_id),
    ).fetchall()

    if not neighbor_rows:
        ctx["neighbor_events"] = []
        return ctx

    # Attach ocel_time by joining through the per-type sub-table. Group
    # neighbors by event_type so we hit each sub-table once.
    by_type: dict[str, list[tuple]] = {}
    for row in neighbor_rows:
        by_type.setdefault(row[1], []).append(row)

    with_time: list[dict[str, Any]] = []
    for etype, rows in by_type.items():
        table = type_map.get(etype)
        if not table:
            # Unknown event type — emit without ocel_time.
            for eid, et, qual, oid in rows:
                with_time.append({
                    "ocel_id": eid, "ocel_type": et, "ocel_time": None,
                    "qualifier": qual, "ocel_object_id": oid,
                })
            continue
        ids = [r[0] for r in rows]
        ph = ", ".join("?" * len(ids))
        times = dict(conn.execute(
            f'SELECT ocel_id, ocel_time FROM "{table}" WHERE ocel_id IN ({ph})',
            ids,
        ).fetchall())
        for eid, et, qual, oid in rows:
            with_time.append({
                "ocel_id": eid, "ocel_type": et,
                "ocel_time": times.get(eid),
                "qualifier": qual, "ocel_object_id": oid,
            })

    with_time.sort(key=lambda e: _ts_key(e["ocel_time"]))
    # One entry per event: an event linked to several case objects would
    # otherwise fill the slots with copies of itself.
    with_time = list({e["ocel_id"]: e for e in reversed(with_time)}.values())[::-1]

    # Balanced slice around the anchor's own timestamp when we can find it.
    # Empty-string timestamps count as missing: all real timestamps compare
    # > '', which would collapse the balanced slice to an all-following one.
    anchor_time = None
    for etype, table in type_map.items():
        got = conn.execute(
            f'SELECT ocel_time FROM "{table}" WHERE ocel_id = ? LIMIT 1',
            (event_id,),
        ).fetchone()
        if got and got[0]:  # truthy check rejects both None and ''
            anchor_time = got[0]
            break

    if len(with_time) > max_neighbors:
        if anchor_time is not None:
            preceding = [e for e in with_time if e["ocel_time"] and str(e["ocel_time"]) <= str(anchor_time)]
            following = [e for e in with_time if e["ocel_time"] and str(e["ocel_time"]) > str(anchor_time)]
            half = max_neighbors // 2
            # `preceding[-0:]` would return the whole list (Python's -0 == 0
            # slice quirk), so guard the half=0 case explicitly.
            head = preceding[-half:] if half else []
            slice_ = head + following[:max_neighbors - half]
            # Top up if one side was short.
            if len(slice_) < max_neighbors:
                extras = [e for e in with_time if e not in slice_][: max_neighbors - len(slice_)]
                slice_.extend(extras)
            with_time = slice_
        else:
            # No anchor time: prefer events of the most specific objects
            # (fewest events) over those of busy hubs, whose earliest
            # events belong to unrelated cases.
            n_events = dict(conn.execute(
                f"SELECT ocel_object_id, COUNT(*) FROM event_object "
                f"WHERE ocel_object_id IN ({placeholders}) GROUP BY ocel_object_id",
                object_ids,
            ).fetchall())
            ranked = sorted(with_time, key=lambda e: n_events.get(e["ocel_object_id"], 0))
            with_time = sorted(ranked[:max_neighbors], key=lambda e: _ts_key(e["ocel_time"]))

    ctx["neighbor_events"] = with_time
    # Format hint: first non-null neighbor timestamp, verbatim.
    for e in with_time:
        if e.get("ocel_time"):
            ctx["expected_format"] = e["ocel_time"]
            break
    return ctx
