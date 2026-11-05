# src/exploration/report_store.py

"""Per-database storage for exploration artifacts.

Artifacts live next to the data: each explored database gets its own
directory under an `exploration/` folder beside the sqlite file, keyed by the
file's stem, so reports for different logs never overwrite each other:

    <db_dir>/exploration/<db_stem>/exploration_profile.json
    <db_dir>/exploration/<db_stem>/exploration_guide.json
    <db_dir>/exploration/<db_stem>/exploration_report.md

e.g. data/synthetic/exploration/order-management-corrupted/. This is the
single convention shared by the dashboard, the CLI, the repair tasks' hint
lookup and the evaluation; pass `base_dir` only to override it.
"""

import json
from pathlib import Path
from typing import Any

def default_base_dir(db_path: str | Path) -> Path:
    """`<db_dir>/exploration` — where artifacts for `db_path` live by default."""
    return Path(db_path).parent / "exploration"


def exploration_dir(db_path: str | Path, base_dir: str | Path | None = None) -> Path:
    base = Path(base_dir) if base_dir is not None else default_base_dir(db_path)
    return base / Path(db_path).stem


def profile_path(db_path: str | Path, base_dir: str | Path | None = None) -> Path:
    return exploration_dir(db_path, base_dir) / "exploration_profile.json"


def guide_path(db_path: str | Path, base_dir: str | Path | None = None) -> Path:
    return exploration_dir(db_path, base_dir) / "exploration_guide.json"


def report_path(db_path: str | Path, base_dir: str | Path | None = None) -> Path:
    return exploration_dir(db_path, base_dir) / "exploration_report.md"


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_json(p: Path) -> dict[str, Any] | None:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def load_guide(db_path: str | Path, base_dir: str | Path | None = None) -> dict[str, Any] | None:
    return _load_json(guide_path(db_path, base_dir))


def load_profile(db_path: str | Path, base_dir: str | Path | None = None) -> dict[str, Any] | None:
    return _load_json(profile_path(db_path, base_dir))


def load_report(db_path: str | Path, base_dir: str | Path | None = None) -> str:
    p = report_path(db_path, base_dir)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def guide_is_stale(db_path: str | Path, base_dir: str | Path | None = None) -> bool:
    """True when there is no guide or the log's *structure* changed since it
    was built (tables, object types, qualifiers). Value-level repairs do not
    invalidate the guide — its knowledge is about semantics, not values."""
    from src.exploration.db_profiler import connect_readonly, schema_fingerprint

    guide = load_guide(db_path, base_dir)
    if guide is None:
        return True
    try:
        conn = connect_readonly(db_path)
        try:
            current = schema_fingerprint(conn)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — unreadable db == can't trust the guide
        return True
    return current != guide.get("source_fingerprint")
