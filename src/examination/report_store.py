# src/examination/report_store.py

"""Per-database storage for examination artifacts.

Artifacts live next to the data: each examined database gets its own
directory under an `examination/` folder beside the sqlite file, keyed by the
file's stem, so reports for different logs never overwrite each other:

    <db_dir>/examination/<db_stem>/examination_profile.json
    <db_dir>/examination/<db_stem>/examination_guide.json
    <db_dir>/examination/<db_stem>/examination_report.md

e.g. data/synthetic/examination/order-management-corrupted/. This is the
single convention shared by the dashboard, the CLI, the repair tasks' hint
lookup and the evaluation; pass `base_dir` only to override it.
"""

import json
from pathlib import Path
from typing import Any

def default_base_dir(db_path: str | Path) -> Path:
    """`<db_dir>/examination` — where artifacts for `db_path` live by default."""
    return Path(db_path).parent / "examination"


def examination_dir(db_path: str | Path, base_dir: str | Path | None = None) -> Path:
    base = Path(base_dir) if base_dir is not None else default_base_dir(db_path)
    return base / Path(db_path).stem


def profile_path(db_path: str | Path, base_dir: str | Path | None = None) -> Path:
    return examination_dir(db_path, base_dir) / "examination_profile.json"


def guide_path(db_path: str | Path, base_dir: str | Path | None = None) -> Path:
    return examination_dir(db_path, base_dir) / "examination_guide.json"


def report_path(db_path: str | Path, base_dir: str | Path | None = None) -> Path:
    return examination_dir(db_path, base_dir) / "examination_report.md"


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


_fingerprint_cache: dict[str, tuple[tuple[int, int], str]] = {}


def current_fingerprint(db_path: str | Path) -> str:
    """`schema_fingerprint` of the log, recomputed only when the file changed
    (mtime or size). The dashboard asks on every refresh, and the scan takes
    up to a second on the large real-world logs."""
    from src.examination.db_profiler import connect_readonly, schema_fingerprint

    p = Path(db_path)
    st = p.stat()
    stamp = (st.st_mtime_ns, st.st_size)
    cached = _fingerprint_cache.get(str(p))
    if cached is not None and cached[0] == stamp:
        return cached[1]
    conn = connect_readonly(p)
    try:
        fingerprint = schema_fingerprint(conn)
    finally:
        conn.close()
    _fingerprint_cache[str(p)] = (stamp, fingerprint)
    return fingerprint


def guide_is_stale(db_path: str | Path, base_dir: str | Path | None = None) -> bool:
    """True when there is no guide or the log's *structure* changed since it
    was built (tables, object types, qualifiers). Value-level repairs do not
    invalidate the guide — its knowledge is about semantics, not values."""
    guide = load_guide(db_path, base_dir)
    if guide is None:
        return True
    try:
        current = current_fingerprint(db_path)
    except Exception:  # noqa: BLE001 — unreadable db == can't trust the guide
        return True
    return current != guide.get("source_fingerprint")
