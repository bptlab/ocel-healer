"""One injection scenario per (issue key, difficulty) for the synthetic evaluation.

Each scenario injects exactly one issue into a clean copy of the evaluation
log (see `build_eval_log.py`). Issue keys follow the dashboard overview
(`mapping` in `src/dashboard.py`); merged cells are split into their keys.

Rule-detected scenarios are scored on the rule detector's output. For
LLM-detected scenarios the runner needs to know which sweep candidate is the
injected one: `llm_type` is the type the sweep is scoped to (the *injected*
type, e.g. the wrong type an object was moved to) and `attribute` narrows the
match for attribute-level candidates. The candidate's ocel_id comes from the
injector's `affected_ids`; schema-level scenarios (`missing_*_attribute`)
match on the type row and the dropped column instead.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from src.order_management_corruption import event_issues as ev
from src.order_management_corruption import object_issues as ob
from src.order_management_corruption import relation_issues as rel


@dataclass(frozen=True)
class Scenario:
    issue_key: str
    cell: str                 # "<row> / <column>" in the dashboard overview
    difficulty: str           # "easy" | "hard"
    inject: Callable[[sqlite3.Connection], Any]
    detection: str = "rule"   # "rule" | "llm"
    llm_type: str | None = None
    attribute: str | None = None

    @property
    def name(self) -> str:
        return f"{self.issue_key}/{self.difficulty}"


def _rule(key: str, cell: str, easy, hard) -> list[Scenario]:
    return [Scenario(key, cell, "easy", easy), Scenario(key, cell, "hard", hard)]


def _llm(key: str, cell: str, easy: tuple, hard: tuple) -> list[Scenario]:
    """`easy` / `hard` are (inject, llm_type, attribute)."""
    return [
        Scenario(key, cell, d, inj, "llm", llm_type, attr)
        for d, (inj, llm_type, attr) in (("easy", easy), ("hard", hard))
    ]


SCENARIOS: list[Scenario] = [
    # ── Missing Data / Events ──────────────────────────────────────────
    *_rule("missing_event", "Missing / Event",
           ev.inject_missing_event_place_order_easy,
           ev.inject_missing_event_bare_id_hard),
    *_rule("missing_event_type", "Missing / Event Type",
           ev.inject_missing_event_type_null_confirm_easy,
           ev.inject_missing_event_type_whitespace_package_hard),
    *_rule("missing_event_timestamp", "Missing / Event Time",
           ev.inject_missing_event_timestamp_null_place_order_easy,
           ev.inject_missing_event_timestamp_null_item_out_of_stock_hard),
    *_llm("missing_event_attribute", "Missing / Event Attribute",
          (ev.inject_missing_event_attribute_drop_amount_easy, "pay order", "amount"),
          (ev.inject_missing_event_attribute_drop_resource_hard, "pick item", "resource")),
    *_rule("missing_event_attribute_value", "Missing / Event Attribute Value",
           ev.inject_missing_event_attribute_value_null_resource_easy,
           ev.inject_missing_event_attribute_value_null_amount_hard),
    # ── Missing Data / Objects ─────────────────────────────────────────
    *_rule("missing_object", "Missing / Object",
           rel.inject_missing_object_order_easy,
           rel.inject_missing_object_package_hard),
    *_rule("missing_object_type", "Missing / Object Type",
           ob.inject_missing_object_type_null_employee,
           ob.inject_missing_object_type_whitespace_product),
    *_llm("missing_object_attribute", "Missing / Object Attribute",
          (ob.inject_missing_object_attribute_drop_price_easy, "orders", "price"),
          (ob.inject_missing_object_attribute_drop_weight_hard, "packages", "weight")),
    *_rule("missing_attribute_value", "Missing / Object Attribute Value",
           ob.inject_missing_attribute_value_null_product_weight,
           ob.inject_missing_attribute_value_null_order_price),
    # ── Missing Data / Relations ───────────────────────────────────────
    *_rule("dangling_o2o_relationship", "Missing / Object-to-Object",
           rel.inject_dangling_o2o_relationship_missing_source,
           rel.inject_dangling_o2o_relationship_missing_both_typo),
    *_rule("dangling_e2o_relationship", "Missing / Event-to-Object",
           rel.inject_dangling_e2o_relationship_missing_object_easy,
           rel.inject_dangling_e2o_relationship_missing_both),
    # ── Incorrect Data / Events ────────────────────────────────────────
    *_rule("duplicate_events_on_ids", "Incorrect / Event",
           ev.inject_duplicate_events_on_ids_easy,
           ev.inject_duplicate_events_on_ids_conflicting_types_hard),
    *_rule("duplicate_events_on_attributes", "Incorrect / Event",
           ev.inject_duplicate_events_on_attributes_clone_easy,
           ev.inject_duplicate_events_on_attributes_clone_with_refs_hard),
    *_llm("incorrect_event_type", "Incorrect / Event Type",
          (ev.inject_incorrect_event_type_swap_easy, "pick item", None),
          (ev.inject_incorrect_event_type_swap_related_hard, "pay order", None)),
    *_llm("incorrect_event_time", "Incorrect / Event Time",
          (ev.inject_incorrect_event_time_future_easy, "place order", None),
          (ev.inject_incorrect_event_time_past_hard, "confirm order", None)),
    *_rule("incorrect_event_attribute_datatype", "Incorrect / Event Attribute Type",
           ev.inject_incorrect_event_attribute_datatype_string_in_amount_easy,
           ev.inject_incorrect_event_attribute_datatype_blob_in_resource_hard),
    *_llm("incorrect_event_attribute_value", "Incorrect / Event Attribute Value",
          (ev.inject_incorrect_event_attribute_value_negative_amount_easy, "pay order", "amount"),
          (ev.inject_incorrect_event_attribute_value_wrong_resource_hard, "pick item", "resource")),
    # ── Incorrect Data / Objects ───────────────────────────────────────
    *_rule("duplicate_objects_on_ids", "Incorrect / Object",
           ob.inject_duplicate_objects_on_ids_product,
           ob.inject_duplicate_objects_on_ids_triple_conflicting_type),
    *_rule("duplicate_objects_on_attributes", "Incorrect / Object",
           ob.inject_duplicate_objects_on_attributes_clone_product,
           ob.inject_duplicate_objects_on_attributes_clone_order_and_referenced),
    *_llm("incorrect_object_type", "Incorrect / Object Type",
          (ob.inject_incorrect_object_type_swap_order_to_employee, "employees", None),
          (ob.inject_incorrect_object_type_swap_item_to_product, "products", None)),
    *_rule("incorrect_attribute_datatype", "Incorrect / Object Attribute Type",
           ob.inject_incorrect_attribute_datatype_string_in_weight,
           ob.inject_incorrect_attribute_datatype_blob_in_role),
    *_llm("incorrect_attribute_value", "Incorrect / Object Attribute Value",
          (ob.inject_incorrect_object_attribute_value_negative_weight_easy, "products", "weight"),
          (ob.inject_incorrect_object_attribute_value_implausible_price_hard, "products", "price")),
    # ── Incorrect Data / Relations ─────────────────────────────────────
    *_rule("duplicate_o2o_relations", "Incorrect / Object-to-Object",
           rel.inject_duplicate_o2o_relations_comprises_easy,
           rel.inject_duplicate_o2o_relations_sales_rep_hard),
    *_rule("o2o_self_loop", "Incorrect / Object-to-Object",
           rel.inject_o2o_self_loop_order_easy,
           rel.inject_o2o_self_loop_product_hard),
    *_rule("duplicate_e2o_relations", "Incorrect / Event-to-Object",
           rel.inject_duplicate_e2o_relations_order_easy,
           rel.inject_duplicate_e2o_relations_sales_person_hard),
]

SCHEMA_KEYS = {"missing_object_attribute", "missing_event_attribute"}
