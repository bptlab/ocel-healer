"""Synthetic evaluation runner with the LLM stubbed out."""

import sqlite3
from argparse import Namespace
from datetime import timedelta

import pytest

import src.evaluation.evaluate_synthetic as es
from src.evaluation.build_eval_log import build_eval_log
from src.llm.tasks._base import DetectionResult


@pytest.fixture(scope="module")
def eval_log(tmp_path_factory) -> str:
    return build_eval_log(dst=str(tmp_path_factory.mktemp("eval") / "eval.sqlite"))


def _args(eval_log: str, only: str) -> Namespace:
    return Namespace(eval_log=eval_log, runs=1, only=only, distractors=5, seed=0,
                     time_tolerance=timedelta(hours=1))


def _apply_sql(sql: str, params: tuple):
    def apply(db, action, dry_run=True):
        with sqlite3.connect(db) as conn:
            conn.execute(sql, params)
        return "ok"
    return apply


def test_values_close():
    tol = timedelta(hours=1)
    assert es.values_close(0.78, 0.8, tol)
    assert not es.values_close(0.78, 5.0, tol)
    assert es.values_close("2023-04-03 12:08:18", "2023-04-03 12:38:00", tol)
    assert not es.values_close("2023-04-03 12:08:18", "2023-04-05 12:08:18", tol)
    assert not es.values_close("manager", "Sales", tol)


def test_rule_scenario_restored(eval_log, tmp_path, monkeypatch):
    """A repair that restores the clean value counts as lenient and strict."""
    monkeypatch.setattr(es, "suggest_repair", lambda key, row, db: {"kind": "update"})
    monkeypatch.setattr(es, "apply_repair", _apply_sql(
        "UPDATE object_Products SET weight = 0.78 "
        "WHERE ocel_id = 'Echo' AND ocel_changed_field IS NULL", ()))
    [rec] = es.evaluate(_args(eval_log, "missing_attribute_value/easy"), tmp_path)
    assert rec["error"] == ""
    assert rec["detected"] and rec["lenient"] and rec["strict"]
    assert rec["repairs_applied"] == 1
    assert (tmp_path / "runs.csv").read_text().count("\n") == 2


def test_rule_scenario_noop(eval_log, tmp_path, monkeypatch):
    """A declined repair leaves the issue: detected, but not resolved."""
    monkeypatch.setattr(es, "suggest_repair",
                        lambda key, row, db: {"kind": "noop", "rationale": "unsure"})
    [rec] = es.evaluate(_args(eval_log, "missing_attribute_value/easy"), tmp_path)
    assert rec["detected"] and not rec["lenient"] and not rec["strict"]
    assert rec["noop_reasons"] == "unsure"


def test_llm_scenario(eval_log, tmp_path, monkeypatch):
    """LLM cell: only the injected candidate is flagged; restoring its type
    resolves it, and no distractor counts as a false alarm."""
    def judge(key, rows, db, **kw):
        return [(r, DetectionResult(flagged=r["ocel_id"] == "o-990001", rationale="",
                                    confidence=1.0, suggested_value="orders"))
                for r in rows]
    monkeypatch.setattr(es, "detect_all_with_llm", judge)
    monkeypatch.setattr(es, "suggest_repair", lambda key, row, db: {"kind": "update"})
    monkeypatch.setattr(es, "apply_repair", _apply_sql(
        "UPDATE object SET ocel_type = 'orders' WHERE ocel_id = 'o-990001'", ()))
    [rec] = es.evaluate(_args(eval_log, "incorrect_object_type/easy"), tmp_path)
    assert rec["error"] == ""
    assert rec["detected"] and rec["lenient"] and rec["strict"]
    assert (rec["false_positives"], rec["distractors"]) == (0, 5)


def test_llm_scenario_no_repair_no_credit(eval_log, tmp_path, monkeypatch):
    """A flagged candidate whose repair is declined is not resolved, even if a
    later re-check stops flagging it."""
    def judge(key, rows, db, **kw):
        return [(r, DetectionResult(flagged=r["ocel_id"] == "o-990001", rationale="",
                                    confidence=1.0, suggested_value="orders"))
                for r in rows]
    monkeypatch.setattr(es, "detect_all_with_llm", judge)
    monkeypatch.setattr(es, "detect_with_llm", lambda key, row, db: DetectionResult(
        flagged=False, rationale="", confidence=1.0, suggested_value=None))
    monkeypatch.setattr(es, "suggest_repair",
                        lambda key, row, db: {"kind": "noop", "rationale": "unsure"})
    [rec] = es.evaluate(_args(eval_log, "incorrect_object_type/easy"), tmp_path)
    assert rec["detected"] and rec["repairs_applied"] == 0
    assert not rec["lenient"] and not rec["strict"]


def test_llm_repair_reuses_detection(eval_log, tmp_path, monkeypatch):
    """The repair gets the detection's suggestion stashed on the row."""
    seen = {}
    def judge(key, rows, db, **kw):
        return [(r, DetectionResult(flagged=r["ocel_id"] == "o-990001", rationale="r",
                                    confidence=0.9, suggested_value="orders"))
                for r in rows]
    def repair(key, row, db):
        seen.update(row)
        return {"kind": "noop", "rationale": ""}
    monkeypatch.setattr(es, "detect_all_with_llm", judge)
    monkeypatch.setattr(es, "suggest_repair", repair)
    es.evaluate(_args(eval_log, "incorrect_object_type/easy"), tmp_path)
    assert seen["_detected_suggestion"] == "orders"
    assert seen["_detected_confidence"] == 0.9


def test_new_violations_block_lenient(eval_log, tmp_path, monkeypatch):
    """Filling the missing weight with text removes the issue but adds a
    datatype violation: listed in new_violations, not lenient."""
    monkeypatch.setattr(es, "suggest_repair", lambda key, row, db: {"kind": "update"})
    monkeypatch.setattr(es, "apply_repair", _apply_sql(
        "UPDATE object_Products SET weight = 'heavy' "
        "WHERE ocel_id = 'Echo' AND ocel_changed_field IS NULL", ()))
    [rec] = es.evaluate(_args(eval_log, "missing_attribute_value/easy"), tmp_path)
    assert rec["repairs_applied"] == 1 and not rec["lenient"]
    assert rec["new_violations"] == "incorrect_attribute_datatype+1"


def test_timeout_counted(eval_log, tmp_path, monkeypatch):
    def repair(key, row, db):
        raise TimeoutError("timed out")
    monkeypatch.setattr(es, "suggest_repair", repair)
    [rec] = es.evaluate(_args(eval_log, "missing_attribute_value/easy"), tmp_path)
    assert rec["timeouts"] == 1 and rec["error"] == ""
    assert rec["action_kinds"] == "timeout"
