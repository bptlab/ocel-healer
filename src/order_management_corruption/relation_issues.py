"""Relation-side corruption injectors.

Covers issues rooted in the relation tables (`event_object`,
`object_object`): dangling E2O and O2O references (an endpoint id turned
into a one-character typo of the real id), deleted objects whose relations
survive (`missing_object`), duplicated relation triples, and O2O
self-loops.

Each `inject_<issue_key>_<flavor>(conn, picker)` corrupts `picker.n`
seeded, non-overlapping targets and returns one `Injection` per target
(never an empty list — the picker raises instead). `RELATION_INJECTORS`
maps ``(issue_key, tier)`` to the injector.
"""

from __future__ import annotations

import sqlite3
from typing import Callable

from src.order_management_corruption._common import (
    MAX_DELETE_FANOUT,
    Injection,
    TargetPicker,
    _delete_object_fully,
    _touch_low_fanout,
)


def _names_type(qualifier: str | None, ocel_type: str) -> bool:
    """Whether `detect_missing_object` infers `ocel_type` from `qualifier`
    (the type, its singular, or a qualifier ending in the singular)."""
    if not isinstance(qualifier, str) or not qualifier.strip():
        return False
    singular = ocel_type[:-1] if ocel_type.endswith("s") else ocel_type
    return qualifier in (ocel_type, singular) or qualifier.split()[-1] in (ocel_type, singular)


def _object_type(conn: sqlite3.Connection, ocel_id: str) -> str | None:
    row = conn.execute("SELECT ocel_type FROM object WHERE ocel_id = ?", (ocel_id,)).fetchone()
    return row[0] if row else None


def _accept_remap(real: dict, corrupted: dict) -> dict:
    """Repair rule for a dangling relation: remap to the real id, or drop it."""
    return {"mode": "one_of", "options": [
        {"action": "remap", **real},
        {"action": "delete", **corrupted},
    ]}


# ---------------------------------------------------------------------------
# dangling_e2o_relationship (Easy/Medium/Hard) — an existing E2O row gets a
# typo in its object id, event id, or both.
# ---------------------------------------------------------------------------


def _dangling_e2o(
    conn: sqlite3.Connection, picker: TargetPicker, *,
    tier: str, flavor: str, event_type: str, qualifier: str, typo_event: bool, typo_object: bool,
) -> list[Injection]:
    out = []
    for eid in picker.pick("SELECT ocel_id FROM event WHERE ocel_type = ?", (event_type,)):
        oid = conn.execute(
            "SELECT ocel_object_id FROM event_object WHERE ocel_event_id = ? AND ocel_qualifier = ?",
            (eid, qualifier),
        ).fetchone()[0]
        _touch_low_fanout(picker, [oid])
        bad_eid = picker.near_miss(eid) if typo_event else eid
        bad_oid = picker.near_miss(oid) if typo_object else oid
        conn.execute(
            "UPDATE event_object SET ocel_event_id = ?, ocel_object_id = ? "
            "WHERE ocel_event_id = ? AND ocel_object_id = ? AND ocel_qualifier = ?",
            (bad_eid, bad_oid, eid, oid, qualifier),
        )
        real = {"ocel_event_id": eid, "ocel_object_id": oid, "ocel_qualifier": qualifier}
        bad = {"ocel_event_id": bad_eid, "ocel_object_id": bad_oid, "ocel_qualifier": qualifier}
        side = "both" if typo_event and typo_object else "event" if typo_event else "object"
        expected = [{"issue": "dangling_e2o_relationship", **bad, "missing_side": side}]
        if typo_event:
            expected.append({"issue": "missing_event", **bad})
        if typo_object and _names_type(qualifier, _object_type(conn, oid)):
            expected.append({"issue": "missing_object", **bad})
        out.append(Injection(
            "dangling_e2o_relationship", tier, flavor, "e2o", bad,
            original=real, corrupted=bad,
            expected_rows=expected, accept=_accept_remap(real, bad),
        ))
    return out


def inject_dangling_e2o_relationship_missing_object_easy(conn, picker):
    """dangling_e2o_relationship Easy: a place order's `order` link points
    at a one-digit typo of the order id."""
    return _dangling_e2o(conn, picker, tier="easy", flavor="missing_object",
                         event_type="place order", qualifier="order",
                         typo_event=False, typo_object=True)


def inject_dangling_e2o_relationship_missing_event(conn, picker):
    """dangling_e2o_relationship Medium: a pick item's `item` link comes
    from a one-digit typo of the event id."""
    return _dangling_e2o(conn, picker, tier="medium", flavor="missing_event",
                         event_type="pick item", qualifier="item",
                         typo_event=True, typo_object=False)


def inject_dangling_e2o_relationship_missing_both(conn, picker):
    """dangling_e2o_relationship Hard: a confirm order's `sales person` link
    has typos on both ends — a digit in the event id, a letter in the
    employee's name."""
    return _dangling_e2o(conn, picker, tier="hard", flavor="missing_both",
                         event_type="confirm order", qualifier="sales person",
                         typo_event=True, typo_object=True)


# ---------------------------------------------------------------------------
# dangling_o2o_relationship (Easy/Medium/Hard) — an existing O2O row gets a
# typo in its source, target, or both. `picks` names the endpoint the
# picker samples (it is unique per row for the chosen qualifier).
# ---------------------------------------------------------------------------


def _dangling_o2o(
    conn: sqlite3.Connection, picker: TargetPicker, *,
    tier: str, flavor: str, qualifier: str, picks: str, typo_source: bool, typo_target: bool,
) -> list[Injection]:
    other = "ocel_target_id" if picks == "ocel_source_id" else "ocel_source_id"
    out = []
    for pid in picker.pick(
        f"SELECT {picks} FROM object_object WHERE ocel_qualifier = ?", (qualifier,)
    ):
        (xid,) = conn.execute(
            f"SELECT {other} FROM object_object WHERE {picks} = ? AND ocel_qualifier = ?",
            (pid, qualifier),
        ).fetchone()
        _touch_low_fanout(picker, [xid])
        src, tgt = (pid, xid) if picks == "ocel_source_id" else (xid, pid)
        bad_src = picker.near_miss(src) if typo_source else src
        bad_tgt = picker.near_miss(tgt) if typo_target else tgt
        conn.execute(
            "UPDATE object_object SET ocel_source_id = ?, ocel_target_id = ? "
            "WHERE ocel_source_id = ? AND ocel_target_id = ? AND ocel_qualifier = ?",
            (bad_src, bad_tgt, src, tgt, qualifier),
        )
        real = {"ocel_source_id": src, "ocel_target_id": tgt, "ocel_qualifier": qualifier}
        bad = {"ocel_source_id": bad_src, "ocel_target_id": bad_tgt, "ocel_qualifier": qualifier}
        side = "both" if typo_source and typo_target else "source" if typo_source else "target"
        out.append(Injection(
            "dangling_o2o_relationship", tier, flavor, "o2o", bad,
            original=real, corrupted=bad,
            expected_rows=[{"issue": "dangling_o2o_relationship", **bad, "missing_side": side}],
            accept=_accept_remap(real, bad),
        ))
    return out


def inject_dangling_o2o_relationship_missing_source(conn, picker):
    """dangling_o2o_relationship Easy: a `places` row whose source is a
    one-letter typo of the customer's name."""
    return _dangling_o2o(conn, picker, tier="easy", flavor="missing_source",
                         qualifier="places", picks="ocel_target_id",
                         typo_source=True, typo_target=False)


def inject_dangling_o2o_relationship_missing_target(conn, picker):
    """dangling_o2o_relationship Medium: a `comprises` row whose target is a
    one-digit typo of the item id."""
    return _dangling_o2o(conn, picker, tier="medium", flavor="missing_target",
                         qualifier="comprises", picks="ocel_target_id",
                         typo_source=False, typo_target=True)


def inject_dangling_o2o_relationship_missing_both_typo(conn, picker):
    """dangling_o2o_relationship Hard: a `packed by` row with typos on both
    ends — a digit in the package id, a letter in the employee's name."""
    return _dangling_o2o(conn, picker, tier="hard", flavor="missing_both_typo",
                         qualifier="packed by", picks="ocel_source_id",
                         typo_source=True, typo_target=True)


# ---------------------------------------------------------------------------
# missing_object (Easy/Medium/Hard) — the object and its object_<Type> rows
# are deleted; its E2O rows survive and trip `missing_object` (where the
# qualifier names the type) and `dangling_e2o_relationship`, its O2O rows
# trip `dangling_o2o_relationship`. Only objects with at most
# MAX_DELETE_FANOUT E2O rows are eligible, so the cascade stays small.
# ---------------------------------------------------------------------------


def _missing_object(
    conn: sqlite3.Connection, picker: TargetPicker, *,
    tier: str, flavor: str, ocel_type: str,
) -> list[Injection]:
    out = []
    ids = picker.pick("SELECT ocel_id FROM object WHERE ocel_type = ?", (ocel_type,),
                      max_fanout=MAX_DELETE_FANOUT)
    for oid in ids:
        removed = _delete_object_fully(conn, oid)
        _touch_low_fanout(picker, [r["ocel_event_id"] for r in removed["e2o"]] + [
            r["ocel_target_id"] if r["ocel_source_id"] == oid else r["ocel_source_id"]
            for r in removed["o2o"]
        ])
        expected = []
        for r in removed["e2o"]:
            rel = {"ocel_event_id": r["ocel_event_id"], "ocel_object_id": oid,
                   "ocel_qualifier": r["ocel_qualifier"]}
            expected.append({"issue": "dangling_e2o_relationship", **rel, "missing_side": "object"})
            if _names_type(r["ocel_qualifier"], ocel_type):
                expected.append({"issue": "missing_object", **rel})
        for r in removed["o2o"]:
            expected.append({
                "issue": "dangling_o2o_relationship",
                "ocel_source_id": r["ocel_source_id"], "ocel_target_id": r["ocel_target_id"],
                "ocel_qualifier": r["ocel_qualifier"],
                "missing_side": "source" if r["ocel_source_id"] == oid else "target",
            })
        out.append(Injection(
            "missing_object", tier, flavor, "object", oid,
            original=removed, corrupted=None,
            expected_rows=expected, accept={"mode": "exact", "value": ocel_type},
        ))
    return out


def inject_missing_object_order_easy(conn, picker):
    """missing_object Easy: delete an order. Its `order` E2O links name the
    type, and its customer and items stay linked through O2O."""
    return _missing_object(conn, picker, tier="easy", flavor="order", ocel_type="orders")


def inject_missing_object_item_medium(conn, picker):
    """missing_object Medium: delete an item. Its attributes must be
    reconstructed from the product it `is a` and its order's other items."""
    return _missing_object(conn, picker, tier="medium", flavor="item", ocel_type="items")


def inject_missing_object_package_hard(conn, picker):
    """missing_object Hard: delete a package. Only the `shipped package`
    links name the type; `creates` and the O2O rows to items and employees
    do not."""
    return _missing_object(conn, picker, tier="hard", flavor="package", ocel_type="packages")


# ---------------------------------------------------------------------------
# duplicate_e2o_relations / duplicate_o2o_relations (Easy/Medium/Hard) —
# extra copies of a real triple. Both endpoints exist.
# ---------------------------------------------------------------------------


def _duplicate_e2o(
    conn: sqlite3.Connection, picker: TargetPicker, *,
    tier: str, flavor: str, event_type: str, qualifier: str, extras: int,
) -> list[Injection]:
    out = []
    for eid in picker.pick("SELECT ocel_id FROM event WHERE ocel_type = ?", (event_type,)):
        (oid,) = conn.execute(
            "SELECT ocel_object_id FROM event_object WHERE ocel_event_id = ? AND ocel_qualifier = ?",
            (eid, qualifier),
        ).fetchone()
        _touch_low_fanout(picker, [oid])
        rel = {"ocel_event_id": eid, "ocel_object_id": oid, "ocel_qualifier": qualifier}
        conn.executemany("INSERT INTO event_object VALUES (?, ?, ?)", [(eid, oid, qualifier)] * extras)
        out.append(Injection(
            "duplicate_e2o_relations", tier, flavor, "e2o", rel,
            original=1, corrupted=1 + extras,
            expected_rows=[{"issue": "duplicate_e2o_relations", **rel}],
            accept={"mode": "delete_any_of", "keep": 1, **rel},
        ))
    return out


def inject_duplicate_e2o_relations_order_easy(conn, picker):
    """duplicate_e2o_relations Easy: a place order's `order` link recorded
    three times."""
    return _duplicate_e2o(conn, picker, tier="easy", flavor="order",
                          event_type="place order", qualifier="order", extras=2)


def inject_duplicate_e2o_relations_item_medium(conn, picker):
    """duplicate_e2o_relations Medium: a pick item's `item` link recorded
    twice, among the log's highest-volume qualifier."""
    return _duplicate_e2o(conn, picker, tier="medium", flavor="item",
                          event_type="pick item", qualifier="item", extras=1)


def inject_duplicate_e2o_relations_sales_person_hard(conn, picker):
    """duplicate_e2o_relations Hard: a confirm order's `sales person` link
    recorded twice; a second copy could pass for a second rep."""
    return _duplicate_e2o(conn, picker, tier="hard", flavor="sales_person",
                          event_type="confirm order", qualifier="sales person", extras=1)


def _duplicate_o2o(
    conn: sqlite3.Connection, picker: TargetPicker, *,
    tier: str, flavor: str, qualifier: str, picks: str, extras: int,
) -> list[Injection]:
    other = "ocel_target_id" if picks == "ocel_source_id" else "ocel_source_id"
    out = []
    for pid in picker.pick(
        f"SELECT {picks} FROM object_object WHERE ocel_qualifier = ?", (qualifier,)
    ):
        (xid,) = conn.execute(
            f"SELECT {other} FROM object_object WHERE {picks} = ? AND ocel_qualifier = ?",
            (pid, qualifier),
        ).fetchone()
        _touch_low_fanout(picker, [xid])
        src, tgt = (pid, xid) if picks == "ocel_source_id" else (xid, pid)
        rel = {"ocel_source_id": src, "ocel_target_id": tgt, "ocel_qualifier": qualifier}
        conn.executemany("INSERT INTO object_object VALUES (?, ?, ?)", [(src, tgt, qualifier)] * extras)
        out.append(Injection(
            "duplicate_o2o_relations", tier, flavor, "o2o", rel,
            original=1, corrupted=1 + extras,
            expected_rows=[{"issue": "duplicate_o2o_relations", **rel}],
            accept={"mode": "delete_any_of", "keep": 1, **rel},
        ))
    return out


def inject_duplicate_o2o_relations_comprises_easy(conn, picker):
    """duplicate_o2o_relations Easy: an order→item `comprises` row recorded
    three times."""
    return _duplicate_o2o(conn, picker, tier="easy", flavor="comprises",
                          qualifier="comprises", picks="ocel_target_id", extras=2)


def inject_duplicate_o2o_relations_places_medium(conn, picker):
    """duplicate_o2o_relations Medium: a customer→order `places` row
    recorded twice."""
    return _duplicate_o2o(conn, picker, tier="medium", flavor="places",
                          qualifier="places", picks="ocel_target_id", extras=1)


def inject_duplicate_o2o_relations_sales_rep_hard(conn, picker):
    """duplicate_o2o_relations Hard: a customer's `primarySalesRep` row
    recorded twice; the qualifier is rare (15 rows), so the copy could
    pass for a second assignment."""
    return _duplicate_o2o(conn, picker, tier="hard", flavor="sales_rep",
                          qualifier="primarySalesRep", picks="ocel_source_id", extras=1)


# ---------------------------------------------------------------------------
# o2o_self_loop (Easy/Medium/Hard) — an existing object linked to itself.
# ---------------------------------------------------------------------------


def _o2o_self_loop(
    conn: sqlite3.Connection, picker: TargetPicker, *,
    tier: str, flavor: str, ocel_type: str, qualifier: str,
) -> list[Injection]:
    out = []
    for oid in picker.pick("SELECT ocel_id FROM object WHERE ocel_type = ?", (ocel_type,)):
        rel = {"ocel_source_id": oid, "ocel_target_id": oid, "ocel_qualifier": qualifier}
        conn.execute("INSERT INTO object_object VALUES (?, ?, ?)", (oid, oid, qualifier))
        out.append(Injection(
            "o2o_self_loop", tier, flavor, "o2o", rel,
            original=None, corrupted=rel,
            expected_rows=[{"issue": "o2o_self_loop", **rel}],
            accept={"mode": "delete_any_of", "keep": 0, **rel},
        ))
    return out


def inject_o2o_self_loop_order_easy(conn, picker):
    """o2o_self_loop Easy: an order `comprises` itself."""
    return _o2o_self_loop(conn, picker, tier="easy", flavor="order",
                          ocel_type="orders", qualifier="comprises")


def inject_o2o_self_loop_employee_medium(conn, picker):
    """o2o_self_loop Medium: an employee is their own `primarySalesRep` —
    superficially plausible, still structurally wrong."""
    return _o2o_self_loop(conn, picker, tier="medium", flavor="employee",
                          ocel_type="employees", qualifier="primarySalesRep")


def inject_o2o_self_loop_product_hard(conn, picker):
    """o2o_self_loop Hard: a product `is a` itself; the qualifier is a real
    item→product one, so only source = target gives it away."""
    return _o2o_self_loop(conn, picker, tier="hard", flavor="product",
                          ocel_type="products", qualifier="is a")


RELATION_INJECTORS: dict[tuple[str, str], Callable[[sqlite3.Connection, TargetPicker], list[Injection]]] = {
    ("dangling_e2o_relationship", "easy"):   inject_dangling_e2o_relationship_missing_object_easy,
    ("dangling_e2o_relationship", "medium"): inject_dangling_e2o_relationship_missing_event,
    ("dangling_e2o_relationship", "hard"):   inject_dangling_e2o_relationship_missing_both,
    ("dangling_o2o_relationship", "easy"):   inject_dangling_o2o_relationship_missing_source,
    ("dangling_o2o_relationship", "medium"): inject_dangling_o2o_relationship_missing_target,
    ("dangling_o2o_relationship", "hard"):   inject_dangling_o2o_relationship_missing_both_typo,
    ("missing_object", "easy"):              inject_missing_object_order_easy,
    ("missing_object", "medium"):            inject_missing_object_item_medium,
    ("missing_object", "hard"):              inject_missing_object_package_hard,
    ("duplicate_e2o_relations", "easy"):     inject_duplicate_e2o_relations_order_easy,
    ("duplicate_e2o_relations", "medium"):   inject_duplicate_e2o_relations_item_medium,
    ("duplicate_e2o_relations", "hard"):     inject_duplicate_e2o_relations_sales_person_hard,
    ("duplicate_o2o_relations", "easy"):     inject_duplicate_o2o_relations_comprises_easy,
    ("duplicate_o2o_relations", "medium"):   inject_duplicate_o2o_relations_places_medium,
    ("duplicate_o2o_relations", "hard"):     inject_duplicate_o2o_relations_sales_rep_hard,
    ("o2o_self_loop", "easy"):               inject_o2o_self_loop_order_easy,
    ("o2o_self_loop", "medium"):             inject_o2o_self_loop_employee_medium,
    ("o2o_self_loop", "hard"):               inject_o2o_self_loop_product_hard,
}
