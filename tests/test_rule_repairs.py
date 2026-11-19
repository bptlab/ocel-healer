"""Repairs that need no LLM, scored by the synthetic evaluation runner."""

import shutil
import sqlite3
from argparse import Namespace
from datetime import timedelta

import pytest

import src.evaluation.evaluate_synthetic as es
from src.detection.error_detection import detect_all
from src.evaluation.build_eval_log import build_eval_log
from src.evaluation.scenarios import SCENARIOS
from src.llm.resolution import suggest_repair
from src.llm.sampling import closest_ids
from src.llm.tasks import get_task
from src.order_management_corruption._common import _remove_all_primary_keys


@pytest.fixture(scope="module")
def eval_log(tmp_path_factory) -> str:
    return build_eval_log(dst=str(tmp_path_factory.mktemp("eval") / "eval.sqlite"))


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    def fail(*a, **kw):
        raise AssertionError("LLM must not be called")
    monkeypatch.setattr("src.llm.resolution._call_task_or_legacy", fail)


def _args(eval_log: str, only: str) -> Namespace:
    return Namespace(eval_log=eval_log, runs=1, only=only, distractors=5, seed=0,
                     time_tolerance=timedelta(hours=1))


def _injected(eval_log: str, name: str, tmp_path) -> str:
    db = str(tmp_path / "run.sqlite")
    shutil.copy2(eval_log, db)
    sc = next(s for s in SCENARIOS if s.name == name)
    with sqlite3.connect(db) as conn:
        _remove_all_primary_keys(conn)
        sc.inject(conn)
    return db


@pytest.mark.parametrize("only", [
    "duplicate_events_on_attributes", "duplicate_objects_on_attributes",
    "dangling_o2o_relationship", "dangling_e2o_relationship/easy",
])
def test_rule_cells_restore_clean_log(eval_log, tmp_path, only):
    for rec in es.evaluate(_args(eval_log, only), tmp_path):
        assert rec["error"] == ""
        assert rec["strict"], rec


def test_duplicate_group_with_own_relations_is_kept(eval_log):
    """Employees sharing a role and start time are distinct people."""
    [row] = [r for r in detect_all(eval_log)["duplicate_objects_on_attributes"].to_dicts()
             if r["object_type"] == "employees" and "Wil van der Aalst" in r["ocel_ids"]]
    assert suggest_repair("duplicate_objects_on_attributes", row, eval_log)["kind"] == "noop"


def test_dangling_both_ends_fixes_clear_end_first(eval_log, tmp_path):
    """The swapped event id is fixed by rule; the dropped digit of the order
    id is ambiguous and left to the LLM, with the true id ranked first."""
    db = _injected(eval_log, "dangling_e2o_relationship/hard", tmp_path)
    [row] = detect_all(db)["dangling_e2o_relationship"].to_dicts()
    action = suggest_repair("dangling_e2o_relationship", row, db)
    assert action["kind"] == "update" and action["column"] == "ocel_event_id"
    es.apply_repair(db, action, dry_run=False)
    [row] = detect_all(db)["dangling_e2o_relationship"].to_dicts()
    assert row["missing_side"] == "object"
    with sqlite3.connect(db) as conn:
        assert get_task("dangling_e2o_relationship").rule_action(conn, row) is None
        top = closest_ids(conn, row["ocel_object_id"], kind="object",
                          expected_type="order", context_id=row["ocel_event_id"])[0]
    assert row["ocel_event_id"].endswith(top["ocel_id"])


def test_placeholder_emptied_then_followed_up(eval_log, tmp_path, monkeypatch):
    """'unknown' in a REAL column is emptied by rule, then the missing value
    gets one follow-up repair."""
    keys = []
    def repair(key, row, db):
        keys.append(key)
        return suggest_repair(key, row, db) if key == "incorrect_event_attribute_datatype" \
            else {"kind": "noop", "rationale": "stub"}
    monkeypatch.setattr(es, "suggest_repair", repair)
    [rec] = es.evaluate(_args(eval_log, "incorrect_event_attribute_datatype/easy"), tmp_path)
    assert keys == ["incorrect_event_attribute_datatype", "missing_event_attribute_value"]
    assert rec["action_kinds"] == "update;followup:noop"
    assert rec["new_violations"] == "missing_event_attribute_value+1" and not rec["strict"]


def test_peer_price_is_not_a_duplicate(eval_log, tmp_path, monkeypatch):
    """Filling a missing order price with a peer's price (within 10 %) no
    longer makes the two orders attribute duplicates."""
    def apply(db, action, dry_run=True):
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE object_Orders SET price = 1268.98 WHERE ocel_id = 'o-990001'")
    monkeypatch.setattr(es, "suggest_repair", lambda key, row, db: {"kind": "update"})
    monkeypatch.setattr(es, "apply_repair", apply)
    [rec] = es.evaluate(_args(eval_log, "missing_attribute_value/hard"), tmp_path)
    assert rec["strict"] and rec["new_violations"] == ""


def test_missing_event_insert_fills_attributes(eval_log, tmp_path):
    db = _injected(eval_log, "missing_event/hard", tmp_path)
    row = detect_all(db)["missing_event"].to_dicts()[0]
    task = get_task("missing_event")
    with sqlite3.connect(db) as conn:
        ctx = task.build_context(conn, row)
    assert {"ocel_object_id": "Detlef Wetzeler", "ocel_type": "employees",
            "qualifier": "packer"} in ctx["event_links"]
    result = task.parse_payload(row, {"ocel_type": "create package", "attributes": {
        "ocel_time": "2023-04-08 01:00:00", "resource": "Detlef Wetzeler", "bogus": 1}})
    assert result.inserts[1]["columns"] == {
        "ocel_id": "create_p-660001", "ocel_time": "2023-04-08 01:00:00",
        "resource": "Detlef Wetzeler"}


def test_missing_object_insert_times_first_event(eval_log, tmp_path):
    db = _injected(eval_log, "missing_object/hard", tmp_path)
    row = detect_all(db)["missing_object"].to_dicts()[0]
    task = get_task("missing_object")
    with sqlite3.connect(db) as conn:
        task.build_context(conn, row)
    result = task.parse_payload(row, {"ocel_type": "packages", "attributes": {"weight": 8.484}})
    assert result.inserts[1]["columns"]["ocel_time"] == "2023-04-08 01:00:00"


def test_decoded_role_restores_clean_log(eval_log, tmp_path, monkeypatch):
    """The scrambled role is the employee's real one, so decoding it is exact."""
    def apply(db, action, dry_run=True):
        with sqlite3.connect(db) as conn:
            (oid, blob), = conn.execute(
                "SELECT ocel_id, role FROM object_Employees WHERE typeof(role) = 'blob'").fetchall()
            conn.execute("UPDATE object_Employees SET role = ? WHERE ocel_id = ? "
                         "AND ocel_changed_field IS NULL", (blob.decode("utf-16-le"), oid))
    monkeypatch.setattr(es, "suggest_repair", lambda key, row, db: {"kind": "update"})
    monkeypatch.setattr(es, "apply_repair", apply)
    [rec] = es.evaluate(_args(eval_log, "incorrect_attribute_datatype/hard"), tmp_path)
    assert rec["strict"]


def test_reinsertion_followed_up(eval_log, tmp_path, monkeypatch):
    """A reinserted event missing its resource gets one follow-up repair."""
    keys = []
    def repair(key, row, db):
        keys.append(key)
        if key == "missing_event":
            return {"kind": "insert", "inserts": [
                {"table": "event", "columns": {"ocel_id": "place_o-990002", "ocel_type": "place order"}},
                {"table": "event_PlaceOrder", "columns": {"ocel_id": "place_o-990002",
                                                          "ocel_time": "2023-04-03 16:36:55"}}]}
        return {"kind": "noop", "rationale": "stub"}
    monkeypatch.setattr(es, "suggest_repair", repair)
    [rec] = es.evaluate(_args(eval_log, "missing_event/easy"), tmp_path)
    assert keys == ["missing_event", "missing_event_attribute_value"]
    assert rec["action_kinds"] == "insert;followup:noop"
