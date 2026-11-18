"""Shared helpers used across the corruption injectors.

Path constants, schema tweaks (PK removal), the ground-truth record
(`Injection`, `write_manifest`), seeded target selection (`TargetPicker`),
and low-level inject helpers (`_null_type_for`, `_capture_and_update`,
`_clone_object_row`, `_delete_object_fully`, `_delete_event_fully`,
`inject_missing_attribute_value`) that the issue modules build on.
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"

DEFAULT_CLEAN_PATH = str(_DATA_DIR / "synthetic" / "order-management-baseline.sqlite")
DEFAULT_FULL_PATH = str(_DATA_DIR / "synthetic" / "order-management-corrupted.sqlite")     # level='all' output


NUMERIC_TOLERANCE = 0.10  # relative error counted as "within tolerance"


# --- ground truth -----------------------------------------------------------

INJECTION_KINDS = frozenset({
    "object", "event", "object_attr", "event_attr",
    "event_time", "e2o", "o2o", "schema",
})
ACCEPT_MODES = frozenset({
    "exact", "numeric_tol", "time_window", "delete_any_of", "one_of",
})


@dataclass
class Injection:
    """One injected corruption plus what counts as detecting and repairing it.

    - `target`: the corrupted ids, or the relation triple for e2o/o2o.
    - `original`: the pre-corruption state, including deleted sub-table rows
      and dropped column values.
    - `expected_rows`: detector rows this injection should produce, as
      ``{"issue": <detector key>, **identifying fields}``, including declared
      secondary hits (e.g. `missing_object` also tripping `dangling_e2o`).
    - `accept`: the repair-scoring rule, ``{"mode": <ACCEPT_MODES>, ...}``.
    """

    issue_type: str
    tier: str
    flavor: str
    kind: str
    target: Any
    original: Any = None
    corrupted: Any = None
    expected_rows: list[dict] = field(default_factory=list)
    accept: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in INJECTION_KINDS:
            raise ValueError(f"Unknown injection kind {self.kind!r}")
        if self.accept and self.accept.get("mode") not in ACCEPT_MODES:
            raise ValueError(f"Unknown accept mode {self.accept.get('mode')!r}")

    def to_dict(self) -> dict:
        return asdict(self)


def _accept_value(value: object) -> dict:
    """Repair rule for restoring a single attribute value."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {"mode": "numeric_tol", "value": value, "tol": NUMERIC_TOLERANCE}
    return {"mode": "exact", "value": value}


def _json_default(value: object) -> object:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__bytes__": bytes(value).hex()}
    raise TypeError(f"Not JSON serializable: {type(value).__name__}")


def manifest_path_for(dst_path: str | Path) -> Path:
    return Path(f"{dst_path}.groundtruth.json")


def write_manifest(
    dst_path: str | Path, injections: list[Injection], meta: dict | None = None
) -> Path:
    """Write `<dst_path>.groundtruth.json` next to the corrupted log."""
    path = manifest_path_for(dst_path)
    payload = {**(meta or {}), "injections": [i.to_dict() for i in injections]}
    path.write_text(json.dumps(payload, indent=2, default=_json_default) + "\n")
    return path


# --- seeded target selection ------------------------------------------------

# Events that already coincide in (ocel_time, resource) in the source log and
# form the baseline `duplicate_events_on_attributes` groups. Never corrupt them.
PROTECTED_IDS = frozenset({
    "pick_i-882845", "pick_i-883212", "send_p-661091", "send_p-661084",
})


# Cap on E2O rows referencing an object eligible for deletion, so the
# missing_object cascade stays bounded (products/employees/customers have
# hundreds to thousands; orders, items and packages at most 16).
MAX_DELETE_FANOUT = 20


class TargetPicker:
    """Seeded, non-overlapping target selection for one corrupted log.

    Every id handed out (or registered via `touch`) is remembered, so no
    object or event is corrupted twice in the same log. All randomness goes
    through one `random.Random(seed)`, so a fixed call order yields a fixed
    set of targets.
    """

    def __init__(self, conn: sqlite3.Connection, seed: int = 0, n: int = 1):
        self.conn = conn
        self.seed = seed
        self.n = n
        self.rng = random.Random(seed)
        self.touched: set[str] = set()
        self._issued: set[str] = set()

    def touch(self, *ids: str) -> None:
        self.touched.update(ids)

    def pick(
        self,
        sql: str,
        params: tuple = (),
        k: int | None = None,
        *,
        max_fanout: int | None = None,
    ) -> list[str]:
        """Sample `k` (default `n`) ids from the first column of `sql`.

        Already-touched and protected ids are skipped. With `max_fanout`,
        objects referenced by more than that many E2O rows are skipped too
        (keeps deletion cascades bounded). Raises if too few candidates
        remain instead of silently returning fewer.
        """
        k = self.n if k is None else k
        ids = sorted({
            r[0] for r in self.conn.execute(sql, params)
            if r[0] is not None
        } - self.touched - PROTECTED_IDS)
        if max_fanout is not None and ids:
            fanout = dict(self.conn.execute(
                "SELECT ocel_object_id, COUNT(*) FROM event_object "
                "GROUP BY ocel_object_id"
            ).fetchall())
            ids = [i for i in ids if fanout.get(i, 0) <= max_fanout]
        if len(ids) < k:
            raise ValueError(
                f"Only {len(ids)} eligible candidate(s), need {k}: {sql.strip()}"
            )
        chosen = sorted(self.rng.sample(ids, k))
        self.touched.update(chosen)
        return chosen

    def _exists(self, ocel_id: str) -> bool:
        return ocel_id in self._issued or any(
            self.conn.execute(f"SELECT 1 FROM {t} WHERE ocel_id = ? LIMIT 1", (ocel_id,)).fetchone()
            for t in ("object", "event")
        )

    def new_id(self, prefix: str) -> str:
        """Next free id of the form `<prefix><digits>`, keeping the log's
        zero-padding (e.g. ``new_id("o-")`` → ``o-992001``)."""
        pat = re.compile(re.escape(prefix) + r"(\d+)$")
        existing = [
            m.group(1)
            for t in ("object", "event")
            for (oid,) in self.conn.execute(
                f"SELECT ocel_id FROM {t} WHERE substr(ocel_id, 1, ?) = ?",
                (len(prefix), prefix),
            )
            if (m := pat.match(oid or ""))
        ] + [m.group(1) for i in self._issued if (m := pat.match(i))]
        if not existing:
            raise ValueError(f"No ids with prefix {prefix!r} in the log")
        width = max(len(d) for d in existing)
        new = f"{prefix}{max(int(d) for d in existing) + 1:0{width}d}"
        self._issued.add(new)
        return new

    def near_miss(self, name: str) -> str:
        """A seeded one-character edit of `name` (substitute, delete or swap
        neighbours) that is not an id in the log — a realistic typo. Coded
        ids (containing digits, e.g. ``o-990001``) only get a digit
        substituted, so the result keeps the id pattern."""
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
        digits = [j for j, c in enumerate(name) if c.isdigit()]
        for _ in range(200):
            if digits:
                i, op = self.rng.choice(digits), "sub"
            else:
                i = self.rng.randrange(len(name))
                op = self.rng.choice(("sub", "del", "swap"))
            if op == "sub":
                c = name[i]
                pool = (
                    "0123456789" if c.isdigit()
                    else alphabet[:26].upper() if c.isupper()
                    else alphabet[:26]
                )
                cand = name[:i] + self.rng.choice(pool.replace(c, "") or alphabet) + name[i + 1:]
            elif op == "del" and len(name) > 2:
                cand = name[:i] + name[i + 1:]
            elif op == "swap" and i + 1 < len(name) and name[i] != name[i + 1]:
                cand = name[:i] + name[i + 1] + name[i] + name[i + 2:]
            else:
                continue
            if cand != name and not self._exists(cand):
                self._issued.add(cand)
                return cand
        raise ValueError(f"Could not find a free near-miss for {name!r}")


def _touch_low_fanout(picker: TargetPicker, ids: list[str]) -> None:
    """Mark the neighbours of a corrupted relation or deletion as touched,
    so a later injection can't alter how its rows are detected. Objects
    referenced by more than MAX_DELETE_FANOUT E2O rows (the few shared
    products, employees, customers) are left free, as they can't be
    deleted and touching them would starve the object injectors. Event ids
    have no such references and are always touched."""
    for i in set(ids):
        (n,) = picker.conn.execute(
            "SELECT COUNT(*) FROM event_object WHERE ocel_object_id = ?", (i,)
        ).fetchone()
        if n <= MAX_DELETE_FANOUT:
            picker.touch(i)


def _default_dst_for_level(level: str) -> str:
    if level == "all":
        return DEFAULT_FULL_PATH
    return str(_DATA_DIR / "synthetic" / f"order-management-{level}.sqlite")


def _remove_object_primary_key(conn: sqlite3.Connection) -> None:
    """Recreate the object table without PRIMARY KEY so duplicate ocel_id rows
    can be inserted (needed by every duplicate_objects_on_ids flavor).
    Idempotent: no-op if the PK is already absent."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='object'"
    ).fetchone()
    if row is None or "PRIMARY KEY" not in (row[0] or "").upper():
        return
    conn.execute("CREATE TABLE object_tmp (ocel_id TEXT, ocel_type TEXT)")
    conn.execute("INSERT INTO object_tmp SELECT ocel_id, ocel_type FROM object")
    conn.execute("DROP TABLE object")
    conn.execute("ALTER TABLE object_tmp RENAME TO object")


def _remove_event_primary_key(conn: sqlite3.Connection) -> None:
    """Recreate the event table without PRIMARY KEY so duplicate ocel_id rows
    can be inserted (needed by duplicate_events_on_ids). Idempotent: no-op if
    the PK is already absent."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='event'"
    ).fetchone()
    if row is None or "PRIMARY KEY" not in (row[0] or "").upper():
        return
    conn.execute("CREATE TABLE event_tmp (ocel_id TEXT, ocel_type TEXT)")
    conn.execute("INSERT INTO event_tmp SELECT ocel_id, ocel_type FROM event")
    conn.execute("DROP TABLE event")
    conn.execute("ALTER TABLE event_tmp RENAME TO event")


def _remove_event_object_primary_key(conn: sqlite3.Connection) -> None:
    """Recreate event_object without its composite PRIMARY KEY
    (event_id, object_id, qualifier) so duplicate triples can be inserted
    (needed by duplicate_e2o_relations). Idempotent."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='event_object'"
    ).fetchone()
    if row is None or "PRIMARY KEY" not in (row[0] or "").upper():
        return
    conn.execute(
        "CREATE TABLE event_object_tmp "
        "(ocel_event_id TEXT, ocel_object_id TEXT, ocel_qualifier TEXT)"
    )
    conn.execute(
        "INSERT INTO event_object_tmp "
        "SELECT ocel_event_id, ocel_object_id, ocel_qualifier FROM event_object"
    )
    conn.execute("DROP TABLE event_object")
    conn.execute("ALTER TABLE event_object_tmp RENAME TO event_object")


def _remove_object_object_primary_key(conn: sqlite3.Connection) -> None:
    """Recreate object_object without its composite PRIMARY KEY
    (source_id, target_id, qualifier) so duplicate triples can be inserted
    (needed by duplicate_o2o_relations). Idempotent."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='object_object'"
    ).fetchone()
    if row is None or "PRIMARY KEY" not in (row[0] or "").upper():
        return
    conn.execute(
        "CREATE TABLE object_object_tmp "
        "(ocel_source_id TEXT, ocel_target_id TEXT, ocel_qualifier TEXT)"
    )
    conn.execute(
        "INSERT INTO object_object_tmp "
        "SELECT ocel_source_id, ocel_target_id, ocel_qualifier FROM object_object"
    )
    conn.execute("DROP TABLE object_object")
    conn.execute("ALTER TABLE object_object_tmp RENAME TO object_object")


def _remove_all_primary_keys(conn: sqlite3.Connection) -> None:
    """Relax PK constraints on all four OCEL2 spine tables (object, event,
    event_object, object_object) so every issue type -- including the
    duplicate_* flavors on ids and on relations -- can be injected via a
    plain INSERT. Idempotent; safe to call multiple times per connection."""
    _remove_object_primary_key(conn)
    _remove_event_primary_key(conn)
    _remove_event_object_primary_key(conn)
    _remove_object_object_primary_key(conn)


def _null_type_for(
    conn: sqlite3.Connection, ocel_id: str, *, set_to: str | None
) -> str | None:
    n = conn.execute(
        "UPDATE object SET ocel_type = ? WHERE ocel_id = ? AND ocel_type IS NOT NULL "
        "AND (ocel_type != '' OR ? IS NULL)",
        (set_to, ocel_id, set_to),
    ).rowcount
    return ocel_id if n > 0 else None


def _capture_and_update(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    ocel_id: str,
    new_value: object,
    *,
    id_col: str = "ocel_id",
    where_initial: bool = False,
) -> object:
    """Read `column`'s current value for `ocel_id` in `table`, overwrite it
    with `new_value`, and return the value that was there before.

    Centralizes the "capture pre-corruption value, then corrupt" pattern so
    injectors can report `original_values` in their return dict instead of
    just the affected id.

    With `where_initial=True`, only the initial-state row
    (``ocel_changed_field IS NULL``) is read and written, leaving the
    change history intact. Tables without that column are unaffected.
    """
    where = f"{id_col} = ?"
    if where_initial and _has_column(conn, table, "ocel_changed_field"):
        where += " AND ocel_changed_field IS NULL"
    row = conn.execute(
        f'SELECT "{column}" FROM "{table}" WHERE {where}', (ocel_id,)
    ).fetchone()
    old_value = row[0] if row else None
    conn.execute(
        f'UPDATE "{table}" SET "{column}" = ? WHERE {where}',
        (new_value, ocel_id),
    )
    return old_value


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f'PRAGMA table_info("{table}")'))


def _rows_as_dicts(cur: sqlite3.Cursor) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _type_table(conn: sqlite3.Connection, kind: str, ocel_type: str | None) -> str | None:
    """`object_<Type>` / `event_<Type>` for an ocel_type, via the map tables."""
    if ocel_type is None:
        return None
    row = conn.execute(
        f"SELECT ocel_type_map FROM {kind}_map_type WHERE ocel_type = ?", (ocel_type,)
    ).fetchone()
    return f"{kind}_{row[0]}" if row else None


def _delete_fully(conn: sqlite3.Connection, kind: str, ocel_id: str) -> dict:
    spine = _rows_as_dicts(conn.execute(f"SELECT * FROM {kind} WHERE ocel_id = ?", (ocel_id,)))
    if not spine:
        raise ValueError(f"No {kind} {ocel_id!r} to delete")
    table = _type_table(conn, kind, spine[0]["ocel_type"])
    sub = (
        _rows_as_dicts(conn.execute(f'SELECT * FROM "{table}" WHERE ocel_id = ?', (ocel_id,)))
        if table else []
    )
    if kind == "object":
        e2o = _rows_as_dicts(conn.execute(
            "SELECT * FROM event_object WHERE ocel_object_id = ?", (ocel_id,)))
        o2o = _rows_as_dicts(conn.execute(
            "SELECT * FROM object_object WHERE ocel_source_id = ? OR ocel_target_id = ?",
            (ocel_id, ocel_id)))
    else:
        e2o = _rows_as_dicts(conn.execute(
            "SELECT * FROM event_object WHERE ocel_event_id = ?", (ocel_id,)))
        o2o = []
    conn.execute(f"DELETE FROM {kind} WHERE ocel_id = ?", (ocel_id,))
    if table:
        conn.execute(f'DELETE FROM "{table}" WHERE ocel_id = ?', (ocel_id,))
    return {kind: spine, "table": table, "sub": sub, "e2o": e2o, "o2o": o2o}


def _delete_object_fully(conn: sqlite3.Connection, ocel_id: str) -> dict:
    """Delete an object from `object` and its `object_<Type>` rows (all
    change-history rows). E2O/O2O rows are left in place — they become the
    declared cascade — and returned alongside the removed rows:
    ``{"object", "table", "sub", "e2o", "o2o"}``."""
    return _delete_fully(conn, "object", ocel_id)


def _delete_event_fully(conn: sqlite3.Connection, ocel_id: str) -> dict:
    """Delete an event from `event` and its `event_<Type>` row. E2O rows are
    left in place and returned: ``{"event", "table", "sub", "e2o", "o2o"}``."""
    return _delete_fully(conn, "event", ocel_id)


def _clone_object_row(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    clone_id: str,
    ocel_type: str,
    table: str,
) -> str | None:
    # Check if ocel_changed_field column exists
    has_changed_field = conn.execute(
        f"SELECT COUNT(*) FROM pragma_table_info('{table}') WHERE name = 'ocel_changed_field'",
    ).fetchone()[0]
    where_clause = (
        "WHERE ocel_id = ? AND ocel_changed_field IS NULL"
        if has_changed_field
        else "WHERE ocel_id = ?"
    )
    row = conn.execute(
        f'SELECT * FROM "{table}" {where_clause} LIMIT 1',
        (source_id,),
    ).fetchone()
    if row is None:
        return None
    cols = [
        desc[0]
        for desc in conn.execute(f'SELECT * FROM "{table}" LIMIT 0').description
    ]
    conn.execute("INSERT INTO object VALUES (?, ?)", (clone_id, ocel_type))
    placeholders = ", ".join(["?"] * len(cols))
    conn.execute(
        f'INSERT INTO "{table}" VALUES ({placeholders})',
        (clone_id, *row[1:]),
    )
    return clone_id


def inject_missing_attribute_value(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    *,
    count: int = 1,
    ocel_ids: list[str] | None = None,
    changed_field_col: str = "ocel_changed_field",
) -> list[str]:
    """Missing attribute value: NULL `column` in `count` initial-state rows of `table`.

    Skips change-delta rows (where ``ocel_changed_field IS NOT NULL``) so the
    resulting NULLs are genuine missing values.
    """
    has_changed_field = conn.execute(
        f"SELECT COUNT(*) FROM pragma_table_info('{table}') WHERE name = ?",
        (changed_field_col,),
    ).fetchone()[0]
    where = (
        f'WHERE "{changed_field_col}" IS NULL AND "{column}" IS NOT NULL'
        if has_changed_field
        else f'WHERE "{column}" IS NOT NULL'
    )
    if ocel_ids:
        placeholders = ", ".join("?" * len(ocel_ids))
        rows = conn.execute(
            f'SELECT ocel_id FROM "{table}" {where} AND ocel_id IN ({placeholders})',
            ocel_ids,
        ).fetchall()
    else:
        rows = conn.execute(
            f'SELECT ocel_id FROM "{table}" {where} LIMIT ?', (count,)
        ).fetchall()
    affected = [r[0] for r in rows]
    for ocel_id in affected:
        conn.execute(f'UPDATE "{table}" SET "{column}" = NULL WHERE ocel_id = ?', (ocel_id,))
    return affected
