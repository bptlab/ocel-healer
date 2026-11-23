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
    """A repair that restores the clean value is strict, with no error left."""
    monkeypatch.setattr(es, "suggest_repair", lambda key, row, db: {"kind": "update"})
    monkeypatch.setattr(es, "apply_repair", _apply_sql(
        "UPDATE object_Products SET weight = 0.78 "
        "WHERE ocel_id = 'Echo' AND ocel_changed_field IS NULL", ()))
    [rec] = es.evaluate(_args(eval_log, "missing_attribute_value/easy"), tmp_path)
    assert rec["error"] == ""
    assert rec["detected"] and rec["strict"]
    assert rec["repairs_applied"] == 1
    assert (rec["cells_wrong"], rec["rows_missing"], rec["rows_extra"]) == (0, 0, 0)
    assert (tmp_path / "runs.csv").read_text().count("\n") == 2


def test_rule_scenario_noop(eval_log, tmp_path, monkeypatch):
    """A declined repair leaves the issue: detected, but not resolved."""
    monkeypatch.setattr(es, "suggest_repair",
                        lambda key, row, db: {"kind": "noop", "rationale": "unsure"})
    [rec] = es.evaluate(_args(eval_log, "missing_attribute_value/easy"), tmp_path)
    assert rec["detected"] and not rec["strict"]
    assert rec["noop_reasons"] == "unsure"
    assert rec["cells_wrong"] == ""  # nothing applied, nothing scored


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
    assert rec["detected"] and rec["strict"]
    assert (rec["false_positives"], rec["distractors"], rec["invalid_outputs"]) == (0, 5, 0)


def test_llm_scenario_no_repair_no_credit(eval_log, tmp_path, monkeypatch):
    """A flagged candidate whose repair is declined is not resolved."""
    def judge(key, rows, db, **kw):
        return [(r, DetectionResult(flagged=r["ocel_id"] == "o-990001", rationale="",
                                    confidence=1.0, suggested_value="orders"))
                for r in rows]
    monkeypatch.setattr(es, "detect_all_with_llm", judge)
    monkeypatch.setattr(es, "suggest_repair",
                        lambda key, row, db: {"kind": "noop", "rationale": "unsure"})
    [rec] = es.evaluate(_args(eval_log, "incorrect_object_type/easy"), tmp_path)
    assert rec["detected"] and rec["repairs_applied"] == 0
    assert not rec["strict"]


def test_llm_repair_is_separate(eval_log, tmp_path, monkeypatch):
    """A flagged candidate is repaired by its own call, without the detection's answer."""
    seen = {}
    def judge(key, rows, db, **kw):
        return [(r, DetectionResult(flagged=r["ocel_id"] == "o-990001", rationale="r",
                                    confidence=0.9)) for r in rows]
    def repair(key, row, db):
        seen.update(row)
        return {"kind": "noop", "rationale": ""}
    monkeypatch.setattr(es, "detect_all_with_llm", judge)
    monkeypatch.setattr(es, "suggest_repair", repair)
    [rec] = es.evaluate(_args(eval_log, "incorrect_object_type/easy"), tmp_path)
    assert rec["detected"] and seen["ocel_id"] == "o-990001"
    assert not any(k.startswith("_detected") for k in seen)


def test_new_violations_listed(eval_log, tmp_path, monkeypatch):
    """Filling the missing weight with text adds a datatype violation: listed
    in new_violations, and the wrong cell is counted."""
    monkeypatch.setattr(es, "suggest_repair", lambda key, row, db: {"kind": "update"})
    monkeypatch.setattr(es, "apply_repair", _apply_sql(
        "UPDATE object_Products SET weight = 'heavy' "
        "WHERE ocel_id = 'Echo' AND ocel_changed_field IS NULL", ()))
    [rec] = es.evaluate(_args(eval_log, "missing_attribute_value/easy"), tmp_path)
    assert rec["repairs_applied"] == 1 and not rec["strict"]
    assert rec["new_violations"] == "incorrect_attribute_datatype+1"
    assert rec["cells_wrong"] == 1


def test_timeout_counted(eval_log, tmp_path, monkeypatch):
    def repair(key, row, db):
        raise TimeoutError("timed out")
    monkeypatch.setattr(es, "suggest_repair", repair)
    [rec] = es.evaluate(_args(eval_log, "missing_attribute_value/easy"), tmp_path)
    assert rec["timeouts"] == 1 and rec["error"] == ""
    assert rec["action_kinds"] == "timeout"


def test_failed_detection_is_an_error(eval_log, tmp_path, monkeypatch):
    """A lost connection during LLM detection must not score as a miss."""
    def judge(key, rows, db, **kw):
        return [(r, DetectionResult(flagged=False, confidence=0.0, suggested_value=None,
                                    rationale="detection failed: [Errno 61] Connection refused"))
                for r in rows]
    monkeypatch.setattr(es, "detect_all_with_llm", judge)
    [rec] = es.evaluate(_args(eval_log, "incorrect_object_type/easy"), tmp_path)
    assert "LLM detections failed" in rec["error"]
    assert rec["false_positives"] == ""


def test_run_repeated_after_server_returns(eval_log, tmp_path, monkeypatch):
    """A run that failed while the server was down is repeated once it is back."""
    calls = []
    def repair(key, row, db):
        calls.append(key)
        if len(calls) == 1:
            raise ConnectionRefusedError("[Errno 61] Connection refused")
        return {"kind": "noop", "rationale": "unsure"}
    up = iter([True, False, True, True, True])
    monkeypatch.setattr(es, "llm_ready", lambda: (next(up), []))
    monkeypatch.setattr(es, "suggest_repair", repair)
    args = _args(eval_log, "missing_attribute_value/easy")
    args.wait_minutes = 1
    [rec] = es.evaluate(args, tmp_path)
    assert rec["error"] == "" and len(calls) == 2


def test_stops_when_server_stays_away(eval_log, tmp_path, monkeypatch):
    monkeypatch.setattr(es, "llm_ready", lambda: (False, []))
    monkeypatch.setattr(es.time, "sleep", lambda s: None)
    args = _args(eval_log, "missing_attribute_value/easy")
    args.wait_minutes = 0.0001
    assert es.evaluate(args, tmp_path) == [] and args.stopped_early


def test_error_size_recorded(eval_log, tmp_path, monkeypatch):
    """A timestamp filled in 2 h off is outside the 1 h tolerance: not strict,
    and the error is kept in minutes."""
    def apply(db, action, dry_run=True):
        with sqlite3.connect(db) as conn, sqlite3.connect(eval_log) as clean:
            (oid,), = conn.execute("SELECT ocel_id FROM event_PlaceOrder WHERE ocel_time IS NULL")
            (t,), = clean.execute("SELECT ocel_time FROM event_PlaceOrder WHERE ocel_id = ?", (oid,))
            conn.execute("UPDATE event_PlaceOrder SET ocel_time = datetime(?, '+2 hours') "
                         "WHERE ocel_id = ?", (t, oid))
    monkeypatch.setattr(es, "suggest_repair", lambda key, row, db: {"kind": "update"})
    monkeypatch.setattr(es, "apply_repair", apply)
    [rec] = es.evaluate(_args(eval_log, "missing_event_timestamp/easy"), tmp_path)
    assert not rec["strict"] and rec["time_error_min"] == 120
    assert rec["cells_wrong"] == 0


def test_invalid_output_left_out(eval_log, tmp_path, monkeypatch):
    """Unparsable LLM output is neither a miss nor a clean pass."""
    def judge(key, rows, db, **kw):
        return [(r, DetectionResult(flagged=False, confidence=0.0, suggested_value=None,
                                    rationale="LLM output invalid: bad json" if i < 2 else ""))
                for i, r in enumerate(rows)]
    monkeypatch.setattr(es, "detect_all_with_llm", judge)
    [rec] = es.evaluate(_args(eval_log, "incorrect_object_type/easy"), tmp_path)
    assert rec["detected"] == "invalid" and rec["error"] == ""
    assert (rec["invalid_outputs"], rec["distractors"], rec["false_positives"]) == (2, 4, 0)


def test_fresh_distractors_per_run(eval_log, tmp_path, monkeypatch):
    seen = []
    def judge(key, rows, db, **kw):
        seen.append({r["ocel_id"] for r in rows[1:]})
        return [(r, DetectionResult(flagged=False, rationale="", confidence=1.0,
                                    suggested_value=None)) for r in rows]
    monkeypatch.setattr(es, "detect_all_with_llm", judge)
    args = _args(eval_log, "incorrect_object_type/hard")
    args.runs = 2
    es.evaluate(args, tmp_path)
    assert len(seen) == 2 and seen[0] != seen[1]


@pytest.mark.parametrize("names,strict", [(["price"], True), (["Price", "discount"], False)])
def test_schema_precision_recall(eval_log, tmp_path, monkeypatch, names, strict):
    """Every proposed name counts towards precision and is applied; strict
    needs exactly the clean columns back."""
    def judge(key, rows, db, **kw):
        suggest = lambda ns: [{"name": n, "affinity": "REAL", "confidence": 0.9} for n in ns]
        return [(r, DetectionResult(flagged=True, rationale="", confidence=0.9,
                                    suggested_value=suggest(names if i == 0 else ["x"] * (i == 1))))
                for i, r in enumerate(rows)]
    monkeypatch.setattr(es, "detect_all_with_llm", judge)
    [rec] = es.evaluate(_args(eval_log, "missing_object_attribute/easy"), tmp_path)
    assert rec["detected"] and rec["names_correct"] == 1
    assert rec["names_proposed"] == len(names) + 1
    assert rec["repairs_applied"] == len(names) and rec["strict"] is strict


def test_rule_repair_source(eval_log, tmp_path, monkeypatch):
    """A self-loop is removed by rule; the LLM is never called."""
    def fail(*a, **kw):
        raise AssertionError("LLM must not be called")
    monkeypatch.setattr("src.llm.resolution._call_task_or_legacy", fail)
    [rec] = es.evaluate(_args(eval_log, "o2o_self_loop/easy"), tmp_path)
    assert rec["strict"] and rec["repair_sources"] == "rule"


def test_lookup_baseline(eval_log, tmp_path):
    """The derived event attributes are recovered exactly by lookup."""
    args = _args(eval_log, None)
    args.baseline = "lookup"
    recs = es.evaluate(args, tmp_path)
    assert {r["issue_key"] for r in recs} == es.LOOKUP_KEYS
    assert all(r["strict"] and r["error"] == "" for r in recs), recs
