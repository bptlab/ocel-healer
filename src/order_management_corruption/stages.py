"""Corruption entry point + tier stages.

`corrupt_database` copies the clean SQLite to the destination and runs the
selected corruption level. Each `_stage_*` function calls one injector per
issue in a fixed order — new event/relation issues are appended to the
existing object-side stage members so `easy` and `hard` grow at the
same time. Issues that need the evaluation log (event attributes) or change the
schema (missing attributes) are not staged; the evaluation injects them
one at a time (`src/evaluation/scenarios.py`).
"""

from __future__ import annotations

import shutil
import sqlite3
from typing import Callable

from src.order_management_corruption._common import (
    DEFAULT_CLEAN_PATH,
    DEFAULT_FULL_PATH,
    _default_dst_for_level,
    _remove_object_primary_key,
)
from src.order_management_corruption.event_issues import (
    inject_missing_event_bare_id_hard,
    inject_missing_event_place_order_easy,
    inject_missing_event_timestamp_null_item_out_of_stock_hard,
    inject_missing_event_timestamp_null_place_order_easy,
    inject_missing_event_type_null_confirm_easy,
    inject_missing_event_type_whitespace_package_hard,
)
from src.order_management_corruption.object_issues import (
    inject_duplicate_objects_on_attributes_clone_order_and_referenced,
    inject_duplicate_objects_on_attributes_clone_product,
    inject_duplicate_objects_on_ids_product,
    inject_duplicate_objects_on_ids_triple_conflicting_type,
    inject_incorrect_attribute_datatype_blob_in_role,
    inject_incorrect_attribute_datatype_string_in_weight,
    inject_incorrect_object_type_swap_item_to_product,
    inject_incorrect_object_type_swap_order_to_employee,
    inject_missing_attribute_value_null_order_price,
    inject_missing_attribute_value_null_product_weight,
    inject_missing_object_type_null_employee,
    inject_missing_object_type_whitespace_product,
)
from src.order_management_corruption.relation_issues import (
    inject_dangling_e2o_relationship_missing_both,
    inject_dangling_e2o_relationship_missing_object_easy,
    inject_dangling_o2o_relationship_missing_both_typo,
    inject_dangling_o2o_relationship_missing_source,
    inject_duplicate_e2o_relations_order_easy,
    inject_duplicate_e2o_relations_sales_person_hard,
    inject_duplicate_o2o_relations_comprises_easy,
    inject_duplicate_o2o_relations_sales_rep_hard,
    inject_missing_object_order_easy,
    inject_missing_object_package_hard,
    inject_o2o_self_loop_order_easy,
    inject_o2o_self_loop_product_hard,
)


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
        for stage in stages:
            stage(conn)
        conn.commit()
    return dst_path


def _stage_easy(conn: sqlite3.Connection) -> None:
    # Object-side
    inject_duplicate_objects_on_ids_product(conn)   # duplicate_objects_on_ids first (uses PK-less object table)
    inject_missing_object_type_null_employee(conn)  # then missing_object_type (on a disjoint id)
    inject_missing_attribute_value_null_product_weight(conn)
    inject_incorrect_object_type_swap_order_to_employee(conn)
    inject_incorrect_attribute_datatype_string_in_weight(conn)
    inject_duplicate_objects_on_attributes_clone_product(conn)
    inject_dangling_e2o_relationship_missing_object_easy(conn)
    inject_dangling_o2o_relationship_missing_source(conn)
    # Event-side (Missing Data row)
    inject_missing_event_timestamp_null_place_order_easy(conn)
    inject_missing_event_place_order_easy(conn)
    inject_missing_event_type_null_confirm_easy(conn)
    # Relation-borne missing object
    inject_missing_object_order_easy(conn)
    # Incorrect-Data row (Relations)
    inject_duplicate_o2o_relations_comprises_easy(conn)
    inject_o2o_self_loop_order_easy(conn)
    inject_duplicate_e2o_relations_order_easy(conn)


def _stage_hard(conn: sqlite3.Connection) -> None:
    # Object-side
    inject_duplicate_objects_on_ids_triple_conflicting_type(conn)
    inject_missing_object_type_whitespace_product(conn)
    inject_missing_attribute_value_null_order_price(conn)
    inject_incorrect_object_type_swap_item_to_product(conn)
    inject_incorrect_attribute_datatype_blob_in_role(conn)
    inject_duplicate_objects_on_attributes_clone_order_and_referenced(conn)
    inject_dangling_e2o_relationship_missing_both(conn)
    inject_dangling_o2o_relationship_missing_both_typo(conn)
    # Event-side (Missing Data row)
    inject_missing_event_timestamp_null_item_out_of_stock_hard(conn)
    inject_missing_event_bare_id_hard(conn)
    inject_missing_event_type_whitespace_package_hard(conn)
    # Relation-borne missing object
    inject_missing_object_package_hard(conn)
    # Incorrect-Data row (Relations)
    inject_duplicate_o2o_relations_sales_rep_hard(conn)
    inject_o2o_self_loop_product_hard(conn)
    inject_duplicate_e2o_relations_sales_person_hard(conn)


_LEVEL_STAGES: dict[str, list[Callable[[sqlite3.Connection], None]]] = {
    "easy":   [_stage_easy],
    "hard":   [_stage_hard],
    "all":    [_stage_easy, _stage_hard],
}


__all__ = [
    "corrupt_database",
    "DEFAULT_CLEAN_PATH",
    "DEFAULT_FULL_PATH",
]
