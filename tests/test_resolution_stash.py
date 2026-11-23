"""suggest_repair on a confirmed LLM flag: a missing-attribute flag carries
its fix; every other flag gets a separate repair call."""

import src.llm.resolution as res


def test_missing_attribute_flag_skips_llm(monkeypatch):
    def no_llm(*a, **kw):
        raise AssertionError("LLM must not be called")
    monkeypatch.setattr(res, "_call_task_or_legacy", no_llm)
    row = {"ocel_type": "orders", "object_type": "orders",
           "_resolved_target_table": "object_Orders",
           "_detected_suggestion": {"name": "price", "affinity": "REAL"},
           "_detected_rationale": "peers have it", "_detected_confidence": 0.8}
    action = res.suggest_repair("missing_object_attribute", row, ":memory:")
    assert action["kind"] == "alter_add_column" and action["source"] == "detection"


def test_other_flags_get_a_repair_call(monkeypatch):
    calls = []
    def repair(task, ctx):
        calls.append(task.issue_key)
        return {"inferred_type": "orders", "rationale": "id shape", "confidence": 0.8}
    monkeypatch.setattr(res, "_call_task_or_legacy", repair)
    monkeypatch.setattr(res, "_connect", lambda p: __import__("sqlite3").connect(":memory:"))
    monkeypatch.setattr(type(res.get_task("incorrect_object_type")), "build_context",
                        lambda self, conn, row, use_hints=True: {})
    row = {"ocel_id": "o-1", "ocel_type": "employees", "_detected_suggestion": None}
    action = res.suggest_repair("incorrect_object_type", row, ":memory:")
    assert calls == ["incorrect_object_type"]
    assert action["kind"] == "update" and action["new_value"] == "orders"
    assert action["source"] == "llm"
