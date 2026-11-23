"""Lookup baseline for the event attributes the evaluation log adds.

`build_eval_log.py` derives `resource` (the employee linked to the event)
and `amount` (the linked order's price) from data already in the log. A
missing or broken value can therefore be recovered by looking it up the same
way. This baseline does exactly that, with no LLM, and shows what the LLM's
repair is measured against on these cells.
"""

from __future__ import annotations

import sqlite3

from src.evaluation.build_eval_log import EMPLOYEE_QUALIFIERS, NO_EMPLOYEE_RESOURCE
from src.llm.actions import event_attribute_target

# The rule-detected cells whose repair is a plain lookup of a derived attribute.
LOOKUP_KEYS = {"missing_event_attribute_value", "incorrect_event_attribute_datatype"}


def lookup_value(conn: sqlite3.Connection, event_id: str, column: str):
    """The value `build_eval_log` derived for `column` of `event_id`."""
    if column == "resource":
        priority = {q: i for i, q in enumerate(EMPLOYEE_QUALIFIERS)}
        linked = conn.execute(
            f"SELECT ocel_object_id, ocel_qualifier FROM event_object "
            f"WHERE ocel_event_id = ? AND ocel_qualifier IN ({', '.join('?' * len(priority))})",
            (event_id, *priority),
        ).fetchall()
        return min(linked, key=lambda r: (priority[r[1]], r[0]))[0] if linked else NO_EMPLOYEE_RESOURCE
    if column == "amount":
        row = conn.execute(
            "SELECT o.price FROM event_object eo JOIN object_Orders o "
            "ON o.ocel_id = eo.ocel_object_id AND o.ocel_changed_field IS NULL "
            "WHERE eo.ocel_event_id = ? AND eo.ocel_qualifier = 'order' LIMIT 1",
            (event_id,),
        ).fetchone()
        return row[0] if row else None
    raise ValueError(f"no lookup for column {column!r}")


def lookup_repair(issue_key: str, row: dict, sqlite_path: str) -> dict:
    """An update action that sets the cell to its looked-up value."""
    target = event_attribute_target(row, sqlite_path=sqlite_path)
    if target is None:
        raise ValueError(f"cannot locate the event attribute of {row!r}")
    with sqlite3.connect(sqlite_path) as conn:
        value = lookup_value(conn, target["target_pk"]["ocel_id"], target["column"])
    return {
        "kind": "update", **target, "new_value": value,
        "rationale": "lookup baseline", "confidence": 1.0,
        "issue_key": issue_key, "proposed_value": value, "source": "baseline",
    }
