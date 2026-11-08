"""Sweep-coverage display in the Issue Overview (dashboard_render helpers)."""

from src import dashboard_render as dr

PATH = "data/x.sqlite"
LLM_KEYS = {"missing_event_attribute", "incorrect_object_type"}
TYPES = {"event": ["Order", "Ship", "Pay"], "object": ["item", "customer"]}


def _kind_of(key: str) -> str:
    return "event" if key == "missing_event_attribute" else "object"


def _coverage(keys, sweep_result=None, sweep_ran=None, flags=None):
    return dr.build_cell_coverage(
        keys,
        llm_keys=LLM_KEYS,
        kind_of=_kind_of,
        types_by_kind=TYPES,
        sweep_result=sweep_result or {},
        sweep_ran=sweep_ran or set(),
        confirmed=dr.confirmed_by_key_type(flags or {}, PATH),
        sqlite_path=PATH,
    )


def _entry(flagged: int) -> dict:
    return {"flagged_count": flagged, "total_judged": 1, "proposals": []}


def test_rule_only_cell_has_no_coverage():
    assert _coverage(["duplicate_o2o_relations", "o2o_self_loop"]) is None


def test_no_sweep_is_none():
    cov = _coverage(["missing_event_attribute"])
    assert cov["state"] == "none"
    assert (cov["scanned"], cov["total"]) == (0, 3)


def test_partial_then_complete():
    key = "missing_event_attribute"
    result = {(PATH, key, "Order"): _entry(5)}
    cov = _coverage([key], result, {(PATH, key)})
    assert cov["state"] == "partial"
    assert (cov["scanned"], cov["total"]) == (1, 3)

    result[(PATH, key, "Ship")] = _entry(0)
    result[(PATH, key, "Pay")] = _entry(2)
    cov = _coverage([key], result, {(PATH, key)})
    assert cov["state"] == "complete"
    assert cov["scanned"] == cov["total"] == 3


def test_other_files_and_keys_do_not_count():
    result = {
        ("data/other.sqlite", "missing_event_attribute", "Order"): _entry(1),
        (PATH, "incorrect_object_type", "item"): _entry(1),
    }
    assert _coverage(["missing_event_attribute"], result)["state"] == "none"


def test_proposed_and_confirmed_per_type():
    key = "missing_event_attribute"
    flags = {
        (PATH, key, "e1", "a"): {"ocel_type": "Order"},
        (PATH, key, "e2", "b"): {"ocel_type": "Order"},
        (PATH, key, "e3", "c"): {"event_type": "Ship"},
        ("data/other.sqlite", key, "e9", "z"): {"ocel_type": "Order"},
    }
    result = {(PATH, key, "Order"): _entry(5), (PATH, key, "Ship"): _entry(1)}
    cov = _coverage([key], result, {(PATH, key)}, flags)
    by_name = {t["name"]: t for t in cov["types"]}
    assert (by_name["Order"]["proposed"], by_name["Order"]["confirmed"]) == (5, 2)
    assert (by_name["Ship"]["proposed"], by_name["Ship"]["confirmed"]) == (1, 1)
    assert by_name["Pay"]["scanned"] is False


def test_sweep_ran_with_no_types_is_complete():
    key = "missing_event_attribute"
    cov = dr.build_cell_coverage(
        [key],
        llm_keys=LLM_KEYS,
        kind_of=_kind_of,
        types_by_kind={"event": [], "object": []},
        sweep_result={},
        sweep_ran={(PATH, key)},
        confirmed={},
        sqlite_path=PATH,
    )
    assert cov["state"] == "complete"


def _table(cov, kind="count", count=3, c_idx=0, n_cols=1):
    spec = {
        "kind": kind,
        "count": count,
        "row_label": "Missing Data",
        "col_label": "Event Attribute",
        "issue_key": "missing_event_attribute",
        "coverage": cov,
    }
    return dr.render_overview_table_html(
        rows=["Missing Data"],
        col_groups=[("Events", ["Event Attribute"] * n_cols)],
        cols_flat=["Event Attribute"] * n_cols,
        cell_meta={f"0:{i}": spec for i in range(n_cols)},
        selected_issue_key=None,
    )


def test_partial_cell_renders_sublabel_and_popover():
    key = "missing_event_attribute"
    result = {(PATH, key, "Order"): _entry(5)}
    cov = _coverage([key], result, {(PATH, key)})
    html = _table(cov)
    assert "1/3 types" in html
    assert "dashed" in html  # partial pill style
    assert "1 of 3 event types · 5 proposed · 0 confirmed" in html
    assert "○ Ship" not in html and "✓ Order" not in html
    assert "lower bound" not in html


def test_pending_cell_keeps_question_mark_and_lists_unscanned():
    cov = _coverage(["missing_event_attribute"])
    html = _table(cov, kind="pending", count=None)
    assert ">?<" in html
    assert "Not scanned yet · 0 of 3 event types" in html


def test_rule_cell_has_no_popover():
    html = _table(None)
    assert 'class="oh-cov' not in html
    assert "types</span>" not in html


def test_last_columns_open_leftwards():
    cov = _coverage(["missing_event_attribute"])
    html = _table(cov, kind="pending", count=None, n_cols=4)
    assert html.count('class="oh-cov oh-right"') == 3  # last three of four columns


def test_summary_line():
    key = "missing_event_attribute"
    cov = _coverage([key], {(PATH, key, "Order"): _entry(1)}, {(PATH, key)})
    meta = {"0:0": {"coverage": cov}, "0:1": {"coverage": None}}
    html = dr.coverage_summary_html(meta)
    assert "1 of 3" in html and "width:33%" in html
    assert dr.coverage_summary_html({"0:0": {"coverage": None}}) == ""


def test_drill_lists_split_scanned_and_not_scanned():
    key = "missing_event_attribute"
    flags = {(PATH, key, "e1", "a"): {"ocel_type": "Pay"}}
    result = {(PATH, key, "Pay"): _entry(4)}
    cov = _coverage([key], result, {(PATH, key)}, flags)
    cov["types"][1]["name"] = "<i>Ship</i>"
    html = dr.coverage_table_html(cov)
    assert "Coverage: 1 of 3 event types" in html
    assert "Scanned (1)" in html and "Not scanned (2)" in html
    assert "Pay (4 / 1)" in html
    assert html.index("Scanned (1)") < html.index("Pay") < html.index("Not scanned (2)")
    assert "&lt;i&gt;Ship" in html and "<i>" not in html
    assert "<table" not in html


def test_drill_lists_omit_empty_areas_and_fill_width():
    types = [
        {"name": f"T{i}", "kind": "event", "key": "k", "scanned": False,
         "proposed": 0, "confirmed": 0}
        for i in range(6)
    ]
    cov = {"state": "none", "scanned": 0, "total": 6, "noun": "event", "types": types}
    html = dr.coverage_table_html(cov)
    assert "Scanned (" not in html and "Not scanned (6)" in html
    assert "width:100%" in html and "repeat(auto-fill, minmax(6ch, 1fr))" in html
    # column width follows the longest item
    types[0]["name"] = "A very long type name"
    assert "minmax(25ch, 1fr)" in dr.coverage_table_html(cov)
