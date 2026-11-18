"""Corruption entry point + tier stages.

`corrupt_database` copies the clean SQLite to the destination and runs the
selected corruption level. Each `_stage_*` function calls one injector per
issue in a fixed order, sharing one `TargetPicker` so targets never overlap,
and returns the collected `Injection` records.
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
from src.order_management_corruption.event_issues import (
    inject_missing_event_failed_delivery_hard,
    inject_missing_event_pick_item_medium,
    inject_missing_event_place_order_easy,
    inject_missing_event_timestamp_empty_pick_item_medium,
    inject_missing_event_timestamp_null_item_out_of_stock_hard,
    inject_missing_event_timestamp_null_place_order_easy,
    inject_missing_event_type_empty_pay_medium,
    inject_missing_event_type_null_confirm_easy,
    inject_missing_event_type_whitespace_package_hard,
)
from src.order_management_corruption.object_issues import (
    inject_duplicate_objects_on_attributes_clone_employee,
    inject_duplicate_objects_on_attributes_clone_order_and_referenced,
    inject_duplicate_objects_on_attributes_clone_product,
    inject_duplicate_objects_on_ids_conflicting_types,
    inject_duplicate_objects_on_ids_product,
    inject_duplicate_objects_on_ids_triple_null_type,
    inject_incorrect_attribute_datatype_blob_in_role,
    inject_incorrect_attribute_datatype_string_in_order_price,
    inject_incorrect_attribute_datatype_string_in_weight,
    inject_incorrect_object_type_item_to_product,
    inject_incorrect_object_type_order_to_employee,
    inject_incorrect_object_type_package_to_item,
    inject_missing_attribute_value_empty_string_role,
    inject_missing_attribute_value_null_order_price,
    inject_missing_attribute_value_null_product_weight,
    inject_missing_object_type_empty_string_order,
    inject_missing_object_type_null_employee,
    inject_missing_object_type_whitespace_product,
)
from src.order_management_corruption.relation_issues import (
    inject_dangling_e2o_relationship_missing_both,
    inject_dangling_e2o_relationship_missing_event,
    inject_dangling_e2o_relationship_missing_object_easy,
    inject_dangling_o2o_relationship_missing_both_typo,
    inject_dangling_o2o_relationship_missing_source,
    inject_dangling_o2o_relationship_missing_target,
    inject_duplicate_e2o_relations_item_medium,
    inject_duplicate_e2o_relations_order_easy,
    inject_duplicate_e2o_relations_sales_person_hard,
    inject_duplicate_o2o_relations_comprises_easy,
    inject_duplicate_o2o_relations_places_medium,
    inject_duplicate_o2o_relations_sales_rep_hard,
    inject_missing_object_item_medium,
    inject_missing_object_order_easy,
    inject_missing_object_package_hard,
    inject_o2o_self_loop_employee_medium,
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
        picker = TargetPicker(conn, seed=0, n=1)
        injections: list[Injection] = []
        for stage in stages:
            injections += stage(conn, picker)
        conn.commit()
    return dst_path


def _stage_easy(conn: sqlite3.Connection, picker: TargetPicker) -> list[Injection]:
    out: list[Injection] = []
    # Object-side
    out += inject_duplicate_objects_on_ids_product(conn, picker)
    out += inject_missing_object_type_null_employee(conn, picker)
    out += inject_missing_attribute_value_null_product_weight(conn, picker)
    out += inject_incorrect_object_type_order_to_employee(conn, picker)
    out += inject_incorrect_attribute_datatype_string_in_weight(conn, picker)
    out += inject_duplicate_objects_on_attributes_clone_product(conn, picker)
    out += inject_dangling_e2o_relationship_missing_object_easy(conn, picker)
    out += inject_dangling_o2o_relationship_missing_source(conn, picker)
    # Event-side (Missing Data row)
    out += inject_missing_event_timestamp_null_place_order_easy(conn, picker)
    out += inject_missing_event_place_order_easy(conn, picker)
    out += inject_missing_event_type_null_confirm_easy(conn, picker)
    # Relation-borne missing object
    out += inject_missing_object_order_easy(conn, picker)
    # Incorrect-Data row (Relations)
    out += inject_duplicate_o2o_relations_comprises_easy(conn, picker)
    out += inject_o2o_self_loop_order_easy(conn, picker)
    out += inject_duplicate_e2o_relations_order_easy(conn, picker)
    return out


def _stage_medium(conn: sqlite3.Connection, picker: TargetPicker) -> list[Injection]:
    out: list[Injection] = []
    # Object-side
    out += inject_duplicate_objects_on_ids_conflicting_types(conn, picker)
    out += inject_missing_object_type_empty_string_order(conn, picker)
    out += inject_missing_attribute_value_empty_string_role(conn, picker)
    out += inject_incorrect_object_type_item_to_product(conn, picker)
    out += inject_incorrect_attribute_datatype_string_in_order_price(conn, picker)
    out += inject_duplicate_objects_on_attributes_clone_employee(conn, picker)
    out += inject_dangling_e2o_relationship_missing_event(conn, picker)
    out += inject_dangling_o2o_relationship_missing_target(conn, picker)
    # Event-side (Missing Data row)
    out += inject_missing_event_timestamp_empty_pick_item_medium(conn, picker)
    out += inject_missing_event_pick_item_medium(conn, picker)
    out += inject_missing_event_type_empty_pay_medium(conn, picker)
    # Relation-borne missing object
    out += inject_missing_object_item_medium(conn, picker)
    # Incorrect-Data row (Relations)
    out += inject_duplicate_o2o_relations_places_medium(conn, picker)
    out += inject_o2o_self_loop_employee_medium(conn, picker)
    out += inject_duplicate_e2o_relations_item_medium(conn, picker)
    return out


def _stage_hard(conn: sqlite3.Connection, picker: TargetPicker) -> list[Injection]:
    out: list[Injection] = []
    # Object-side
    out += inject_duplicate_objects_on_ids_triple_null_type(conn, picker)
    out += inject_missing_object_type_whitespace_product(conn, picker)
    out += inject_missing_attribute_value_null_order_price(conn, picker)
    out += inject_incorrect_object_type_package_to_item(conn, picker)
    out += inject_incorrect_attribute_datatype_blob_in_role(conn, picker)
    out += inject_duplicate_objects_on_attributes_clone_order_and_referenced(conn, picker)
    out += inject_dangling_e2o_relationship_missing_both(conn, picker)
    out += inject_dangling_o2o_relationship_missing_both_typo(conn, picker)
    # Event-side (Missing Data row)
    out += inject_missing_event_timestamp_null_item_out_of_stock_hard(conn, picker)
    out += inject_missing_event_failed_delivery_hard(conn, picker)
    out += inject_missing_event_type_whitespace_package_hard(conn, picker)
    # Relation-borne missing object
    out += inject_missing_object_package_hard(conn, picker)
    # Incorrect-Data row (Relations)
    out += inject_duplicate_o2o_relations_sales_rep_hard(conn, picker)
    out += inject_o2o_self_loop_product_hard(conn, picker)
    out += inject_duplicate_e2o_relations_sales_person_hard(conn, picker)
    return out


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
