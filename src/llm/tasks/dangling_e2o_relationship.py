from src.llm.actions import ActionResult, relation_swap_target
from src.llm.schemas import InferredReferentOutput
from src.llm.tasks._base import ResolutionTask
from src.llm.tasks._dangling import dangling_rule, missing_sides, side_candidates


_E2O_SIDES = {
    "event": {"column": "ocel_event_id", "pk": ["ocel_object_id", "ocel_qualifier"],
              "kind": "event", "context_column": "ocel_object_id"},
    "object": {"column": "ocel_object_id", "pk": ["ocel_event_id", "ocel_qualifier"],
               "kind": "object", "typed_by_qualifier": True,
               "context_column": "ocel_event_id"},
}
_E2O_PK = ["ocel_event_id", "ocel_object_id", "ocel_qualifier"]


class DanglingE2ORelationship(ResolutionTask):
    issue_key = "dangling_e2o_relationship"
    family = "relation"
    OutputModel = InferredReferentOutput

    TASK = """
        An `event_object` relation references an event or object that does
        not exist, most likely through a typo. Pick the intended referent
        for the missing side from the candidate list.
    """

    INPUTS = """
        - violation.missing_side       'object' or 'event' — which end is missing
        - violation.ocel_qualifier     names the relationship (usually
                                       implies the expected type of the
                                       missing side)
        - object                       the known end's id, type, attributes
        - events                       up to 8 events touching the known
                                       object (context only)
        - candidate_objects            up to 10 {ocel_id, ocel_type,
                                       similarity}: the existing ids closest
                                       to the dangling one, best first — set
                                       when missing_side='object', narrowed
                                       to the qualifier's type when it names one
        - candidate_events             the same for missing_side='event'
    """

    METHOD = """
        1. Read `violation.ocel_qualifier` — it usually names a role that
           pins the expected `ocel_type` of the missing side (e.g.
           qualifier `customer` selects `ocel_type='customer'`).
        2. Filter the candidate list to entries whose `ocel_type` matches.
        3. Among survivors, prefer the id that differs from the dangling
           id by a plausible typo (one changed, missing, doubled or swapped
           character) and fits the known end (e.g. `pick_i-880001` goes
           with item `i-880001`).
        4. Return one `ocel_id` from the candidate list verbatim, or null
           when no candidate plausibly matches.
    """

    EXAMPLES = """
        violation.missing_side = 'object'
        violation.ocel_qualifier = 'customer'
        object = {ocel_id: 'e-42', ocel_type: 'place_order'}
        candidate_objects = [{ocel_id: 'c-1', ocel_type: 'customer'},
                             {ocel_id: 'p-9', ocel_type: 'product'}]
        → {"inferred_referent": "c-1",
           "rationale": "qualifier 'customer' selects ocel_type='customer'; only c-1 matches",
           "confidence": 0.9}
    """

    def rule_action(self, conn, row: dict) -> ActionResult | None:
        return dangling_rule(conn, row, table="event_object", sides=_E2O_SIDES, pk_cols=_E2O_PK)

    def extend_context(self, conn, ctx: dict, row: dict) -> None:
        for side in missing_sides(row, _E2O_SIDES):
            ctx[f"candidate_{side}s"] = side_candidates(conn, row, _E2O_SIDES[side])

    def parse_payload(self, row: dict, payload: dict) -> ActionResult:
        new = payload.get("inferred_referent")
        if not new:
            reason = (payload.get("rationale") or "").strip() or "no reason provided"
            return ActionResult.decline(f"LLM declined to infer a referent: {reason}")
        target = relation_swap_target(row, table="event_object", sides=_E2O_SIDES)
        if target is None:
            return ActionResult.unrouted("Both ends of the E2O relation missing; cannot patch.")
        return ActionResult.update(
            target_table=target["target_table"],
            target_pk=target["target_pk"],
            column=target["column"],
            old_value=target["old_value"],
            new_value=new,
        )

    def suppressed_target(self, row: dict) -> dict | None:
        return relation_swap_target(row, table="event_object", sides=_E2O_SIDES)
