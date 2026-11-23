"""Duplicate ids with conflicting types: the LLM's `canonical_type` drives
the repair, and re-detection cleans up the remaining same-type copies."""

import shutil
import sqlite3

import pytest

from src.detection.error_detection import detect_all
from src.llm.actions import apply_repair, from_task_result
from src.llm.tasks import get_task
from src.order_management_corruption.object_issues import (
    inject_duplicate_objects_on_ids_triple_conflicting_type,
)
from src.order_management_corruption._common import _remove_all_primary_keys

BASELINE = "data/synthetic/order-management-baseline.sqlite"


@pytest.fixture
def db(tmp_path) -> str:
    path = str(tmp_path / "dup.sqlite")
    shutil.copy(BASELINE, path)
    with sqlite3.connect(path) as conn:
        _remove_all_primary_keys(conn)
        inject_duplicate_objects_on_ids_triple_conflicting_type(conn)
    return path


def _repair(db: str, payload: dict) -> dict:
    [row] = detect_all(db)["duplicate_objects_on_ids"].to_dicts()
    action = from_task_result(get_task("duplicate_objects_on_ids"), row, payload)
    if action["kind"] != "noop":
        apply_repair(db, action, dry_run=False)
    return action


def test_context_marks_rows_backed_by_attribute_table(db):
    [row] = detect_all(db)["duplicate_objects_on_ids"].to_dicts()
    with sqlite3.connect(db) as conn:
        ctx = get_task("duplicate_objects_on_ids").build_context(conn, row)
    backed = {r["ocel_type"]: r["has_attribute_row"] for r in ctx["duplicate_rows"]}
    assert backed == {"customers": True, "orders": False}


def test_canonical_type_resolves_in_two_steps(db):
    payload = {"canonical_id": "x", "canonical_type": "Customers",
               "rationale": "", "confidence": 0.9}
    assert _repair(db, payload)["kind"] == "delete_all"
    assert _repair(db, payload)["kind"] == "delete"
    assert detect_all(db)["duplicate_objects_on_ids"].is_empty()


def test_unknown_canonical_type_stays_manual(db):
    action = _repair(db, {"canonical_id": "x", "canonical_type": "vendors",
                          "rationale": "", "confidence": 0.9})
    assert action["kind"] == "noop"
