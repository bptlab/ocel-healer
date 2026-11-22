"""Rule-based detection on the real-world logs: how many issues each detector finds.

The real-world logs have no ground truth, so this only counts: for every log
and every rule detector (`detect_all`), the number of rows it reports. No LLM
is used and nothing is repaired; each log is opened read-only.

One markdown file per log is written to `--out-dir`, with the log's size,
its findings grouped like the dashboard overview, and the detection runtime.

Usage:
    python -m src.evaluation.evaluate_real_world [--only name,...] [--allow-dirty]
"""

from __future__ import annotations

import argparse
import platform
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from src.detection.error_detection import (
    _event_type_tables,
    _object_type_tables,
    detect_all,
)
from src.evaluation.evaluate_synthetic import _git_state

# Rule-detected issue keys by dashboard overview cell (`overview_config` in
# src/dashboard.py), in overview order.
CELLS = [
    ("Missing Data", "Event", "missing_event"),
    ("Missing Data", "Event Type", "missing_event_type"),
    ("Missing Data", "Event Time", "missing_event_timestamp"),
    ("Missing Data", "Event Attribute Value", "missing_event_attribute_value"),
    ("Missing Data", "Object", "missing_object"),
    ("Missing Data", "Object Type", "missing_object_type"),
    ("Missing Data", "Object Attribute Value", "missing_attribute_value"),
    ("Missing Data", "Object-to-Object", "dangling_o2o_relationship"),
    ("Missing Data", "Event-to-Object", "dangling_e2o_relationship"),
    ("Incorrect Data", "Event", "duplicate_events_on_ids"),
    ("Incorrect Data", "Event", "duplicate_events_on_attributes"),
    ("Incorrect Data", "Event Attribute Type", "incorrect_event_attribute_datatype"),
    ("Incorrect Data", "Object", "duplicate_objects_on_ids"),
    ("Incorrect Data", "Object", "duplicate_objects_on_attributes"),
    ("Incorrect Data", "Object Attribute Type", "incorrect_attribute_datatype"),
    ("Incorrect Data", "Object-to-Object", "duplicate_o2o_relations"),
    ("Incorrect Data", "Object-to-Object", "o2o_self_loop"),
    ("Incorrect Data", "Event-to-Object", "duplicate_e2o_relations"),
]

STAT_TABLES = ["event", "object", "event_object", "object_object"]


def _readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)


def log_stats(conn: sqlite3.Connection) -> dict[str, int]:
    stats = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in STAT_TABLES}
    stats["event types"] = len(_event_type_tables(conn))
    stats["object types"] = len(_object_type_tables(conn))
    return stats


def evaluate_log(path: Path) -> dict:
    with _readonly(path) as conn:
        stats = log_stats(conn)
        t0 = time.time()
        found = {k: len(df) for k, df in detect_all(conn).items()}
        seconds = time.time() - t0
    return {"stats": stats, "counts": found, "seconds": seconds}


def render_report(path: Path, result: dict, meta: dict) -> str:
    counts = result["counts"]
    missing = set(counts) - {k for *_, k in CELLS}
    if missing:  # a detector was added to detect_all but not to CELLS
        raise RuntimeError(f"No overview cell for: {', '.join(sorted(missing))}")
    lines = [
        f"# Rule-based detection: `{path.stem}`",
        "",
        f"*Log:* `{path.as_posix()}` ({path.stat().st_size / 1e6:.0f} MB) — *date:* {meta['date']} "
        f"— *detection runtime:* {result['seconds']:.1f} s",
        f"*Commit:* `{meta['git_sha']}`{' (dirty)' if meta['git_dirty'] else ''} — "
        f"*Python* {meta['python_version']} — *SQLite* {meta['sqlite_version']}",
        "",
        "Rows reported by each rule detector on the log as published (no ground truth, "
        "no LLM, no repair).",
        "",
        "## Log",
        "",
        "| Table | Rows |",
        "|---|---:|",
        *(f"| {k} | {v:,} |" for k, v in result["stats"].items()),
        "",
        "## Findings",
        "",
        "| Row | Cell | Issue | Count |",
        "|---|---|---|---:|",
        *(f"| {row} | {cell} | `{key}` | {counts[key]:,} |" for row, cell, key in CELLS),
        f"| | | **Total** | **{sum(counts.values()):,}** |",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--logs-dir", default="data/real-world")
    ap.add_argument("--only", help="comma-separated log names (file stem)")
    ap.add_argument("--out-dir", default="evaluation_results/real_world")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="run although the working tree has uncommitted changes")
    args = ap.parse_args()

    sha, dirty = _git_state()
    if dirty and not args.allow_dirty:
        raise SystemExit("Uncommitted changes: commit first so the results map to a "
                         "commit, or pass --allow-dirty for a trial run.")
    logs = sorted(Path(args.logs_dir).glob("*.sqlite"))
    if args.only:
        wanted = {w.strip() for w in args.only.split(",") if w.strip()}
        unknown = wanted - {p.stem for p in logs}
        if unknown:
            raise SystemExit(f"Unknown log(s): {', '.join(sorted(unknown))}")
        logs = [p for p in logs if p.stem in wanted]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "git_sha": sha, "git_dirty": dirty,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "python_version": platform.python_version(),
        "sqlite_version": sqlite3.sqlite_version,
    }
    for i, path in enumerate(logs, 1):
        result = evaluate_log(path)
        (out_dir / f"{path.stem}.md").write_text(render_report(path, result, meta), encoding="utf-8")
        print(f"[{i}/{len(logs)}] {path.stem}: {sum(result['counts'].values()):,} findings "
              f"in {result['seconds']:.1f}s", flush=True)


if __name__ == "__main__":
    main()
