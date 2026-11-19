from src.detection.error_detection import _event_type_tables
from src.llm.schemas import InferredTypeOutput
from src.llm.tasks._base import DetectionResult, DetectionTask


class IncorrectEventType(DetectionTask):
    """Event-side mirror of :class:`IncorrectObjectType`.

    Checks whether an event's ``ocel_type`` is consistent with:
      - the activity keyword in its ``ocel_id`` (if any),
      - the types of the objects it touches through ``event_object``,
      - the qualifiers under which it references those objects.

    Flags a mismatch only when the evidence points to a different type from
    ``candidate_types``; otherwise returns ``inferred_type: null``.
    """

    issue_key = "incorrect_event_type"
    family = "type"
    kind = "event"
    OutputModel = InferredTypeOutput

    DETECT_TASK = """
        An event row has a non-empty `ocel_type`, but that type may be incorrect.
        Decide only WHETHER the current type is wrong, given the event's id, the
        objects it touches, and the qualifiers it uses. Do not propose a
        replacement: that is a separate step.
    """

    DETECT_METHOD = """
        1. Compare the current `ocel_type` against these signals, most to
           least informative:
             - The `ocel_id`. An activity keyword embedded in the id (e.g.
               `E2:1001` or `e2_x-1001` for an `E2` event) that contradicts
               the current type is STRONG on its own — do NOT downgrade it
               to "weak" just because touched objects are sparse.
             - The touched objects' types + qualifiers in `related_objects`:
               an event whose objects and roles match those of another
               event type likely belongs to that type.
             - The event's own attribute row.
        2. `is_violation: true` when this evidence contradicts the current
           type, even if you are not sure which type is right.
        3. `is_violation: false` when the evidence is consistent with the
           current type, or when the id is opaque (UUID or numeric with no
           activity keyword) AND touched objects are absent or neutral.
    """

    DETECT_EXAMPLES = """
        violation = {ocel_id: 'E2:1010', ocel_type: 'E1'}
        related_objects = [{ocel_type: 'type A', ocel_qualifier: 'a'}]
        candidate_types = ['E1', 'E2', 'E3', 'E4']
        → {"is_violation": true,
           "rationale": "id keyword 'E2' contradicts the current 'E1' type",
           "confidence": 0.95}

        violation = {ocel_id: 'ev-7', ocel_type: 'E2'}
        related_objects = [{ocel_type: 'type A', ocel_qualifier: 'a'}]
        candidate_types = ['E1', 'E2', 'E3']
        → {"is_violation": false,
           "rationale": "id is opaque; the touched type A object is consistent with 'E2'",
           "confidence": 0.8}
    """

    TASK = """
        This event's `ocel_type` has been judged incorrect. Infer the correct
        type from `candidate_types`, using the event's id, the objects it
        touches, and the qualifiers it uses.
    """

    INPUTS = """
        - violation.ocel_id       the id of the event being checked
        - violation.ocel_type     the event's CURRENT type (the value to validate)
        - event.attributes        the anchor's existing attributes (may be empty)
        - related_objects         list of {ocel_object_id, ocel_type,
                                  ocel_qualifier} — the objects this event
                                  touches; qualifiers name the role each
                                  object plays in this activity
        - candidate_types         the closed list of valid event types
    """

    METHOD = """
        1. Weigh the signals, most to least informative: an activity keyword
           embedded in the `ocel_id` (e.g. `E2:1001` or `e2_x-1001` for an
           `E2` event), the touched objects' types and qualifiers in
           `related_objects`, and the event's own attribute row.
        2. Return the one value from `candidate_types` that best fits the
           evidence. It must differ from the current `ocel_type`.
        3. Return `inferred_type: null` only when no other candidate fits
           better than the current type.
    """

    EXAMPLES = """
        violation = {ocel_id: 'E2:1010', ocel_type: 'E1'}
        related_objects = [{ocel_type: 'type A', ocel_qualifier: 'a'}]
        candidate_types = ['E1', 'E2', 'E3', 'E4']
        → {"inferred_type": "E2",
           "rationale": "id keyword 'E2' names the type",
           "confidence": 0.95}
    """

    def build_context(self, conn, row, *, use_hints=True):
        # Copy the base flow (attach anchor event, hints, extend) but swap
        # in event-side candidate_types since `IssueTask.build_context`
        # hard-codes object_type_tables.
        ctx: dict = {"issue_key": self.issue_key, "violation": dict(row)}
        ctx["candidate_types"] = [t for t, _ in _event_type_tables(conn)]
        anchor_id, anchor_type = self.anchor(row)
        if anchor_id:
            self._attach_anchor(conn, ctx, anchor_id, anchor_type)
        if use_hints:
            self._attach_examination_hints(conn, ctx, row)
        self.extend_context(conn, ctx, row)
        return ctx

    def anchor(self, row: dict) -> tuple[str | None, str | None]:
        # Row shape (from ``_candidates_incorrect_event_type``) carries
        # ``ocel_id`` and ``ocel_type``; expose them as the (id, type)
        # anchor pair so ``_attach_anchor`` can locate the per-type sub-table.
        return row.get("ocel_id"), row.get("ocel_type")

    def extend_context(self, conn, ctx, row):
        # Related objects (through event_object) — the strongest event-type
        # signal after the id keyword.
        anchor_id = row.get("ocel_id")
        if not anchor_id:
            return
        related = conn.execute(
            "SELECT eo.ocel_object_id, o.ocel_type, eo.ocel_qualifier "
            "FROM event_object eo LEFT JOIN object o ON o.ocel_id = eo.ocel_object_id "
            "WHERE eo.ocel_event_id = ? LIMIT 8",
            (anchor_id,),
        ).fetchall()
        if related:
            ctx["related_objects"] = [
                {"ocel_object_id": oid, "ocel_type": otype, "ocel_qualifier": qual}
                for oid, otype, qual in related
            ]

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
        # Writes to the main `event` table's `ocel_type` — mirror of
        # `IncorrectObjectType.suppressed_target` targeting `object.ocel_type`.
        return {
            "target_table": "event",
            "target_pk": {"ocel_id": row.get("ocel_id")},
            "column": "ocel_type",
            "old_value": row.get("ocel_type"),
        }
