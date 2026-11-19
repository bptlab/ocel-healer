"""Per-issue synthetic evaluation: inject one issue, detect it, resolve it.

For every scenario in `scenarios.py` (one per overview issue key and
difficulty) and every run:

  1. Copy the clean evaluation log (`build_eval_log.py`) and inject the issue.
  2. Detect it.
     * Rule cells: rows the detector reports on top of the clean log.
     * LLM cells: the LLM judges the injected candidate plus `--distractors`
       clean candidates of the same type (fixed seed), as the dashboard sweep
       would. Flagged distractors are false alarms.
  3. Resolve every detected row the way the dashboard does
     (`suggest_repair` → `apply_repair`). LLM cells fix with the detection's
     own suggestion, like a confirmed flag in the dashboard. Datatype repairs
     that empty a value get one follow-up repair of the missing value.
  4. Score the repaired log. Both scores need at least one applied repair.
     * lenient: the issue is no longer detected and no detector reports
       more rows than on the corrupted log (rows that grew are listed in
       `new_violations`). LLM cells check the injected candidate against
       the clean log first and only re-ask the LLM if it changed into
       something that is neither clean nor the corruption.
     * strict: the repaired log equals the clean log (numbers within 10 %,
       timestamps within `--time-tolerance`). For schema issues
       (`missing_*_attribute`) the dropped column must be back under its
       original name; its values can't be recovered by construction.
     Strict does not imply lenient: a value within tolerance can still
     trigger another detector (e.g. copying a peer's value).

Injections are deterministic, so the runs of a scenario repeat the same
corruption and vary only in the LLM's answers (detection in LLM cells,
repair in all cells). LLM timeouts are counted in `timeouts`.

Usage:
    python -m src.evaluation.evaluate_synthetic --model qwen3:8b [--runs 10]
        [--only key[/difficulty],...] [--distractors 20]
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import shutil
import sqlite3
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
from src.evaluation.build_eval_log import DEFAULT_EVAL_PATH, build_eval_log
from src.evaluation.scenarios import SCENARIOS, SCHEMA_KEYS, Scenario
from src.llm.actions import apply_repair
from src.llm.candidate_sources import candidate_kind, candidate_rows
from src.llm.client import llm_ready, set_active_model, set_timeout
from src.llm.resolution import detect_all_with_llm, detect_with_llm, suggest_repair
from src.order_management_corruption._common import _remove_all_primary_keys

NUMERIC_TOLERANCE = 0.10
MAX_REPAIRS = 10  # repair calls per run; one injection can surface as several rows

# A repair that empties an unusable value (e.g. 'unknown' in a REAL column)
# leaves a missing value behind; fix it the way a dashboard user would next.
FOLLOW_UPS = {
    "incorrect_attribute_datatype": "missing_attribute_value",
    "incorrect_event_attribute_datatype": "missing_event_attribute_value",
}

CSV_FIELDS = [
    "issue_key", "cell", "difficulty", "detection", "run",
    "detected", "issue_rows", "false_positives", "distractors", "collateral",
    "repairs_attempted", "repairs_applied", "action_kinds", "noop_reasons",
    "new_violations", "timeouts", "lenient", "strict", "error", "seconds",
]


def _timed_out(text: Any) -> bool:
    return "timed out" in str(text).lower()


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


def values_close(a: Any, b: Any, time_tol: timedelta) -> bool:
    if a == b:
        return True
    num = (int, float)
    if isinstance(a, num) and isinstance(b, num) and not isinstance(a, bool) and not isinstance(b, bool):
        return abs(a - b) <= NUMERIC_TOLERANCE * abs(a) if a else abs(b) <= NUMERIC_TOLERANCE
    ta, tb = _as_time(a), _as_time(b)
    if ta and tb:
        return abs(ta - tb) <= time_tol
    return False


def _rows_close(r1: tuple, r2: tuple, time_tol: timedelta) -> bool:
    return len(r1) == len(r2) and all(values_close(a, b, time_tol) for a, b in zip(r1, r2))


def equals_clean(clean: Snapshot, repaired: Snapshot, time_tol: timedelta) -> bool:
    """True if `repaired` matches `clean` row for row (with tolerance).
    Rows that differ are paired on their first column (the id)."""
    if set(clean) != set(repaired):
        return False
    for table, (cols, rows) in clean.items():
        cols2, rows2 = repaired[table]
        if cols != cols2:
            return False
        missing = list((rows - rows2).elements())
        extra = list((rows2 - rows).elements())
        if len(missing) != len(extra):
            return False
        for m in missing:
            match = next(
                (i for i, e in enumerate(extra) if e[0] == m[0] and _rows_close(m, e, time_tol)),
                None,
            )
            if match is None:
                return False
            extra.pop(match)
    return True


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _names_match(suggested: str, dropped: str) -> bool:
    s, d = _norm(suggested), _norm(dropped)
    return bool(s) and (s == d or d in s)


def _columns(db: str, table: str) -> list[str]:
    with sqlite3.connect(db) as conn:
        return [r[1] for r in conn.execute(f"PRAGMA table_info('{table}')")]


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


# --- one run ---------------------------------------------------------------------

class Run:
    def __init__(self, sc: Scenario, run: int, args, clean_snap: Snapshot,
                 clean_rows: dict[str, dict]):
        self.sc, self.args = sc, args
        self.clean_snap, self.clean_rows = clean_snap, clean_rows
        self.rec: dict[str, Any] = {
            "issue_key": sc.issue_key, "cell": sc.cell, "difficulty": sc.difficulty,
            "detection": sc.detection, "run": run, "detected": False,
            "issue_rows": 0, "false_positives": "", "distractors": "",
            "collateral": "", "repairs_attempted": 0, "repairs_applied": 0,
            "action_kinds": "", "noop_reasons": "", "new_violations": "",
            "timeouts": 0, "lenient": False, "strict": False, "error": "",
        }
        self.kinds: list[str] = []
        self.noops: list[str] = []

    def execute(self, workdir: Path) -> dict:
        t0 = time.time()
        db = str(workdir / "run.sqlite")
        shutil.copy2(self.args.eval_log, db)
        try:
            with sqlite3.connect(db) as conn:
                _remove_all_primary_keys(conn)
                injected = self.sc.inject(conn)
                conn.commit()
            if injected is None:
                raise RuntimeError("injector found no target row")
            corrupted = detector_rows(db)
            self.rec["collateral"] = ";".join(
                f"{k}+{n - len(self.clean_rows[k])}"
                for k, n in counts(corrupted).items()
                if k != self.sc.issue_key and n > len(self.clean_rows[k])
            )
            if self.sc.detection == "rule":
                self._rule(db, corrupted)
            else:
                self._llm(db, corrupted, injected)
        except Exception as e:  # noqa: BLE001 — one failing run must not sink the evaluation
            self.rec["error"] = f"{type(e).__name__}: {e}"
            self.rec["timeouts"] += _timed_out(e)
        self.rec["action_kinds"] = ";".join(self.kinds)
        self.rec["noop_reasons"] = " | ".join(self.noops)[:500]
        self.rec["seconds"] = round(time.time() - t0, 1)
        return self.rec

    # -- repair plumbing shared by both detection modes --

    def _repair(self, row: dict, db: str, key: str | None = None) -> bool:
        """Ask for and apply one repair. Returns True if something was applied.
        `key` overrides the scenario's issue key for follow-up repairs."""
        self.rec["repairs_attempted"] += 1
        prefix = "followup:" if key else ""
        try:
            action = suggest_repair(key or self.sc.issue_key, row, db)
        except TimeoutError:
            self.rec["timeouts"] += 1
            self.kinds.append(f"{prefix}timeout")
            return False
        self.kinds.append(prefix + action["kind"])
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

    def _score(self, db: str, corrupted: dict[str, dict], issue_gone: bool) -> None:
        """Nothing counts as resolved unless a repair was actually applied:
        a re-check that merely stops flagging the issue is not a fix."""
        after = counts(detector_rows(db))
        grew = {k: after[k] - len(v) for k, v in corrupted.items() if after[k] > len(v)}
        self.rec["new_violations"] = ";".join(f"{k}+{n}" for k, n in grew.items())
        repaired = self.rec["repairs_applied"] > 0
        self.rec["lenient"] = repaired and issue_gone and not grew
        if self.sc.issue_key in SCHEMA_KEYS:
            return  # strict is set by _llm (column-name check)
        self.rec["strict"] = repaired and equals_clean(
            self.clean_snap, snapshot(db), self.args.time_tolerance
        )

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
        remaining = new_rows(detector_rows(db), self.clean_rows, key)
        self._score(db, corrupted, issue_gone=not remaining)

    # -- LLM cells --

    def _llm(self, db: str, corrupted: dict[str, dict], injected: Any) -> None:
        sc = self.sc
        target, distractors = self._candidates(db, injected)
        self.rec["issue_rows"] = 1
        self.rec["distractors"] = len(distractors)
        verdicts = detect_all_with_llm(sc.issue_key, [target, *distractors], db)
        self.rec["timeouts"] += sum(1 for _, v in verdicts if _timed_out(v.rationale))
        (_, tv), others = verdicts[0], verdicts[1:]
        self.rec["false_positives"] = sum(1 for _, v in others if v.flagged)

        row = {k: v for k, v in target.items() if not k.startswith("_")}
        if sc.issue_key in SCHEMA_KEYS:
            hit = next((
                s for s in (tv.suggested_value or [])
                if isinstance(s, dict) and _names_match(s.get("name", ""), sc.attribute)
            ), None) if tv.flagged else None
            self.rec["detected"] = hit is not None
            if hit is None:
                return
            table = target["_resolved_target_table"]
            self._repair({**row, **_stash(tv, hit), "attribute": hit["name"],
                          "_resolved_target_table": table}, db)
            cols = _columns(db, table)
            repaired = self.rec["repairs_applied"] > 0
            self.rec["lenient"] = repaired and any(_names_match(c, sc.attribute) for c in cols)
            self.rec["strict"] = repaired and any(c.lower() == sc.attribute.lower() for c in cols)
            return

        self.rec["detected"] = tv.flagged
        if not tv.flagged:
            return
        # Fix with the detection's own suggestion, as the dashboard does for
        # a confirmed flag, instead of a second LLM call that may disagree.
        self._repair({**row, **_stash(tv, tv.suggested_value)}, db)
        # Ground truth first: the injected candidate is back to its clean
        # state or left the swept type (e.g. its type was corrected). Only a
        # candidate that changed into something else is re-judged.
        again = self._injected_candidates(db, injected)
        issue_gone = not again or (
            _row_key(again[0]) != _row_key(target)
            and not detect_with_llm(sc.issue_key, again[0], db).flagged
        )
        self._score(db, corrupted, issue_gone)

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
        sc, k = self.sc, self.args.distractors
        rng = random.Random(f"{self.args.seed}:{sc.name}")
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
                raise RuntimeError(f"expected 1 injected candidate, found {len(found)}")
            target = found[0]
            others = [
                c for c in candidate_rows(sc.issue_key, db, sc.llm_type)
                if _row_key(c) != _row_key(target)
            ]
        return target, rng.sample(others, min(k, len(others)))


# --- report ------------------------------------------------------------------------

def _frac(rows: list[dict], field: str) -> str:
    return f"{sum(1 for r in rows if r[field] in (True, 'True'))}/{len(rows)}"


def _fp_rate(rows: list[dict]) -> str:
    fp = sum(int(r["false_positives"] or 0) for r in rows)
    n = sum(int(r["distractors"] or 0) for r in rows)
    return f"{fp / n:.0%} ({fp}/{n})" if n else "—"


def render_report(meta: dict, records: list[dict]) -> str:
    by_sc: dict[tuple, list[dict]] = {}
    for r in records:
        by_sc.setdefault((r["cell"], r["issue_key"], r["difficulty"], r["detection"]), []).append(r)

    lines = [
        "# Synthetic Evaluation: inject → detect → resolve",
        "",
        f"*Model:* `{meta['model']}` — *date:* {meta['date']} — *runs per scenario:* "
        f"{meta['runs']} — *duration:* {meta['duration_s'] / 60:.0f} min",
        f"*Log:* `{meta['eval_log']}` — *LLM distractors:* {meta['distractors']} — "
        f"*seed:* {meta['seed']} — *time tolerance:* {meta['time_tolerance']}",
        "",
        "Each run injects one issue into a clean copy of the log. **Detected**: the "
        "issue is reported (rule cells) or the injected candidate is flagged (LLM "
        "cells). **False alarms**: clean distractor candidates flagged by the LLM. "
        "**Repaired**: at least one repair was applied. **Lenient**: issue no longer "
        "detected and no detector reports more rows than before the repair. "
        "**Strict**: the repaired log equals the clean log (numbers ±10 %); for "
        "missing attributes, the dropped column is back under its original name. "
        "Lenient and strict need an applied repair. **New violations**: detectors "
        "reporting more rows after the repair than before it. Injections are "
        "deterministic: runs vary only in the LLM's answers.",
        "",
        "| Cell | Issue | Diff. | Det. | Detected | False alarms | Repaired | Lenient | Strict "
        "| Collateral | New violations | Timeouts | Errors |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for (cell, key, diff, det), rs in sorted(by_sc.items()):
        repaired = f"{sum(1 for r in rs if int(r['repairs_applied']) > 0)}/{len(rs)}"
        collateral, new_viol = (
            sorted({c.split('+')[0] for r in rs for c in str(r[f] or "").split(";") if c})
            for f in ("collateral", "new_violations")
        )
        timeouts = sum(int(r.get("timeouts") or 0) for r in rs)
        errors = sum(1 for r in rs if r["error"])
        lines.append(
            f"| {cell} | `{key}` | {diff} | {det} | {_frac(rs, 'detected')} | "
            f"{_fp_rate(rs) if det == 'llm' else '—'} | {repaired} | "
            f"{_frac(rs, 'lenient')} | {_frac(rs, 'strict')} | "
            f"{', '.join(collateral) or '—'} | {', '.join(new_viol) or '—'} | "
            f"{timeouts or ''} | {errors or ''} |"
        )

    lines += ["", "## Totals", "", "| Group | Runs | Detected | Lenient | Strict |", "|---|---|---|---|---|"]
    groups = {
        "all": records,
        "rule cells": [r for r in records if r["detection"] == "rule"],
        "LLM cells": [r for r in records if r["detection"] == "llm"],
        "easy": [r for r in records if r["difficulty"] == "easy"],
        "hard": [r for r in records if r["difficulty"] == "hard"],
    }
    for name, rs in groups.items():
        if rs:
            lines.append(f"| {name} | {len(rs)} | {_frac(rs, 'detected')} | "
                         f"{_frac(rs, 'lenient')} | {_frac(rs, 'strict')} |")
    n_err = sum(1 for r in records if r["error"])
    if n_err:
        lines += ["", f"⚠️ **{n_err} run(s) failed with an error** — see `runs.csv`."]
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
        by_sc.setdefault((r["cell"], r["issue_key"], r["difficulty"]), []).append(r)
    out = [
        r"\begin{tabular}{lllrrr}",
        r"\toprule",
        r"Cell & Issue & Diff. & Detected & Lenient & Strict \\",
        r"\midrule",
    ]
    for (cell, key, diff), rs in sorted(by_sc.items()):
        n = len(rs)
        pct = lambda f: f"{100 * sum(1 for r in rs if r[f] in (True, 'True')) / n:.0f}\\%"
        out.append(
            f"{cell} & \\texttt{{{key.replace('_', chr(92) + '_')}}} & {diff} & "
            f"{pct('detected')} & {pct('lenient')} & {pct('strict')} \\\\"
        )
    out += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(out) + "\n"


# --- main ----------------------------------------------------------------------------

def _git_state() -> tuple[str, bool]:
    """(HEAD sha, working tree has uncommitted changes under src/)."""
    def git(*a: str) -> str:
        return subprocess.run(["git", *a], capture_output=True, text=True).stdout.strip()
    return git("rev-parse", "HEAD"), bool(git("status", "--porcelain", "--", "src"))


def select(only: str | None) -> list[Scenario]:
    if not only:
        return SCENARIOS
    wanted = {w.strip() for w in only.split(",") if w.strip()}
    picked = [s for s in SCENARIOS if s.issue_key in wanted or s.name in wanted]
    unknown = wanted - {s.issue_key for s in picked} - {s.name for s in picked}
    if unknown:
        raise SystemExit(f"Unknown scenario(s): {', '.join(sorted(unknown))}")
    return picked


def evaluate(args, out_dir: Path) -> list[dict]:
    clean_snap = snapshot(args.eval_log)
    clean_rows = detector_rows(args.eval_log)
    scenarios = select(args.only)
    records: list[dict] = []
    csv_path = out_dir / "runs.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh, \
            tempfile.TemporaryDirectory() as tmp:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for i, sc in enumerate(scenarios, 1):
            for run in range(args.runs):
                rec = Run(sc, run, args, clean_snap, clean_rows).execute(Path(tmp))
                records.append(rec)
                writer.writerow(rec)
                fh.flush()
                print(
                    f"[{i}/{len(scenarios)}] {sc.name} run {run + 1}/{args.runs}: "
                    f"detected={rec['detected']} lenient={rec['lenient']} "
                    f"strict={rec['strict']} ({rec['seconds']}s)"
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
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--time-tolerance-hours", type=float, default=1.0)
    ap.add_argument("--eval-log", default=DEFAULT_EVAL_PATH)
    ap.add_argument("--out-dir", default="data/evaluation/synthetic")
    ap.add_argument("--llm-timeout", type=float, default=120.0, help="seconds per LLM call")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="run although the working tree has uncommitted changes")
    args = ap.parse_args()
    args.time_tolerance = timedelta(hours=args.time_tolerance_hours)

    sha, dirty = _git_state()
    if dirty and not args.allow_dirty:
        raise SystemExit("Uncommitted changes: commit first so the results map to a "
                         "commit, or pass --allow-dirty for a trial run.")
    ok, models = llm_ready()
    if not ok:
        raise SystemExit("Ollama is not reachable (OLLAMA_HOST).")
    if args.model not in models:
        raise SystemExit(f"Model {args.model!r} not pulled. Available: {', '.join(models)}")
    set_active_model(args.model)
    set_timeout(args.llm_timeout)
    if not Path(args.eval_log).exists():
        print(f"Building evaluation log at {args.eval_log}", flush=True)
        build_eval_log(dst=args.eval_log)

    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", args.model)
    out_dir = Path(args.out_dir) / f"{datetime.now():%Y%m%d-%H%M}-{slug}"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"== output: {out_dir} ==", flush=True)

    t0 = time.time()
    records = evaluate(args, out_dir)
    meta = {
        "model": args.model, "runs": args.runs, "distractors": args.distractors,
        "seed": args.seed, "eval_log": args.eval_log,
        "time_tolerance": str(args.time_tolerance), "llm_timeout_s": args.llm_timeout,
        "git_sha": sha, "git_dirty": dirty,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "duration_s": time.time() - t0,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    report = render_report(meta, records)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    (out_dir / "table.tex").write_text(render_latex(records), encoding="utf-8")
    (out_dir / "scenarios.md").write_text(render_scenarios(select(args.only)), encoding="utf-8")
    print(report, flush=True)


if __name__ == "__main__":
    main()
