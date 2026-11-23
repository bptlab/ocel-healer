from src.llm.actions import ActionResult
from src.llm.schemas import InferredTimestampOutput
from src.llm.tasks._base import ResolutionTask
from src.llm.tasks._event_context import neighbor_events_ctx


class MissingEventTimestamp(ResolutionTask):
    """Fill in a NULL/empty `ocel_time` on an event by interpolating from
    the lifecycle timestamps of the objects the event touches."""

    issue_key = "missing_event_timestamp"
    family = "temporal"
    OutputModel = InferredTimestampOutput
    min_confidence = 0.0  # always try; the low-confidence gate is a display concern

    TASK = """
        One event row has no `ocel_time`. Interpolate a plausible timestamp
        from the timestamps of the other events touching the same object(s)
        — the "neighbor events". Match the format of the neighbors' timestamps
        exactly. A bound on one side is enough; return null only when no
        neighbor has a timestamp.
    """

    INPUTS = """
        - violation.ocel_id         the id of the event with a missing timestamp
        - violation.event_type      the event's type
        - violation.target_table    concrete per-type sub-table; FYI only
        - related_objects           list of {ocel_object_id, ocel_type,
                                    ocel_qualifier} — the objects this event
                                    touches
        - neighbor_events           list of {ocel_id, ocel_type, ocel_time,
                                    qualifier, ocel_object_id} — every other
                                    event touching those same objects, sorted
                                    by timestamp
        - expected_format           one representative timestamp string from a
                                    neighbor; use this format verbatim
        - event_type_order          the usual order of event types in this
                                    log, learned from the data: list of
                                    {event_type, position}, where position 0
                                    means first in an object's lifecycle and
                                    1 means last
    """

    METHOD = """
        1. Find the anchor's `event_type` in `event_type_order`. Event types
           with a lower position usually happen before it, those with a
           higher position after it.
        2. Locate the anchor's neighbors that flank it in that order: the
           latest neighbor whose type usually comes before the anchor's, and
           the earliest one whose type usually comes after it.
        3. Pick a timestamp between the tightest bracketing pair. When
           several object-lifecycles apply (an event can touch multiple
           objects), all their neighbor bounds must be respected.
        4. Match `expected_format` verbatim — same separator, same
           precision. Do not switch to ISO with `T` or add a timezone.
        5. If neighbors bound the anchor on one side only (e.g. the first
           event of a lifecycle has only later neighbors), pick a time
           shortly before the earliest later neighbor (or shortly after the
           latest earlier one).
        6. Return `inferred_timestamp: null` only when no neighbor has a
           timestamp.
    """

    EXAMPLES = """
        violation = {ocel_id: 'ev-10', event_type: 'E3'}
        event_type_order = [{event_type: 'E1', position: 0.0},
                            {event_type: 'E2', position: 0.2},
                            {event_type: 'E3', position: 0.5},
                            {event_type: 'E4', position: 0.8}]
        neighbor_events = [
          {ocel_type: 'E1', ocel_time: '2020-01-02 12:00:00'},
          {ocel_type: 'E2', ocel_time: '2020-01-02 12:05:00'},
          {ocel_type: 'E4', ocel_time: '2020-01-02 14:00:00'},
        ]
        expected_format = '2020-01-02 12:00:00'
        → {"inferred_timestamp": "2020-01-02 13:00:00",
           "rationale": "E3 usually sits after E2 (12:05) and before E4 (14:00); picked the midpoint",
           "confidence": 0.9}

        violation = {ocel_id: 'ev-20', event_type: 'E1'}
        event_type_order = [{event_type: 'E1', position: 0.0},
                            {event_type: 'E2', position: 0.2}]
        neighbor_events = [
          {ocel_type: 'E2', ocel_time: '2020-02-03 09:40:00'},
        ]
        → {"inferred_timestamp": "2020-02-03 09:10:00",
           "rationale": "E1 usually comes first, before E2 (09:40); only an upper bound, picked shortly before it",
           "confidence": 0.6}

        violation = {ocel_id: 'ev-30', event_type: 'E4'}
        neighbor_events = []
        → {"inferred_timestamp": null,
           "rationale": "neighbor_events is empty; nothing to anchor a timestamp on",
           "confidence": 0.2}
    """

    def anchor(self, row: dict) -> tuple[str | None, str | None]:
        # No object anchor; skip _attach_anchor / _attach_events.
        return (None, None)

    def build_context(self, conn, row, *, use_hints=True):
        ctx: dict = {"issue_key": self.issue_key, "violation": dict(row)}
        event_id = row.get("ocel_id")
        if event_id:
            ctx.update(neighbor_events_ctx(conn, event_id))
        if use_hints:
            self._attach_examination_hints(conn, ctx, row)
        self.extend_context(conn, ctx, row)
        return ctx

    def parse_payload(self, row: dict, payload: dict) -> ActionResult:
        inferred = payload.get("inferred_timestamp")
        rationale = str(payload.get("rationale", "") or "").strip()
        if inferred is None:
            return ActionResult.decline(
                rationale or "LLM could not bracket the missing timestamp from neighbors."
            )
        target_table = row.get("target_table") or ""
        return ActionResult.update(
            target_table=target_table,
            target_pk={"ocel_id": row.get("ocel_id")},
            column="ocel_time",
            old_value=row.get("actual_value"),
            new_value=inferred,
        )

    def suppressed_target(self, row: dict) -> dict | None:
        target_table = row.get("target_table")
        ocel_id = row.get("ocel_id")
        if not (target_table and ocel_id):
            return None
        return {
            "target_table": target_table,
            "target_pk": {"ocel_id": ocel_id},
            "column": "ocel_time",
            "old_value": row.get("actual_value"),
        }
