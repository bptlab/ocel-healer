from src.examination.hint_selector import all_type_summaries
from src.llm.schemas import InferredTypeOutput
from src.llm.tasks._base import DetectionResult, DetectionTask


class IncorrectObjectType(DetectionTask):
    issue_key = "incorrect_object_type"
    family = "type"
    OutputModel = InferredTypeOutput

    def select_hints(self, profile: dict, guide: dict | None, row: dict) -> dict:
        # The current type is under suspicion — validating it needs the
        # overview of ALL types and their id templates, not just its own slice.
        return {"all_object_types": all_type_summaries(profile, guide)}

    DETECT_TASK = """
        An object row has a non-empty `ocel_type`, but that type may be incorrect.
        Decide only WHETHER the current type is wrong, given the object's id, the
        events touching it, and its attribute row. Do not propose a replacement:
        that is a separate step.
    """

    DETECT_METHOD = """
        1. Compare the current `ocel_type` against these signals, most to least
           informative:
             - The `ocel_id`. An id keyword or prefix that names another type
               (e.g. `typeB-124` currently typed `type A`) is STRONG on its own
               — do NOT downgrade it to "weak" just because events and
               attributes are sparse.
             - If `examination_hints.all_object_types` is present, match the id
               against each type's `id_template` (digit runs shown as `#`): an id
               matching another type's template is strong evidence.
             - Event activities + qualifiers: the qualifier names the role the
               object plays (e.g. qualifier `b` on every event suggests `type B`).
             - Attribute names / shapes: attributes that only another type
               declares suggest that type.
        2. `is_violation: true` when this evidence contradicts the current
           type, even if you are not sure which type is right.
        3. `is_violation: false` when the evidence is consistent with the current
           type, or when the id is opaque AND events and attributes are absent or
           neutral.
    """

    DETECT_EXAMPLES = """
        violation = {ocel_id: 'X42', ocel_type: 'type A'}
        events = [{activity: 'E1', qualifier: 'b'},
                  {activity: 'E2', qualifier: 'b'}]
        candidate_types = ['type A', 'type B', 'type C']
        → {"is_violation": true,
           "rationale": "both events reference this object under qualifier 'b', the role of a type B object, contradicting 'type A'",
           "confidence": 0.9}

        violation = {ocel_id: 'X7', ocel_type: 'type C'}
        events = [{activity: 'E1', qualifier: 'c'}]
        candidate_types = ['type A', 'type B', 'type C']
        → {"is_violation": false,
           "rationale": "the event qualifies this object as 'c', matching the current type",
           "confidence": 0.95}
    """

    TASK = """
        This object's `ocel_type` has been judged incorrect. Infer the correct
        type from `candidate_types`, using the object's id, the events touching
        it, and its attribute row.
    """

    INPUTS = """
        - violation.ocel_id       the id of the object being checked
        - violation.ocel_type     the object's CURRENT type (the value to validate)
        - object.attributes       the anchor's existing attributes (may be empty)
        - events                  up to 8 events touching this object, each with
                                  the qualifier under which the event references it
        - candidate_types         the closed list of valid object types
    """

    METHOD = """
        1. Weigh the signals, most to least informative: the `ocel_id` (an id
           keyword or prefix naming a type, or a matching `id_template` in
           `examination_hints.all_object_types`), the qualifiers under which
           events reference the object (the qualifier names its role), and its
           attribute names.
        2. Return the one value from `candidate_types` that best fits the
           evidence. It must differ from the current `ocel_type`.
        3. Return `inferred_type: null` only when no other candidate fits
           better than the current type.
    """

    EXAMPLES = """
        violation = {ocel_id: 'X42', ocel_type: 'type A'}
        events = [{activity: 'E1', qualifier: 'b'},
                  {activity: 'E2', qualifier: 'b'}]
        candidate_types = ['type A', 'type B', 'type C']
        → {"inferred_type": "type B",
           "rationale": "both events reference this object under qualifier 'b', the role of a type B object",
           "confidence": 0.9}

        violation = {ocel_id: 'typeB-124', ocel_type: 'type C'}
        events = []
        candidate_types = ['type A', 'type B', 'type C']
        → {"inferred_type": "type B",
           "rationale": "id 'typeB-124' names type B",
           "confidence": 0.85}
    """

    def parse_repair(self, row: dict, payload: dict) -> DetectionResult:
        rationale = str(payload.get("rationale", "") or "").strip()
        confidence = float(payload.get("confidence", 0.0) or 0.0)
        suggested = payload.get("inferred_type")
        # Null / empty / "same as current" → not flagged.
        if not suggested or suggested == row.get("ocel_type"):
            return DetectionResult(
                flagged=False, rationale=rationale, confidence=confidence,
                suggested_value=None,
            )
        return DetectionResult(
            flagged=True, rationale=rationale, confidence=confidence,
            suggested_value=suggested,
        )

    def suppressed_target(self, row: dict) -> dict | None:
        return {
            "target_table": "object",
            "target_pk": {"ocel_id": row.get("ocel_id")},
            "column": "ocel_type",
            "old_value": row.get("ocel_type"),
        }
