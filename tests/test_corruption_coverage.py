"""Coverage tests for the corruption injectors.

Each level of `corrupt_database` should produce, at minimum, one rule-detectable
row per detector on top of the clean-DB baseline.  Values are asserted as
``>=`` because injecting a NULL object type causes a cascade in the two
dangling-relationship detectors (every relationship referencing the retyped
object also becomes dangling), and that cascade grows/shrinks with the
dataset. The `missing_object` injectors also add rows that trip
``dangling_e2o_relationship`` — this is intentional: the same violation
surfaces in both detectors and the dashboard routes it to the appropriate
grid cell. The `missing_event` injectors are analogous: they trip
both ``missing_event`` and ``dangling_e2o_relationship`` (from opposite
sides of the same E2O row).
"""

from __future__ import annotations

import shutil
import sqlite3

import pytest

from src.order_management_corruption import DEFAULT_CLEAN_PATH, corrupt_database
from src.order_management_corruption._common import _remove_all_primary_keys
from src.detection.error_detection import detect_all
from src.evaluation.build_eval_log import build_eval_log
from src.evaluation.scenarios import SCENARIOS, SCHEMA_KEYS

RULE_DETECTORS = (
    "missing_object_type",
    "missing_attribute_value",
    "missing_event",
    "missing_event_type",
    "missing_event_timestamp",
    "missing_object",
    "duplicate_objects_on_ids",
    "duplicate_objects_on_attributes",
    "incorrect_attribute_datatype",
    "dangling_o2o_relationship",
    "dangling_e2o_relationship",
    "duplicate_o2o_relations",
    "o2o_self_loop",
    "duplicate_e2o_relations",
)
# `missing_event_attribute_value`, `incorrect_event_attribute_datatype` and
# the duplicate-event detectors aren't in RULE_DETECTORS: the baseline log has
# no event attributes, so `corrupt_database` doesn't stage them. They are
# covered per scenario on the evaluation log below.

# Counts on the pristine clean DB.  Only `duplicate_objects_on_attributes` is
# non-zero — every item of a product inherits its (weight, price), so items
# legitimately share attribute fingerprints.
CLEAN_BASELINE = {
    "missing_object_type":               0,
    "missing_attribute_value":           0,
    "missing_event":                     0,
    "missing_event_type":                0,
    "missing_event_timestamp":           0,
    "missing_event_attribute_value":     0,
    "missing_object":                    0,
    "duplicate_objects_on_ids":          0,
    "duplicate_objects_on_attributes":   123,
    "duplicate_events_on_ids":           0,
    "duplicate_events_on_attributes":    0,
    "incorrect_attribute_datatype":      0,
    "incorrect_event_attribute_datatype": 0,
    "dangling_o2o_relationship":         0,
    "dangling_e2o_relationship":         0,
    "duplicate_o2o_relations":           0,
    "o2o_self_loop":                     0,
    "duplicate_e2o_relations":           0,
}


def _counts(path: str) -> dict[str, int]:
    return {k: v.height for k, v in detect_all(path).items()}


@pytest.fixture(scope="module")
def clean_counts() -> dict[str, int]:
    return _counts(DEFAULT_CLEAN_PATH)


@pytest.fixture(scope="module")
def dirty_paths(tmp_path_factory) -> dict[str, str]:
    """Corrupt the clean DB once per level, share across tests."""
    out: dict[str, str] = {}
    for level in ("easy", "hard", "all"):
        dst = tmp_path_factory.mktemp(level) / f"{level}.sqlite"
        out[level] = corrupt_database(DEFAULT_CLEAN_PATH, str(dst), level=level)
    return out


def test_clean_baseline(clean_counts):
    """Guard the baseline so a change in the source DB is caught here."""
    assert clean_counts == CLEAN_BASELINE


@pytest.mark.parametrize("level", ["easy", "hard"])
@pytest.mark.parametrize("detector", RULE_DETECTORS)
def test_tier_triggers_each_detector(dirty_paths, level, detector):
    """Every tier fires every rule-based detector at least once above baseline."""
    counts = _counts(dirty_paths[level])
    assert counts[detector] >= CLEAN_BASELINE[detector] + 1, (
        f"level={level} detector={detector}: got {counts[detector]}, "
        f"expected >= {CLEAN_BASELINE[detector] + 1}. Full counts: {counts}"
    )


@pytest.mark.parametrize("detector", RULE_DETECTORS)
def test_all_level_triggers_each_detector(dirty_paths, detector):
    """`level='all'` produces at least 2 rows per detector above baseline."""
    counts = _counts(dirty_paths["all"])
    # duplicate_objects_on_ids from `duplicate_objects_on_ids_triple_conflicting_type`
    # counts as a single group of size 3, so the detector row-count
    # contribution is 1 (one group), not 3 — total >= 2 groups (one per flavor).
    expected_new = 2
    assert counts[detector] >= CLEAN_BASELINE[detector] + expected_new, (
        f"detector={detector}: got {counts[detector]}, "
        f"expected >= {CLEAN_BASELINE[detector] + expected_new}. Full counts: {counts}"
    )


# ── Per-scenario isolation on the evaluation log ────────────────────────────

# Detectors that legitimately fire alongside an issue: the same broken
# E2O/O2O row is a missing event/object *and* a dangling relation.
EXPECTED_COLLATERAL = {
    "missing_event": {"dangling_e2o_relationship"},
    "missing_object": {"dangling_e2o_relationship", "dangling_o2o_relationship"},
    "dangling_e2o_relationship": {"missing_event", "missing_object"},
    # A confirm order relabelled as pay order lacks the pay order's `amount`.
    "incorrect_event_type": {"missing_event_attribute_value"},
}


@pytest.fixture(scope="module")
def eval_log(tmp_path_factory) -> str:
    return build_eval_log(dst=str(tmp_path_factory.mktemp("eval") / "eval.sqlite"))


@pytest.fixture(scope="module")
def eval_counts(eval_log) -> dict[str, int]:
    return _counts(eval_log)


def test_eval_log_baseline(eval_counts):
    """The added event attributes keep the clean log (almost) clean. Two
    pick-item pairs share employee and timestamp, a real coincidence in
    the source log."""
    assert {k: v for k, v in eval_counts.items() if v} == {
        "duplicate_objects_on_attributes": 123,
        "duplicate_events_on_attributes": 2,
    }


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s.name)
def test_scenario_is_isolated(sc, eval_log, eval_counts, tmp_path):
    """Each scenario injects something, fires its own rule detector (rule
    cells) and no other detector beyond the documented cascades."""
    db = tmp_path / "run.sqlite"
    shutil.copy2(eval_log, db)
    with sqlite3.connect(db) as conn:
        _remove_all_primary_keys(conn)
        assert sc.inject(conn) is not None
        conn.commit()
    counts = _counts(str(db))
    fired = {k for k, n in counts.items() if n > eval_counts[k]}
    if sc.detection == "rule":
        assert sc.issue_key in fired
    else:
        assert sc.issue_key not in counts  # LLM-only key, no rule detector
    if sc.issue_key not in SCHEMA_KEYS:
        assert fired - {sc.issue_key} <= EXPECTED_COLLATERAL.get(sc.issue_key, set())
