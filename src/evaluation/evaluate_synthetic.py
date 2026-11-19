"""Per-issue synthetic evaluation: inject one issue, detect it, resolve it.

For every scenario in `scenarios.py` (one per overview issue key and
difficulty) and every run:

  1. Copy the clean evaluation log (`build_eval_log.py`) and inject the issue.
  2. Detect it.
     * Rule cells: rows the detector reports on top of the clean log.
     * LLM cells: the LLM judges the injected candidate plus `--distractors`
       clean candidates of the same type, drawn afresh for every run, as
       the dashboard sweep would. Flagged distractors are false alarms.
       Invalid LLM output is counted on its own, never as "not flagged".
  3. Resolve every detected row the way the dashboard does
     (`suggest_repair` → `apply_repair`). Detection and repair are separate
     LLM calls in every cell: an LLM flag only says the row is wrong, and
     the fix is asked for afterwards like for a rule-detected row. Only a
     missing attribute's flag names the fix itself (the attribute). A repair that
     leaves a missing value behind (an emptied datatype, a reinserted
     element) gets one follow-up repair of it (`FOLLOW_UPS`). Each repair
     records who decided it (`repair_sources`): a rule, the LLM, or the
     LLM's missing-attribute flag.
  4. Score the repaired log against the clean log. Needs an applied repair.
     * strict: the repaired log equals the clean log (numbers within 10 %,
       timestamps within `--time-tolerance-hours`).
     * error size: the largest timestamp error in minutes, the largest
       relative numeric error, the number of other wrong cells, and rows
       missing or extra — so any other cutoff can be applied afterwards.
     For schema issues (`missing_*_attribute`) the LLM proposes attribute
     names: recall is whether the dropped name is among them, precision is
     correct names over all names proposed (target and clean types), and
     strict means the table's columns equal the clean ones after applying
     every proposal (the values can't be recovered by construction).

Each scenario corrupts one fixed record; the runs repeat it and vary only in
the LLM's answers (sampled with a per-run seed) and the distractors. The
intervals in the report therefore describe how stable the answers are, not
how the method generalises over records. Examination hints are not used.

Usage:
    python -m src.evaluation.evaluate_synthetic --model qwen3:8b [--runs 10]
        [--only key[/difficulty],...] [--distractors 20] [--baseline lookup]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import platform
import random
import re
import shutil
import sqlite3
import statistics
import subprocess
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.detection.error_detection import (
    _event_type_tables,
    _object_type_tables,
    detect_all,
)
from src.evaluation.baselines import LOOKUP_KEYS, lookup_repair
from src.evaluation.build_eval_log import build_eval_log
from src.evaluation.scenarios import SCENARIOS, SCHEMA_KEYS, Scenario
from src.llm.actions import apply_repair
from src.llm.candidate_sources import candidate_kind, candidate_rows
from src.llm.client import (
    MIN_CONFIDENCE,
    TEMPERATURE,
    LLMOutputInvalid,
    llm_ready,
    server_info,
    set_active_model,
    set_seed,
    set_timeout,
)
from src.llm.resolution import detect_all_with_llm, suggest_repair
from src.order_management_corruption._common import _remove_all_primary_keys

NUMERIC_TOLERANCE = 0.10
MAX_REPAIRS = 10  # repair calls per run; one injection can surface as several rows

# A repair can leave a missing value behind: emptying an unusable value
# (e.g. 'unknown' in a REAL column) or reinserting an element whose
# attributes weren't all inferred. Fix it the way a dashboard user would next.
FOLLOW_UPS = {
    "incorrect_attribute_datatype": "missing_attribute_value",
    "incorrect_event_attribute_datatype": "missing_event_attribute_value",
    "missing_event": "missing_event_attribute_value",
    "missing_object": "missing_attribute_value",
}

CSV_FIELDS = [
    "issue_key", "cell", "difficulty", "detection", "run",
    "detected", "issue_rows", "false_positives", "distractors", "invalid_outputs",
    "sweep_size", "names_proposed", "names_correct", "collateral",
    "repairs_attempted", "repairs_applied", "repair_sources", "action_kinds",
    "noop_reasons", "new_violations", "timeouts", "strict", "time_error_min",
    "num_rel_error", "cells_wrong", "rows_missing", "rows_extra", "error", "seconds",
]


class LLMFailure(RuntimeError):
    """An LLM call failed (connection, timeout): there is no verdict to score."""


def _timed_out(text: Any) -> bool:
    return "timed out" in str(text).lower()


def _invalid(verdict: Any) -> bool:
    """`detect_with_llm` turns unparsable output into an unflagged verdict;
    it is neither a hit nor a clean pass."""
    return str(verdict.rationale).startswith("LLM output invalid")


# --- log snapshots ------------------------------------------------------------

Snapshot = dict[str, tuple[tuple[str, ...], Counter]]


def snapshot(db: str | Path) -> Snapshot:
    """Every table as (column names, multiset of rows)."""
    with sqlite3.connect(db) as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )]
        return {
            t: (
                tuple(r[1] for r in conn.execute(f"PRAGMA table_info('{t}')")),
                Counter(conn.execute(f'SELECT * FROM "{t}"').fetchall()),
            )
            for t in tables
        }


def _as_time(v: Any) -> datetime | None:
    if not isinstance(v, str) or not re.match(r"\d{4}-\d{2}-\d{2}", v.strip()):
        return None
    try:
        return datetime.fromisoformat(v.strip().replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _rel_error(clean: float, got: float) -> float:
    return abs(clean - got) / abs(clean) if clean else abs(got)


def values_close(a: Any, b: Any, time_tol: timedelta) -> bool:
    if a == b:
        return True
    if _is_num(a) and _is_num(b):
        return _rel_error(a, b) <= NUMERIC_TOLERANCE
    ta, tb = _as_time(a), _as_time(b)
    if ta and tb:
        return abs(ta - tb) <= time_tol
    return False


def compare_to_clean(clean: Snapshot, repaired: Snapshot, time_tol: timedelta) -> dict:
    """How far `repaired` is from `clean`. Rows that differ are paired on
    their first column (the id) with the closest candidate; per paired cell
    the error is kept (minutes for timestamps, relative for numbers, a count
    for anything else). `strict` holds when the tables and columns match,
    every row is paired and every cell is within tolerance."""
    out: dict[str, Any] = {"strict": set(clean) == set(repaired), "time_error_min": None,
                           "num_rel_error": None, "cells_wrong": 0,
                           "rows_missing": 0, "rows_extra": 0}

    def cell(a: Any, b: Any) -> None:
        if a == b:
            return
        ta, tb = _as_time(a), _as_time(b)
        if _is_num(a) and _is_num(b):
            err = _rel_error(a, b)
            out["num_rel_error"] = max(out["num_rel_error"] or 0.0, err)
            out["strict"] &= err <= NUMERIC_TOLERANCE
        elif ta and tb:
            err = abs(ta - tb).total_seconds() / 60
            out["time_error_min"] = max(out["time_error_min"] or 0.0, err)
            out["strict"] &= err <= time_tol.total_seconds() / 60
        else:
            out["cells_wrong"] += 1

    for table, (cols, rows) in clean.items():
        if table not in repaired:
            out["rows_missing"] += sum(rows.values())
            continue
        cols2, rows2 = repaired[table]
        if cols != cols2:
            out["strict"] = False  # schema changed: cells can't be compared
            continue
        missing = list((rows - rows2).elements())
        extra = list((rows2 - rows).elements())
        for m in missing:
            same_id = [i for i, e in enumerate(extra) if e[0] == m[0]]
            if not same_id:
                out["rows_missing"] += 1
                continue
            best = min(same_id, key=lambda i: sum(
                not values_close(a, b, time_tol) for a, b in zip(m, extra[i])))
            for a, b in zip(m, extra.pop(best)):
                cell(a, b)
        out["rows_extra"] += len(extra)
    for table in set(repaired) - set(clean):
        out["rows_extra"] += sum(repaired[table][1].values())
    if out["rows_missing"] or out["rows_extra"] or out["cells_wrong"]:
        out["strict"] = False
    return out


def equals_clean(clean: Snapshot, repaired: Snapshot, time_tol: timedelta) -> bool:
    """True if `repaired` matches `clean` row for row (with tolerance)."""
    return compare_to_clean(clean, repaired, time_tol)["strict"]


def _columns(db: str, table: str) -> list[str]:
    with sqlite3.connect(db) as conn:
        return [r[1] for r in conn.execute(f"PRAGMA table_info('{table}')")]


def content_hash(db: str | Path) -> str:
    """sha256 over the SQL dump: equal for logs with equal content, whatever
    the file's page layout."""
    with sqlite3.connect(db) as conn:
        h = hashlib.sha256()
        for line in conn.iterdump():
            h.update(line.encode("utf-8"))
        return h.hexdigest()


# --- detector output -----------------------------------------------------------

def _row_key(row: dict) -> tuple:
    return tuple(sorted((k, str(v)) for k, v in row.items()))


def _stash(verdict: Any, suggestion: Any) -> dict:
    """The detection fields the dashboard keeps on a confirmed LLM flag."""
    return {
        "_detected_suggestion": suggestion,
        "_detected_rationale": verdict.rationale,
        "_detected_confidence": verdict.confidence,
    }


def detector_rows(db: str) -> dict[str, dict[tuple, dict]]:
    """Every rule detector's rows, keyed for multiset-free diffing."""
    return {
        key: {_row_key(r): r for r in df.to_dicts()}
        for key, df in detect_all(db).items()
    }


def new_rows(rows: dict[str, dict], clean_rows: dict[str, dict], key: str) -> list[dict]:
    return [r for k, r in rows[key].items() if k not in clean_rows[key]]


def counts(rows: dict[str, dict]) -> dict[str, int]:
    return {k: len(v) for k, v in rows.items()}


def _suggested_names(verdict: Any) -> list[str]:
    if not verdict.flagged:
        return []
    return [str(s["name"]).strip() for s in verdict.suggested_value or []
            if isinstance(s, dict) and s.get("name")]


# --- one run ---------------------------------------------------------------------

class Run:
    def __init__(self, sc: Scenario, run: int, args, clean_snap: Snapshot,
                 clean_rows: dict[str, dict]):
        self.sc, self.run, self.args = sc, run, args
        self.clean_snap, self.clean_rows = clean_snap, clean_rows
        self.rec: dict[str, Any] = {
            "issue_key": sc.issue_key, "cell": sc.cell, "difficulty": sc.difficulty,
            "detection": sc.detection, "run": run, "detected": False,
            "issue_rows": 0, "false_positives": "", "distractors": "",
            "invalid_outputs": "", "sweep_size": "", "names_proposed": "",
            "names_correct": "", "collateral": "", "repairs_attempted": 0,
            "repairs_applied": 0, "repair_sources": "", "action_kinds": "",
            "noop_reasons": "", "new_violations": "", "timeouts": 0, "strict": False,
            "time_error_min": "", "num_rel_error": "", "cells_wrong": "",
            "rows_missing": "", "rows_extra": "", "error": "",
        }
        self.kinds: list[str] = []
        self.sources: list[str] = []
        self.noops: list[str] = []
        self.verdicts: list[dict] = []  # every LLM detection verdict, for verdicts.jsonl
        self.actions: list[dict] = []   # every suggested repair, for actions.jsonl

    def execute(self, workdir: Path) -> dict:
        t0 = time.time()
        set_seed(self.args.seed * 1000 + self.run)
        db = str(workdir / "run.sqlite")
        shutil.copy2(self.args.eval_log, db)
        with sqlite3.connect(db) as conn:
            _remove_all_primary_keys(conn)
            injected = self.sc.inject(conn)
            conn.commit()
        if injected is None:
            raise RuntimeError(f"{self.sc.name}: injector found no target row")
        corrupted = detector_rows(db)
        self.rec["collateral"] = ";".join(
            f"{k}+{n - len(self.clean_rows[k])}"
            for k, n in counts(corrupted).items()
            if k != self.sc.issue_key and n > len(self.clean_rows[k])
        )
        # Only a failing LLM call is a failed run; anything else is a bug in
        # the evaluation and stops it.
        try:
            if self.sc.detection == "rule":
                self._rule(db, corrupted)
            else:
                self._llm(db, corrupted, injected)
        except (OSError, LLMOutputInvalid, LLMFailure) as e:
            self.rec["error"] = f"{type(e).__name__}: {e}"
            self.rec["timeouts"] += _timed_out(e)
        self.rec["action_kinds"] = ";".join(self.kinds)
        self.rec["repair_sources"] = ";".join(self.sources)
        self.rec["noop_reasons"] = " | ".join(self.noops)[:500]
        self.rec["seconds"] = round(time.time() - t0, 1)
        return self.rec

    # -- repair plumbing shared by both detection modes --

    def _repair(self, row: dict, db: str, key: str | None = None) -> bool:
        """Ask for and apply one repair. Returns True if something was applied.
        `key` overrides the scenario's issue key for follow-up repairs."""
        issue_key = key or self.sc.issue_key
        self.rec["repairs_attempted"] += 1
        prefix = "followup:" if key else ""
        try:
            if getattr(self.args, "baseline", None) == "lookup":
                action = lookup_repair(issue_key, row, db)
            else:
                action = suggest_repair(issue_key, row, db)
        except TimeoutError:
            self.rec["timeouts"] += 1
            self.kinds.append(f"{prefix}timeout")
            self.sources.append("llm")
            return False
        self.kinds.append(prefix + action["kind"])
        self.sources.append(action.get("source", "llm"))
        self.actions.append({"issue_key": issue_key, "followup": bool(key), **action})
        if action["kind"] == "noop":
            self.noops.append(str(action.get("rationale", ""))[:150])
            return False
        try:
            apply_repair(db, action, dry_run=False)
        except Exception as e:  # noqa: BLE001 — a rejected action is a failed repair, not a crash
            self.noops.append(f"apply failed: {e}"[:150])
            return False
        self.rec["repairs_applied"] += 1
        return True

    def _score(self, db: str, corrupted: dict[str, dict]) -> None:
        """Compare the repaired log with the clean one. Nothing is scored
        unless a repair was applied: the corrupted log's distance to the
        clean one says nothing about a repair."""
        after = counts(detector_rows(db))
        grew = {k: after[k] - len(v) for k, v in corrupted.items() if after[k] > len(v)}
        self.rec["new_violations"] = ";".join(f"{k}+{n}" for k, n in grew.items())
        if self.rec["repairs_applied"] > 0:
            self.rec.update(compare_to_clean(
                self.clean_snap, snapshot(db), self.args.time_tolerance
            ))

    # -- rule cells --

    def _rule(self, db: str, corrupted: dict[str, dict]) -> None:
        key = self.sc.issue_key
        rows = new_rows(corrupted, self.clean_rows, key)
        self.rec["issue_rows"] = len(rows)
        self.rec["detected"] = bool(rows)
        if not rows:
            return
        tried: set[tuple] = set()
        for _ in range(MAX_REPAIRS):
            pending = [
                r for r in new_rows(detector_rows(db), self.clean_rows, key)
                if _row_key(r) not in tried
            ]
            if not pending:
                break
            tried.add(_row_key(pending[0]))
            self._repair(pending[0], db)
        follow = FOLLOW_UPS.get(key)
        if follow:
            for r in new_rows(detector_rows(db), corrupted, follow)[:MAX_REPAIRS]:
                self._repair(r, db, key=follow)
        self._score(db, corrupted)

    # -- LLM cells --

    def _llm(self, db: str, corrupted: dict[str, dict], injected: Any) -> None:
        sc = self.sc
        target, distractors = self._candidates(db, injected)
        self.rec["issue_rows"] = 1
        verdicts = detect_all_with_llm(sc.issue_key, [target, *distractors], db)
        self.rec["timeouts"] += sum(1 for _, v in verdicts if _timed_out(v.rationale))
        for i, (cand, v) in enumerate(verdicts):
            self.verdicts.append({
                "injected": i == 0,
                "candidate": {k: x for k, x in cand.items() if not k.startswith("_")},
                "flagged": v.flagged, "invalid": _invalid(v), "confidence": v.confidence,
                "rationale": v.rationale, "suggestion": v.suggested_value,
            })
        # A failed LLM call is not a "not flagged" verdict: scoring it would
        # count a lost connection as a miss and shrink the false alarms.
        failed = [v.rationale for _, v in verdicts if v.rationale.startswith("detection failed")]
        if failed:
            raise LLMFailure(f"{len(failed)}/{len(verdicts)} LLM detections failed: {failed[0]}")
        (_, tv), others = verdicts[0], [v for _, v in verdicts[1:]]
        valid = [v for v in others if not _invalid(v)]
        self.rec["invalid_outputs"] = len(others) - len(valid) + _invalid(tv)
        self.rec["distractors"] = len(valid)
        self.rec["false_positives"] = sum(1 for v in valid if v.flagged)
        if _invalid(tv):
            self.rec["detected"] = "invalid"
            return

        row = {k: v for k, v in target.items() if not k.startswith("_")}
        if sc.issue_key in SCHEMA_KEYS:
            self._schema(db, target, row, tv, valid)
            return

        self.rec["detected"] = tv.flagged
        if not tv.flagged:
            return
        # The flag only says the row is wrong; the fix is a separate call,
        # as after confirming a flag in the dashboard.
        self._repair(row, db)
        self._score(db, corrupted)

    def _schema(self, db: str, target: dict, row: dict, tv: Any, valid: list) -> None:
        """Missing attribute: the dropped column must be among the proposed
        names (exact, ignoring case). Every proposal is applied; strict means
        the table ends up with exactly the clean columns."""
        dropped = self.sc.attribute.lower()
        proposed = _suggested_names(tv)
        self.rec["names_proposed"] = len(proposed) + sum(len(_suggested_names(v)) for v in valid)
        self.rec["names_correct"] = int(any(n.lower() == dropped for n in proposed))
        self.rec["detected"] = bool(self.rec["names_correct"])
        if not proposed:
            return
        table = target["_resolved_target_table"]
        for s in tv.suggested_value:
            if isinstance(s, dict) and s.get("name"):
                self._repair({**row, **_stash(tv, s), "attribute": s["name"],
                              "_resolved_target_table": table}, db)
        clean_cols = {c.lower() for c in self.clean_snap[table][0]}
        self.rec["strict"] = (self.rec["repairs_applied"] > 0
                              and {c.lower() for c in _columns(db, table)} == clean_cols)

    def _injected_candidates(self, db: str, injected: Any) -> list[dict]:
        """Candidates of the injected id that don't exist in the clean log.
        Matching on the id alone is not enough: an object's change rows give
        several candidates with the same id and attribute."""
        clean = {
            _row_key(c) for c in candidate_rows(self.sc.issue_key, self.args.eval_log, self.sc.llm_type)
        }
        ocel_id = injected["affected_ids"][0]
        return [
            c for c in candidate_rows(self.sc.issue_key, db, self.sc.llm_type)
            if c.get("ocel_id") == ocel_id and _row_key(c) not in clean
            and (self.sc.attribute is None or c.get("attribute") == self.sc.attribute)
        ]

    def _candidates(self, db: str, injected: Any) -> tuple[dict, list[dict]]:
        """The injected candidate and `--distractors` clean ones of the same
        type, drawn afresh for every run. `sweep_size` is the number of clean
        candidates a full sweep of the type would judge."""
        sc, k = self.sc, self.args.distractors
        rng = random.Random(f"{self.args.seed}:{sc.name}:{self.run}")
        if sc.issue_key in SCHEMA_KEYS:
            with sqlite3.connect(db) as conn:
                tables = (_event_type_tables if candidate_kind(sc.issue_key) == "event"
                          else _object_type_tables)(conn)
            target = candidate_rows(sc.issue_key, db, sc.llm_type)[0]
            others = [
                c for t, _ in tables if t != sc.llm_type
                for c in candidate_rows(sc.issue_key, db, t)
            ]
        else:
            found = self._injected_candidates(db, injected)
            if len(found) != 1:
                raise RuntimeError(f"{sc.name}: expected 1 injected candidate, found {len(found)}")
            target = found[0]
            others = [
                c for c in candidate_rows(sc.issue_key, db, sc.llm_type)
                if _row_key(c) != _row_key(target)
            ]
        self.rec["sweep_size"] = len(others)
        return target, rng.sample(others, min(k, len(others)))


# --- report ------------------------------------------------------------------------

def _true(v: Any) -> bool:
    return v in (True, "True")


def _int(v: Any) -> int:
    return int(v or 0)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95 % Wilson score interval for k successes in n trials."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def rate(k: int, n: int) -> str:
    """`k/n (lo–hi %)` with the Wilson interval."""
    if not n:
        return "—"
    lo, hi = wilson(k, n)
    return f"{k}/{n} ({100 * lo:.0f}–{100 * hi:.0f} %)"


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _error_size(rs: list[dict]) -> str:
    """Median error over the repaired runs that are not strict-correct."""
    wrong = [r for r in rs if _int(r["repairs_applied"]) > 0 and not _true(r["strict"])]
    if not wrong:
        return "—"
    parts = []
    t = _median([float(r["time_error_min"]) for r in wrong if r["time_error_min"] not in ("", None)])
    if t is not None:
        parts.append(f"{t:.0f} min")
    x = _median([float(r["num_rel_error"]) for r in wrong if r["num_rel_error"] not in ("", None)])
    if x is not None:
        parts.append(f"{100 * x:.0f} %")
    cells = _median([_int(r["cells_wrong"]) for r in wrong])
    rows = _median([_int(r["rows_missing"]) + _int(r["rows_extra"]) for r in wrong])
    if cells:
        parts.append(f"{cells:g} cells")
    if rows:
        parts.append(f"{rows:g} rows")
    return ", ".join(parts) or "schema"


def _names(rs: list[dict], field: str) -> str:
    return ", ".join(sorted({c.split("+")[0] for r in rs
                             for c in str(r[field] or "").split(";") if c})) or "—"


def _sources(rs: list[dict]) -> Counter:
    return Counter(s for r in rs for s in str(r["repair_sources"] or "").split(";") if s)


def _group(det: str, key: str, rs: list[dict]) -> str:
    if key in SCHEMA_KEYS:
        return "schema"
    if det == "llm":
        return "llm_detection"
    sources = set(_sources(rs))
    if sources == {"baseline"}:
        return "baseline"
    return "rule_repair" if sources <= {"rule"} else "llm_repair"


GROUPS = {
    "rule_repair": "Rule detection, rule repair (no LLM involved)",
    "baseline": "Rule detection, lookup baseline repair (no LLM involved)",
    "llm_repair": "Rule detection, LLM repair",
    "llm_detection": "LLM detection, separate LLM repair",
    "schema": "LLM detection of missing attributes (schema)",
}


def render_report(meta: dict, records: list[dict]) -> str:
    by_sc: dict[tuple, list[dict]] = {}
    for r in records:
        by_sc.setdefault((r["cell"], r["issue_key"], r["difficulty"], r["detection"]), []).append(r)

    lines = [
        "# Synthetic Evaluation: inject → detect → resolve",
        "",
        f"*Model:* `{meta['model']}` (digest `{meta.get('model_digest')}`, Ollama "
        f"{meta.get('ollama_version')}) — *date:* {meta['date']} — *runs per scenario:* "
        f"{meta['runs']} — *duration:* {meta['duration_s'] / 60:.0f} min",
        f"*Commit:* `{meta.get('git_sha')}`{' (dirty)' if meta.get('git_dirty') else ''} — "
        f"*eval log sha256:* `{str(meta.get('eval_log_sha256'))[:12]}` — *temperature:* "
        f"{meta.get('temperature')} — *seed:* {meta['seed']} — *LLM distractors:* "
        f"{meta['distractors']} — *time tolerance:* {meta['time_tolerance']} — "
        f"*examination hints:* off"
        + (f" — **baseline:** {meta['baseline']}" if meta.get("baseline") else ""),
        "",
        "Each scenario corrupts **one fixed record** of a clean copy of the log; the "
        "runs repeat it and vary only in the LLM's answers and the distractors. The "
        "95 % intervals (Wilson) therefore describe how stable the answers are, not "
        "how the method generalises over records.",
        "",
        "**Detected**: the issue is reported (rule cells) or the injected candidate is "
        "flagged (LLM cells). **False alarms**: clean candidates of the same type, "
        "drawn afresh per run, flagged by the LLM; *per full sweep* = false-alarm "
        "rate × the number of clean candidates of that type. **Strict**: the "
        "repaired log equals the clean log (numbers ±10 %, timestamps within the "
        "tolerance); needs an applied repair. **Error** (median over repaired runs "
        "that are not strict): largest timestamp error, largest relative numeric "
        "error, wrong cells, rows missing or extra. **Invalid**: LLM replies that "
        "did not parse; left out of the rates. **Errors**: runs whose LLM call "
        "failed; left out of all rates.",
    ]
    grouped: dict[str, list] = {g: [] for g in GROUPS}
    for (cell, key, diff, det), rs in sorted(by_sc.items()):
        grouped[_group(det, key, rs)].append((cell, key, diff, rs))

    for g, rows in grouped.items():
        if not rows:
            continue
        lines += ["", f"## {GROUPS[g]}", ""]
        if g in ("rule_repair", "llm_repair", "baseline"):
            lines += ["| Cell | Issue | Diff. | Runs | Repaired | Strict | Error | Repair by "
                      "| Collateral | New violations | Errors |",
                      "|---|---|---|---|---|---|---|---|---|---|---|"]
        elif g == "llm_detection":
            lines += ["| Cell | Issue | Diff. | Runs | Detected | False alarms | per full sweep "
                      "| Strict | Error | Invalid | Errors |",
                      "|---|---|---|---|---|---|---|---|---|---|---|"]
        else:
            lines += ["| Issue | Diff. | Runs | Recall | Precision (names) | False alarms (types) "
                      "| Strict (columns) | Invalid | Errors |",
                      "|---|---|---|---|---|---|---|---|---|"]
        for cell, key, diff, rs in rows:
            ok = [r for r in rs if not r["error"]]
            errors = len(rs) - len(ok)
            det = [r for r in ok if r["detected"] != "invalid"]
            n_det = sum(1 for r in det if _true(r["detected"]))
            strict = rate(sum(1 for r in det if _true(r["strict"])), len(det))
            fp = sum(_int(r["false_positives"]) for r in det)
            nd = sum(_int(r["distractors"]) for r in det)
            invalid = sum(_int(r["invalid_outputs"]) for r in ok)
            if g in ("rule_repair", "llm_repair", "baseline"):
                repaired = sum(1 for r in ok if _int(r["repairs_applied"]) > 0)
                by = ", ".join(f"{s} {n}" for s, n in _sources(ok).most_common())
                lines.append(
                    f"| {cell} | `{key}` | {diff} | {len(ok)} | {repaired}/{len(ok)} | {strict} "
                    f"| {_error_size(ok)} | {by or '—'} | {_names(ok, 'collateral')} "
                    f"| {_names(ok, 'new_violations')} | {errors or ''} |"
                )
            elif g == "llm_detection":
                sweep = _int(ok[0]["sweep_size"]) if ok else 0
                sweep_fp = f"≈ {fp / nd * sweep:.0f} of {sweep}" if nd else "—"
                lines.append(
                    f"| {cell} | `{key}` | {diff} | {len(ok)} | {rate(n_det, len(det))} "
                    f"| {rate(fp, nd)} | {sweep_fp} | {strict} | {_error_size(det)} "
                    f"| {invalid or ''} | {errors or ''} |"
                )
            else:
                proposed = sum(_int(r["names_proposed"]) for r in det)
                lines.append(
                    f"| `{key}` | {diff} | {len(ok)} | {rate(n_det, len(det))} "
                    f"| {rate(n_det, proposed)} | {rate(fp, nd)} | {strict} "
                    f"| {invalid or ''} | {errors or ''} |"
                )

    lines += ["", "## Totals", "", "| Group | Runs | Detected | Strict |", "|---|---|---|---|"]
    for g, rows in grouped.items():
        det = [r for *_, rs in rows for r in rs if not r["error"] and r["detected"] != "invalid"]
        if det:
            lines.append(
                f"| {GROUPS[g]} | {len(det)} | {rate(sum(_true(r['detected']) for r in det), len(det))} "
                f"| {rate(sum(_true(r['strict']) for r in det), len(det))} |"
            )

    findings = meta.get("clean_log_findings") or {}
    lines += ["", "## Detector findings on the clean log", "",
              "Rows the rule detectors report on the clean log before any injection. "
              "They are subtracted from every run, so they are not counted above; any "
              "listed here is a false positive of that detector."]
    lines += ([""] + [f"- `{k}`: {n}" for k, n in sorted(findings.items())]
              if findings else ["", "None."])

    if meta.get("stopped_early"):
        lines += ["", "⚠️ **Stopped early: the LLM server stayed unreachable.** "
                  "Scenarios after the last row in `runs.csv` were not run."]
    n_err = sum(1 for r in records if r["error"])
    if n_err:
        lines += ["", f"⚠️ **{n_err} run(s) failed with an LLM error** — see `runs.csv`."]
    return "\n".join(lines) + "\n"


def render_scenarios(scenarios: list[Scenario]) -> str:
    """Every scenario's injector docstring: the easy/hard descriptions as
    they were in the code that produced the numbers."""
    lines = ["# Injected issues", "", "| Issue | Diff. | Injector | Description |",
             "|---|---|---|---|"]
    for sc in scenarios:
        doc = " ".join((sc.inject.__doc__ or "").split())
        lines.append(f"| `{sc.issue_key}` | {sc.difficulty} | `{sc.inject.__name__}` | {doc} |")
    return "\n".join(lines) + "\n"


def render_latex(records: list[dict]) -> str:
    by_sc: dict[tuple, list[dict]] = {}
    for r in records:
        by_sc.setdefault((r["cell"], r["issue_key"], r["difficulty"], r["detection"]), []).append(r)
    out = [
        r"\begin{tabular}{llllrr}",
        r"\toprule",
        r"Cell & Issue & Diff. & Det. & Detected & Strict \\",
        r"\midrule",
    ]
    for (cell, key, diff, det), rs in sorted(by_sc.items()):
        ok = [r for r in rs if not r["error"] and r["detected"] != "invalid"]
        n = len(ok)
        pct = lambda f: f"{100 * sum(_true(r[f]) for r in ok) / n:.0f}\\%" if n else "--"
        detected = pct("detected") if det == "llm" else "--"
        out.append(
            f"{cell} & \\texttt{{{key.replace('_', chr(92) + '_')}}} & {diff} & {det} & "
            f"{detected} & {pct('strict')} \\\\"
        )
    out += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(out) + "\n"


# --- main ----------------------------------------------------------------------------

def _git_state() -> tuple[str, bool]:
    """(HEAD sha, any tracked file has uncommitted changes)."""
    def git(*a: str) -> str:
        return subprocess.run(["git", *a], capture_output=True, text=True).stdout.strip()
    return git("rev-parse", "HEAD"), bool(git("status", "--porcelain", "--untracked-files=no"))


def select(only: str | None, baseline: str | None = None) -> list[Scenario]:
    scenarios = SCENARIOS
    if baseline == "lookup":
        scenarios = [s for s in scenarios if s.issue_key in LOOKUP_KEYS]
    if not only:
        return scenarios
    wanted = {w.strip() for w in only.split(",") if w.strip()}
    picked = [s for s in scenarios if s.issue_key in wanted or s.name in wanted]
    unknown = wanted - {s.issue_key for s in picked} - {s.name for s in picked}
    if unknown:
        raise SystemExit(f"Unknown scenario(s): {', '.join(sorted(unknown))}")
    return picked


def _llm_back(minutes: float) -> bool:
    """Wait up to `minutes` for the LLM server to answer (e.g. after an SSH
    tunnel dropped). True once it does."""
    deadline = time.time() + minutes * 60
    while not llm_ready()[0]:
        if time.time() > deadline:
            return False
        print("LLM server unreachable; waiting…", flush=True)
        time.sleep(30)
    return True


def _write_jsonl(fh, run: Run) -> None:
    for kind, items in (("verdict", run.verdicts), ("action", run.actions)):
        for item in items:
            fh[kind].write(json.dumps(
                {"scenario": run.sc.name, "run": run.run, **item}, default=str
            ) + "\n")
        fh[kind].flush()


def evaluate(args, out_dir: Path) -> list[dict]:
    """Run every selected scenario. With `args.wait_minutes`, a run that
    failed while the LLM server was unreachable is repeated once it is back;
    if it stays away, the evaluation stops and returns what it has."""
    # The lookup baseline needs no LLM server, so there is nothing to wait for.
    wait = 0 if getattr(args, "baseline", None) else getattr(args, "wait_minutes", 0)
    clean_snap = snapshot(args.eval_log)
    clean_rows = detector_rows(args.eval_log)
    args.clean_log_findings = {k: len(v) for k, v in clean_rows.items() if v}
    scenarios = select(args.only, getattr(args, "baseline", None))
    records: list[dict] = []
    with (out_dir / "runs.csv").open("w", newline="", encoding="utf-8") as fh, \
            (out_dir / "verdicts.jsonl").open("w", encoding="utf-8") as vf, \
            (out_dir / "actions.jsonl").open("w", encoding="utf-8") as af, \
            tempfile.TemporaryDirectory() as tmp:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for i, sc in enumerate(scenarios, 1):
            for run in range(args.runs):
                if wait and not _llm_back(wait):
                    print(f"LLM server unreachable for {wait:.0f} min; stopping.", flush=True)
                    args.stopped_early = True
                    return records
                r = Run(sc, run, args, clean_snap, clean_rows)
                rec = r.execute(Path(tmp))
                if rec["error"] and wait and not llm_ready()[0]:
                    print(f"{sc.name} run {run + 1}: LLM server lost, repeating "
                          "the run once it is back", flush=True)
                    if not _llm_back(wait):
                        args.stopped_early = True
                        return records
                    r = Run(sc, run, args, clean_snap, clean_rows)
                    rec = r.execute(Path(tmp))
                _write_jsonl({"verdict": vf, "action": af}, r)
                records.append(rec)
                writer.writerow(rec)
                fh.flush()
                print(
                    f"[{i}/{len(scenarios)}] {sc.name} run {run + 1}/{args.runs}: "
                    f"detected={rec['detected']} strict={rec['strict']} ({rec['seconds']}s)"
                    + (f" ERROR {rec['error']}" if rec["error"] else ""),
                    flush=True,
                )
    return records


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", required=True)
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--only", help="comma-separated issue keys or key/difficulty names")
    ap.add_argument("--distractors", type=int, default=20)
    ap.add_argument("--seed", type=int, default=0,
                    help="distractor draw and LLM sampling seed (run r uses seed*1000+r)")
    ap.add_argument("--time-tolerance-hours", type=float, default=10.0)
    ap.add_argument("--eval-log", default=None,
                    help="use this evaluation log instead of rebuilding it from the baseline")
    ap.add_argument("--out-dir", default="data/evaluation/synthetic")
    ap.add_argument("--llm-timeout", type=float, default=120.0, help="seconds per LLM call")
    ap.add_argument("--baseline", choices=["lookup"],
                    help="repair the derived event attributes by lookup instead of the LLM")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="run although the working tree has uncommitted changes")
    ap.add_argument("--wait-minutes", type=float, default=30.0,
                    help="how long to wait for an unreachable LLM server before stopping")
    args = ap.parse_args()
    args.time_tolerance = timedelta(hours=args.time_tolerance_hours)

    sha, dirty = _git_state()
    if dirty and not args.allow_dirty:
        raise SystemExit("Uncommitted changes: commit first so the results map to a "
                         "commit, or pass --allow-dirty for a trial run.")
    info: dict[str, Any] = {}
    if args.baseline is None:
        ok, models = llm_ready()
        if not ok:
            raise SystemExit("Ollama is not reachable (OLLAMA_HOST).")
        if args.model not in models:
            raise SystemExit(f"Model {args.model!r} not pulled. Available: {', '.join(models)}")
        set_active_model(args.model)
        set_timeout(args.llm_timeout)
        info = server_info(args.model)

    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", args.model)
    suffix = f"-{args.baseline}" if args.baseline else ""
    out_dir = Path(args.out_dir) / f"{datetime.now():%Y%m%d-%H%M}-{slug}{suffix}"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"== output: {out_dir} ==", flush=True)

    with tempfile.TemporaryDirectory() as tmp:
        if args.eval_log is None:
            print("Building the evaluation log from the baseline", flush=True)
            args.eval_log = build_eval_log(dst=str(Path(tmp) / "eval.sqlite"))
            built = True
        else:
            built = False
        eval_hash = content_hash(args.eval_log)
        t0 = time.time()
        records = evaluate(args, out_dir)
        meta = {
            "model": args.model, "runs": args.runs, "distractors": args.distractors,
            "seed": args.seed, "eval_log": "rebuilt from the baseline" if built else args.eval_log,
            "eval_log_sha256": eval_hash,
            "time_tolerance": str(args.time_tolerance), "llm_timeout_s": args.llm_timeout,
            "temperature": TEMPERATURE, "min_confidence": MIN_CONFIDENCE,
            "examination_hints": False, "baseline": args.baseline, **info,
            "sqlite_version": sqlite3.sqlite_version, "python_version": platform.python_version(),
            "platform": platform.platform(),
            "git_sha": sha, "git_dirty": dirty,
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "duration_s": time.time() - t0,
            "stopped_early": getattr(args, "stopped_early", False),
            "clean_log_findings": args.clean_log_findings,
        }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    report = render_report(meta, records)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    (out_dir / "table.tex").write_text(render_latex(records), encoding="utf-8")
    (out_dir / "scenarios.md").write_text(
        render_scenarios(select(args.only, args.baseline)), encoding="utf-8")
    print(report, flush=True)


if __name__ == "__main__":
    main()
