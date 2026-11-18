"""Corruption entry point + tier stages.

`corrupt_database` copies the clean SQLite to the destination and runs the
selected corruption level. Each `_stage_*` function runs every injector of
its tier from `INJECTORS` in a fixed order, sharing one `TargetPicker` so
targets never overlap, and returns the collected `Injection` records.
"""

from __future__ import annotations

import shutil
import sqlite3
from typing import Callable

from src.order_management_corruption._common import (
    DEFAULT_CLEAN_PATH,
    DEFAULT_FULL_PATH,
    Injection,
    TargetPicker,
    _default_dst_for_level,
    _remove_object_primary_key,
)
from src.order_management_corruption.event_issues import EVENT_INJECTORS
from src.order_management_corruption.object_issues import OBJECT_INJECTORS
from src.order_management_corruption.relation_issues import RELATION_INJECTORS
from src.order_management_corruption.schema_issues import SCHEMA_INJECTORS

# Every flavor, keyed by (issue_key, tier). Order is the call order within
# a stage; schema drops come last, and only drop columns no other injector
# touches, so later stages never read a dropped column.
INJECTORS: dict[tuple[str, str], Callable[[sqlite3.Connection, TargetPicker], list[Injection]]] = {
    **OBJECT_INJECTORS,
    **EVENT_INJECTORS,
    **RELATION_INJECTORS,
    **SCHEMA_INJECTORS,
}


def corrupt_database(
    src_path: str,
    dst_path: str | None = None,
    *,
    level: str = "all",
) -> str:
    """Copy `src_path` to `dst_path` and inject corruptions for `level`.

    When `dst_path` is None, the default per-level location under ``data/`` is
    used. Returns the resolved destination path.
    """
    dst_path = dst_path or _default_dst_for_level(level)
    shutil.copy2(src_path, dst_path)
    stages = _LEVEL_STAGES.get(level)
    if stages is None:
        raise ValueError(
            f"Unknown corruption level {level!r}; expected one of "
            f"{sorted(_LEVEL_STAGES)}"
        )
    with sqlite3.connect(dst_path) as conn:
        _remove_object_primary_key(conn)
        picker = TargetPicker(conn, seed=0, n=1)
        injections: list[Injection] = []
        for stage in stages:
            injections += stage(conn, picker)
        conn.commit()
    return dst_path


def _stage(tier: str) -> Callable[[sqlite3.Connection, TargetPicker], list[Injection]]:
    """A stage running every registered injector of `tier`, in registry order."""
    def run(conn: sqlite3.Connection, picker: TargetPicker) -> list[Injection]:
        out: list[Injection] = []
        for (_, t), inject in INJECTORS.items():
            if t == tier:
                out += inject(conn, picker)
        return out
    return run


_stage_easy, _stage_medium, _stage_hard = _stage("easy"), _stage("medium"), _stage("hard")


_LEVEL_STAGES: dict[str, list[Callable[[sqlite3.Connection, TargetPicker], list[Injection]]]] = {
    "easy":   [_stage_easy],
    "medium": [_stage_medium],
    "hard":   [_stage_hard],
    "all":    [_stage_easy, _stage_medium, _stage_hard],
}


__all__ = [
    "corrupt_database",
    "DEFAULT_CLEAN_PATH",
    "DEFAULT_FULL_PATH",
]
