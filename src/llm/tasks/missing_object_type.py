from src.examination.hint_selector import all_type_summaries
from src.llm.actions import ActionResult
from src.llm.schemas import InferredTypeOutput
from src.llm.tasks._base import ResolutionTask


class MissingObjectType(ResolutionTask):
    issue_key = "missing_object_type"
    family = "type"
    OutputModel = InferredTypeOutput

    def select_hints(self, profile: dict, guide: dict | None, row: dict) -> dict:
        # The row has no usable object_type — the task needs the overview of
        # ALL types (what they represent + their id templates) to pick one.
        return {"all_object_types": all_type_summaries(profile, guide)}

    TASK = """
        An object row in the `object` table has a NULL or empty `ocel_type`.
        Infer the most likely type for this object from its id, events, and
        attributes. Pick exactly one value from `candidate_types`.
    """

    INPUTS = """
        - violation.ocel_id   the id of the object whose type is missing
        - object.attributes   the anchor object's existing attributes (may be empty)
        - events              up to 8 events touching this object, each with the
                              qualifier under which the event references it
        - candidate_types     the closed list of valid object types — the
                              answer must be one of these strings
    """

    METHOD = """
        1. Read `violation.ocel_id` first — an id prefix or keyword shared with
           the objects of one type, or a human name, is often the single
           strongest signal (e.g. `b-20017` when the ids of `type B` start
           with `b-`).
           If `examination_hints.all_object_types` is present, match the id
           against each type's `id_template` (digit runs shown as `#`): a
           matching template is strong evidence for that type.
        2. Read the qualifiers in `events`. The qualifier names the ROLE
           the object plays, not the event's subject: an object referenced
           under qualifier `q1` fills role `q1` in that event; it is not one
           of the event's other objects.
        3. Attribute names in `object.attributes` reinforce: prefer the type
           whose objects declare the same attributes.
        4. If several types fit, prefer the one whose name best matches the
           id and qualifiers.
    """

    EXAMPLES = """
        violation.ocel_id = 'b-20017'
        events = [{activity: 'E2', qualifier: 'b'},
                  {activity: 'E3', qualifier: 'b'}]
        candidate_types = ['type A', 'type B', 'type C']
        → {"inferred_type": "type B",
           "rationale": "the id prefix 'b-' and the qualifier 'b' on both events point to type B",
           "confidence": 0.9}
    """

    def parse_payload(self, row: dict, payload: dict) -> ActionResult:
        new = payload.get("inferred_type")
        if not new:
            reason = (payload.get("rationale") or "").strip() or "no reason provided"
            return ActionResult.decline(f"LLM declined to infer an object type: {reason}")
        return ActionResult.update(
            target_table="object",
            target_pk={"ocel_id": row["ocel_id"]},
            column="ocel_type",
            old_value=row.get("ocel_type"),
            new_value=new,
        )

    def suppressed_target(self, row: dict) -> dict | None:
        return {
            "target_table": "object",
            "target_pk": {"ocel_id": row.get("ocel_id")},
            "column": "ocel_type",
            "old_value": row.get("ocel_type"),
        }
