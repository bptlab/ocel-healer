"""suggest_repair on a confirmed LLM flag reuses the stashed detection."""

import src.llm.resolution as res


def test_stashed_verdict_skips_llm(monkeypatch):
    def no_llm(*a, **kw):
        raise AssertionError("LLM must not be called")
    monkeypatch.setattr(res, "_call_task_or_legacy", no_llm)
    row = {"ocel_id": "o-1", "ocel_type": "employees",
           "_detected_suggestion": "orders", "_detected_rationale": "id shape",
           "_detected_confidence": 0.8}
    action = res.suggest_repair("incorrect_object_type", row, ":memory:")
    assert action["kind"] == "update"
    assert action["new_value"] == "orders"
    assert action["confidence"] == 0.8
