"""Relation-side corruption injectors.

Covers issues rooted in the relation tables (`event_object`,
`object_object`): dangling E2O and O2O references, plus the
`missing_object` case where an E2O row points at a plausibly-typed but
non-existent object id.
"""

from __future__ import annotations

import sqlite3

from .om_schema import (
    get_om_event_type,
    get_om_e2o_qualifier,
    get_om_o2o_qualifier,
)


# ---------------------------------------------------------------------------
# Typo helpers — every dangling injector corrupts an existing relation in
# place, so the clean log is the exact repair target.
# ---------------------------------------------------------------------------


def _double_letter(s: str) -> str:
    """Repeat the first letter at or after the middle: 'Minerals' → 'Mineraals'."""
    i = next((i for i in range(len(s) // 2, len(s)) if s[i].isalpha()), len(s) - 1)
    return s[: i + 1] + s[i:]


def _swap_letters(s: str) -> str:
    """Swap the first two adjacent, different letters: 'place' → 'plcae'."""
    i = next(
        (i for i in range(1, len(s) - 1)
         if s[i].isalpha() and s[i + 1].isalpha() and s[i] != s[i + 1]),
        None,
    )
    return s if i is None else s[:i] + s[i + 1] + s[i] + s[i + 2:]


def _drop_digit(s: str) -> str:
    """Drop the last digit of a repeated pair: 'o-990006' → 'o-99006', which
    is as close to o-990006 as to o-991006 or o-990906."""
    i = next((i for i in reversed(range(len(s) - 1)) if s[i].isdigit() and s[i] == s[i + 1]), None)
    return s if i is None else s[:i] + s[i + 1:]


def _exists(conn: sqlite3.Connection, table: str, ocel_id: str) -> bool:
    return conn.execute(f"SELECT 1 FROM {table} WHERE ocel_id = ? LIMIT 1", (ocel_id,)).fetchone() is not None


def _retarget(conn: sqlite3.Connection, table: str, match: dict, changes: dict) -> None:
    """Rewrite one relation row (the first matching `match`) with `changes`."""
    where = " AND ".join(f"{c} = ?" for c in match)
    sets = ", ".join(f"{c} = ?" for c in changes)
    conn.execute(
        f"UPDATE {table} SET {sets} WHERE rowid = "
        f"(SELECT rowid FROM {table} WHERE {where} LIMIT 1)",
        (*changes.values(), *match.values()),
    )


# ---------------------------------------------------------------------------
# dangling_e2o_relationship (Easy/Hard)
# ---------------------------------------------------------------------------


def inject_dangling_e2o_relationship_missing_object_easy(conn: sqlite3.Connection) -> dict | None:
    """dangling_e2o_relationship Easy: a place order's `customer` link
    misspells the customer (one doubled letter, e.g. 'Balkan Mineraals
    d.o.o.'). Customer names are distinct, so the intended object is the
    one clear near-miss."""
    row = conn.execute(
        "SELECT ocel_event_id, ocel_object_id FROM event_object "
        "WHERE ocel_qualifier = ? ORDER BY ocel_event_id LIMIT 1 OFFSET 5",
        (get_om_e2o_qualifier("customer"),),
    ).fetchone()
    if row is None:
        return None
    event_id, object_id = row
    typo = _double_letter(object_id)
    if _exists(conn, "object", typo):
        return None
    _retarget(conn, "event_object",
              {"ocel_event_id": event_id, "ocel_object_id": object_id},
              {"ocel_object_id": typo})
    return {"affected_ids": [typo], "original_values": {typo: object_id}}


def inject_dangling_e2o_relationship_missing_both(conn: sqlite3.Connection) -> dict | None:
    """dangling_e2o_relationship Hard: a confirm order's `order` link breaks
    on both ends — two letters of the event id swapped ('cnofirm_o-…') and a
    digit dropped from the order id ('o-99006'). The dropped digit leaves
    several order ids equally close; only the (repaired) event id tells
    which order was meant, so this end needs the LLM."""
    row = conn.execute(
        "SELECT eo.ocel_event_id, eo.ocel_object_id FROM event_object eo "
        "JOIN event e ON e.ocel_id = eo.ocel_event_id "
        "WHERE e.ocel_type = ? AND eo.ocel_qualifier = ? "
        "ORDER BY eo.ocel_event_id LIMIT 1 OFFSET 5",
        (get_om_event_type("confirm order"), get_om_e2o_qualifier("order")),
    ).fetchone()
    if row is None:
        return None
    event_id, object_id = row
    event_typo, object_typo = _swap_letters(event_id), _drop_digit(object_id)
    if _exists(conn, "event", event_typo) or _exists(conn, "object", object_typo):
        return None
    _retarget(conn, "event_object",
              {"ocel_event_id": event_id, "ocel_object_id": object_id},
              {"ocel_event_id": event_typo, "ocel_object_id": object_typo})
    return {"affected_ids": [event_typo, object_typo],
            "original_values": {event_typo: event_id, object_typo: object_id}}


# ---------------------------------------------------------------------------
# dangling_o2o_relationship (Easy/Hard)
# ---------------------------------------------------------------------------


def _sales_rep_link(conn: sqlite3.Connection, offset: int) -> tuple[str, str, str] | None:
    qualifier = get_om_o2o_qualifier("primarySalesRep")
    row = conn.execute(
        "SELECT ocel_source_id, ocel_target_id FROM object_object "
        "WHERE ocel_qualifier = ? ORDER BY ocel_source_id LIMIT 1 OFFSET ?",
        (qualifier, offset),
    ).fetchone()
    return None if row is None else (row[0], row[1], qualifier)


def inject_dangling_o2o_relationship_missing_source(conn: sqlite3.Connection) -> dict | None:
    """dangling_o2o_relationship Easy: a customer → primary sales rep link
    misspells the customer (one doubled letter)."""
    link = _sales_rep_link(conn, 0)
    if link is None:
        return None
    src, dst, qualifier = link
    typo = _double_letter(src)
    if _exists(conn, "object", typo):
        return None
    _retarget(conn, "object_object",
              {"ocel_source_id": src, "ocel_target_id": dst, "ocel_qualifier": qualifier},
              {"ocel_source_id": typo})
    return {"affected_ids": [typo], "original_values": {typo: src}}


def inject_dangling_o2o_relationship_missing_both_typo(conn: sqlite3.Connection) -> dict | None:
    """dangling_o2o_relationship Hard: a customer → primary sales rep link
    misspells both names (a doubled letter in the customer, two swapped
    letters in the employee)."""
    link = _sales_rep_link(conn, 1)
    if link is None:
        return None
    src, dst, qualifier = link
    src_typo, dst_typo = _double_letter(src), _swap_letters(dst)
    if _exists(conn, "object", src_typo) or _exists(conn, "object", dst_typo):
        return None
    _retarget(conn, "object_object",
              {"ocel_source_id": src, "ocel_target_id": dst, "ocel_qualifier": qualifier},
              {"ocel_source_id": src_typo, "ocel_target_id": dst_typo})
    return {"affected_ids": [src_typo, dst_typo],
            "original_values": {src_typo: src, dst_typo: dst}}


# ---------------------------------------------------------------------------
# missing_object (Easy/Hard)
#
# Each injector inserts an E2O row whose ocel_object_id is a plausibly-typed
# but non-existent id. These rows also trip the dangling_e2o_relationship
# detector — that's expected and intentional; the dashboard routes them to
# the missing_object cell.
# ---------------------------------------------------------------------------


def _delete_object(conn: sqlite3.Connection, ocel_id: str, ocel_type: str) -> None:
    """Remove an object from `object` and all its rows (initial state and
    changes) from its per-type table, so only references to it remain."""
    conn.execute("DELETE FROM object WHERE ocel_id = ?", (ocel_id,))
    row = conn.execute(
        "SELECT ocel_type_map FROM object_map_type WHERE ocel_type = ?", (ocel_type,)
    ).fetchone()
    if row:
        conn.execute(f'DELETE FROM "object_{row[0]}" WHERE ocel_id = ?', (ocel_id,))


def inject_missing_object_order_easy(conn: sqlite3.Connection) -> dict | None:
    """missing_object Easy: delete a real order object that is still
    referenced by a 'place order' E2O row, leaving that row dangling.

    Easy because the type is unambiguous from the surviving E2O qualifier,
    and the peer set for the LLM to imitate is large.
    """
    place_type = get_om_event_type("place order")
    order_qual = get_om_e2o_qualifier("order")
    row = conn.execute(
        "SELECT o.ocel_id, o.ocel_type FROM object o "
        "JOIN event_object eo ON eo.ocel_object_id = o.ocel_id "
        "JOIN event e ON e.ocel_id = eo.ocel_event_id "
        "WHERE e.ocel_type = ? AND eo.ocel_qualifier = ? LIMIT 1",
        (place_type, order_qual),
    ).fetchone()
    if row is None:
        return None
    ocel_id, ocel_type = row
    _delete_object(conn, ocel_id, ocel_type)
    return {"affected_ids": [ocel_id], "original_values": {ocel_id: ocel_type}}


def inject_missing_object_package_hard(conn: sqlite3.Connection) -> dict | None:
    """missing_object Hard: delete a real package object that is still
    referenced by its 'create package' E2O row (qualifier `creates`),
    leaving the E2O and O2O rows that point at it dangling.

    Hard because package ids are opaque (`p-…`), so the resolver can't lean
    on id shape and must infer the type from the qualifiers. A package is
    referenced by only a handful of rows (vs. thousands for a product),
    so the injection stays one contained issue.
    """
    create_type = get_om_event_type("create package")
    creates_qual = get_om_e2o_qualifier("creates")
    row = conn.execute(
        "SELECT o.ocel_id, o.ocel_type FROM object o "
        "JOIN event_object eo ON eo.ocel_object_id = o.ocel_id "
        "JOIN event e ON e.ocel_id = eo.ocel_event_id "
        "WHERE e.ocel_type = ? AND eo.ocel_qualifier = ? "
        # A package whose weight no other package shares: removing it must
        # not break up a legitimate duplicate_objects_on_attributes group.
        "  AND o.ocel_id IN (SELECT ocel_id FROM object_Packages WHERE weight IN ("
        "    SELECT weight FROM object_Packages GROUP BY weight HAVING COUNT(*) = 1)) "
        "ORDER BY o.ocel_id LIMIT 1",
        (create_type, creates_qual),
    ).fetchone()
    if row is None:
        return None
    ocel_id, ocel_type = row
    _delete_object(conn, ocel_id, ocel_type)
    return {"affected_ids": [ocel_id], "original_values": {ocel_id: ocel_type}}


# ---------------------------------------------------------------------------
# duplicate_o2o_relations (Easy/Hard)
#
# Insert extra copies of a real (source, target, qualifier) triple in
# `object_object`. Both endpoints exist — it's the triple that's redundant.
# The clean fixture leaves this table without a PRIMARY KEY constraint, so
# straight INSERTs are enough; no schema tweaks needed.
# ---------------------------------------------------------------------------


def _insert_o2o_copies(
    conn: sqlite3.Connection, src: str, tgt: str, qual: str, extras: int
) -> tuple[str, str, str]:
    for _ in range(extras):
        conn.execute(
            "INSERT INTO object_object VALUES (?, ?, ?)",
            (src, tgt, qual),
        )
    return src, tgt, qual


def inject_duplicate_o2o_relations_comprises_easy(
    conn: sqlite3.Connection,
) -> tuple[str, str, str]:
    """duplicate_o2o_relations Easy: insert duplicate o2o triple."""
    qual = get_om_o2o_qualifier("comprises")
    row = conn.execute(
        "SELECT ocel_source_id, ocel_target_id FROM object_object WHERE ocel_qualifier = ? LIMIT 1", (qual,)
    ).fetchone()
    if row is None:
        # Fallback: create relationship between any two materials
        src = conn.execute("SELECT ocel_id FROM object LIMIT 1").fetchone()
        tgt = conn.execute("SELECT ocel_id FROM object LIMIT 1 OFFSET 1").fetchone()
        if src and tgt:
            return _insert_o2o_copies(conn, src[0], tgt[0], qual, extras=2)
        return ("fake", "fake", qual)
    return _insert_o2o_copies(conn, row[0], row[1], qual, extras=2)


def inject_duplicate_o2o_relations_sales_rep_hard(
    conn: sqlite3.Connection,
) -> tuple[str, str, str]:
    """duplicate_o2o_relations Hard: a low-frequency (~15 rows total)
    `primarySalesRep` triple gets two extra copies. Hard because the
    surrounding qualifier is rare — one accidental extra could plausibly
    look like a legitimate multi-rep assignment."""
    return _insert_o2o_copies(
        conn, "Danube Pharmaceuticals BV", "Christine von Dobbert",
        "primarySalesRep", extras=2,
    )


# ---------------------------------------------------------------------------
# o2o_self_loop (Easy/Hard)
#
# Insert an `object_object` row where source == target (same object id
# under a qualifier). Structurally illegal — an object relating to itself
# under a qualifier is not what `object_object` is for.
# ---------------------------------------------------------------------------


def inject_o2o_self_loop_order_easy(conn: sqlite3.Connection) -> str:
    """o2o_self_loop Easy: an order references itself under `contains`.
    Easy because orders are the dataset's central entity — a self-loop
    among them is glaring."""
    conn.execute(
        "INSERT INTO object_object VALUES (?, ?, ?)",
        ("o-990001", "o-990001", "contains"),
    )
    return "o-990001"


def inject_o2o_self_loop_product_hard(conn: sqlite3.Connection) -> str:
    """o2o_self_loop Hard: a product references itself under a qualifier
    that doesn't otherwise appear on products (`is a` typically links
    items to products). Hard because the qualifier itself is
    off-diagonal — the detector must still flag on the source=target
    condition alone, without leaning on qualifier semantics."""
    conn.execute(
        "INSERT INTO object_object VALUES (?, ?, ?)",
        ("Echo", "Echo", "is a"),
    )
    return "Echo"


# ---------------------------------------------------------------------------
# duplicate_e2o_relations (Easy/Hard)
#
# Insert extra copies of a real (event, object, qualifier) triple in
# `event_object`. Event-side mirror of `duplicate_o2o_relations`; the
# clean fixture also leaves this table PK-less.
# ---------------------------------------------------------------------------


def _insert_e2o_copies(
    conn: sqlite3.Connection, ev: str, obj: str, qual: str, extras: int
) -> tuple[str, str, str]:
    for _ in range(extras):
        conn.execute(
            "INSERT INTO event_object VALUES (?, ?, ?)",
            (ev, obj, qual),
        )
    return ev, obj, qual


def inject_duplicate_e2o_relations_order_easy(
    conn: sqlite3.Connection,
) -> tuple[str, str, str]:
    """duplicate_e2o_relations Easy: a place_order↔order triple gets two
    extra copies. Order-touching events are the highest-signal edges in
    the log, so an accidental extra copy sits directly on the critical
    path."""
    return _insert_e2o_copies(conn, "place_o-990001", "o-990001", "order", extras=2)


def inject_duplicate_e2o_relations_sales_person_hard(
    conn: sqlite3.Connection,
) -> tuple[str, str, str]:
    """duplicate_e2o_relations Hard: a place_order↔sales_person triple
    gets two extra copies. Hard because `sales person` is a lower-volume
    qualifier (~2k rows) and multi-rep assignments could look real; the
    detector still fires because it's the SAME (event, sales_person)
    edge repeated, not two different reps on one order."""
    ev = conn.execute(
        "SELECT ocel_event_id, ocel_object_id "
        "FROM event_object WHERE ocel_qualifier = 'sales person' LIMIT 1"
    ).fetchone()
    if not ev:
        return "", "", ""
    return _insert_e2o_copies(conn, ev[0], ev[1], "sales person", extras=2)
