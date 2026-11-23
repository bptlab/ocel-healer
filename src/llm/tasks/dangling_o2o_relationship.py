from src.llm.actions import ActionResult, relation_swap_target
from src.llm.schemas import InferredReferentOutput
from src.llm.tasks._base import ResolutionTask
from src.llm.tasks._dangling import dangling_rule, missing_sides, side_candidates


_O2O_SIDES = {
    "source": {"column": "ocel_source_id", "pk": ["ocel_target_id", "ocel_qualifier"],
               "kind": "object"},
    "target": {"column": "ocel_target_id", "pk": ["ocel_source_id", "ocel_qualifier"],
               "kind": "object"},
}
_O2O_PK = ["ocel_source_id", "ocel_target_id", "ocel_qualifier"]


class DanglingO2ORelationship(ResolutionTask):
    issue_key = "dangling_o2o_relationship"
    family = "relation"
    OutputModel = InferredReferentOutput

    TASK = """
        An `object_object` relation references an object that does not exist
        in the `object` table, most likely through a typo. Pick the intended
        referent for the missing end from the candidate list.
    """

    INPUTS = """
        - violation.missing_side    'source' or 'target' — which end is missing
        - violation.ocel_qualifier  names the relationship (often implies
                                    the type of the missing end)
        - object                    the known end's id, type, attributes
        - candidate_objects         up to 10 {ocel_id, ocel_type,
                                    similarity}: the existing ids closest
                                    to the dangling one, best first
    """

    METHOD = """
        1. Use `violation.ocel_qualifier` together with the known end's
           type to infer the expected `ocel_type` of the missing end
           (e.g. a `type A` source under qualifier `q1` always points to
           `type B` targets).
        2. Filter `candidate_objects` to entries whose type matches.
        3. Among survivors, prefer the id that differs from the dangling
           id by a plausible typo (one changed, missing, doubled or swapped
           character).
        4. Return one `ocel_id` from `candidate_objects` verbatim, or null
           when no candidate plausibly matches.
    """

    EXAMPLES = """
        violation.missing_side = 'target'
        violation.ocel_target_id = 'b-4422'
        violation.ocel_qualifier = 'q1'
        object = {ocel_id: 'a-42', ocel_type: 'type A'}
        candidate_objects = [{ocel_id: 'b-422', ocel_type: 'type B', similarity: 0.89},
                             {ocel_id: 'c-4422', ocel_type: 'type C', similarity: 0.83}]
        → {"inferred_referent": "b-422",
           "rationale": "'q1' from a type A object points to type B; 'b-422' differs from 'b-4422' by one doubled character",
           "confidence": 0.85}
    """

    def anchor(self, row: dict) -> tuple[str | None, str | None]:
        # The known side is whichever end isn't missing.
        if row.get("missing_side") == "source":
            return row.get("ocel_target_id"), row.get("target_type")
        return row.get("ocel_source_id"), row.get("source_type")

    def rule_action(self, conn, row: dict) -> ActionResult | None:
        return dangling_rule(conn, row, table="object_object", sides=_O2O_SIDES, pk_cols=_O2O_PK)

    def extend_context(self, conn, ctx: dict, row: dict) -> None:
        # Qualifiers like 'belongs_to' don't name the missing end's type,
        # so the candidates are ranked by similarity alone.
        sides = missing_sides(row, _O2O_SIDES)
        if sides:
            ctx["candidate_objects"] = side_candidates(conn, row, _O2O_SIDES[sides[0]])

    def parse_payload(self, row: dict, payload: dict) -> ActionResult:
        new = payload.get("inferred_referent")
        if not new:
            reason = (payload.get("rationale") or "").strip() or "no reason provided"
            return ActionResult.decline(f"LLM declined to infer a referent: {reason}")
        target = relation_swap_target(row, table="object_object", sides=_O2O_SIDES)
        if target is None:
            return ActionResult.unrouted("Both ends of the O2O relation missing; cannot patch.")
        return ActionResult.update(
            target_table=target["target_table"],
            target_pk=target["target_pk"],
            column=target["column"],
            old_value=target["old_value"],
            new_value=new,
        )

    def suppressed_target(self, row: dict) -> dict | None:
        return relation_swap_target(row, table="object_object", sides=_O2O_SIDES)
