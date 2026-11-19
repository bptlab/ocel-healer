from src.detection.error_detection import _event_type_tables
from src.llm.schemas import InferredTimestampOutput
from src.llm.tasks._base import DetectionResult, DetectionTask
from src.llm.tasks._event_context import neighbor_events_ctx


class IncorrectEventTime(DetectionTask):
    """LLM-only detector for implausible ``ocel_time`` values.

    Rule detectors don't fire here — a syntactically-valid timestamp that
    violates the object's lifecycle order (or falls outside its window
    entirely) still parses cleanly as a datetime. The LLM judges the
    anchor's ``ocel_time`` against the ordered timestamps of every other
    event touching the same objects; when the anchor is inconsistent, it
    proposes a corrected timestamp using the neighbors' format verbatim.

    Reuses ``InferredTimestampOutput`` and the ``neighbor_events_ctx``
    context builder from :mod:`missing_event_timestamp`.
    """

    issue_key = "incorrect_event_time"
    family = "temporal"
    kind = "event"
    OutputModel = InferredTimestampOutput

    DETECT_TASK = """
        This event's `ocel_time` is present and well-formed, but may be
        implausible given the lifecycles of the objects it touches. Decide only
        WHETHER it is wrong: does the anchor sit sensibly in the ordered timeline
        of its neighbors? Do not propose a corrected time: that is a separate step.
    """

    DETECT_METHOD = """
        1. Find the anchor's `event_type` in `event_type_order` to know which
           neighbor types usually come before and after it.
        2. Compare `actual_value` against the neighbors that bracket the
           anchor in the per-object sequences.
        3. `is_violation: true` when the anchor clearly violates the usual
           order OR clearly falls outside the objects' time window (e.g. years
           away from every neighbor).
        4. `is_violation: false` for small out-of-order differences (seconds
           apart, minor drift) — this is a check for gross errors, not a lint —
           and when the neighbor set is empty or has no timestamps.
    """

    DETECT_EXAMPLES = """
        violation = {ocel_id: 'ev-10', event_type: 'E3',
                     actual_value: '2010-01-01 00:00:00'}
        event_type_order = [{event_type: 'E1', position: 0.0},
                            {event_type: 'E2', position: 0.2},
                            {event_type: 'E3', position: 0.5},
                            {event_type: 'E4', position: 0.8}]
        neighbor_events = [
          {ocel_type: 'E1', ocel_time: '2020-01-02 12:00:00'},
          {ocel_type: 'E2', ocel_time: '2020-01-02 12:05:00'},
          {ocel_type: 'E4', ocel_time: '2020-01-02 14:00:00'},
        ]
        → {"is_violation": true,
           "rationale": "2010 is ten years before every neighbor (2020-01-02); E3 usually sits between E2 and E4",
           "confidence": 0.95}

        violation = {ocel_id: 'ev-11', event_type: 'E3',
                     actual_value: '2020-01-02 13:00:00'}
        event_type_order = [{event_type: 'E1', position: 0.0},
                            {event_type: 'E2', position: 0.2},
                            {event_type: 'E3', position: 0.5},
                            {event_type: 'E4', position: 0.8}]
        neighbor_events = [
          {ocel_type: 'E1', ocel_time: '2020-01-02 12:00:00'},
          {ocel_type: 'E2', ocel_time: '2020-01-02 12:05:00'},
          {ocel_type: 'E4', ocel_time: '2020-01-02 14:00:00'},
        ]
        → {"is_violation": false,
           "rationale": "13:00 sits between E2 (12:05) and E4 (14:00), matching the usual order",
           "confidence": 0.9}
    """

    TASK = """
        This event's `ocel_time` has been judged implausible. Propose a
        corrected timestamp from the timestamps of its neighbor events.
    """

    INPUTS = """
        - violation.ocel_id         the id of the event being checked
        - violation.event_type      the event's type
        - violation.actual_value    the CURRENT `ocel_time` under scrutiny
        - violation.target_table    concrete per-type sub-table where
                                    `ocel_time` lives; FYI only
        - related_objects           list of {ocel_object_id, ocel_type,
                                    ocel_qualifier} — the objects this event
                                    touches
        - neighbor_events           list of {ocel_id, ocel_type, ocel_time,
                                    qualifier, ocel_object_id} — every other
                                    event touching those same objects, sorted
                                    by timestamp
        - expected_format           one representative timestamp string from a
                                    neighbor; use this format verbatim if you
                                    propose a corrected value
        - event_type_order          the usual order of event types in this
                                    log, learned from the data: list of
                                    {event_type, position}, where position 0
                                    means first in an object's lifecycle and
                                    1 means last
    """

    METHOD = """
        1. Find the anchor's `event_type` in `event_type_order` to know which
           neighbor types usually come before and after it.
        2. Pick a timestamp between the tightest bracketing pair: the latest
           neighbor that usually comes before the anchor and the earliest one
           that usually comes after it, respecting every touched object.
        3. If neighbors bound the anchor on one side only, pick a time shortly
           before the earliest later neighbor (or shortly after the latest
           earlier one).
        4. Match `expected_format` verbatim (same separator, same precision,
           no timezone). The value must differ from `actual_value`.
        5. Return `inferred_timestamp: null` only when no neighbor has a
           timestamp.
    """

    EXAMPLES = """
        violation = {ocel_id: 'ev-10', event_type: 'E3',
                     actual_value: '2010-01-01 00:00:00'}
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
           "confidence": 0.85}
    """

    def anchor(self, row: dict) -> tuple[str | None, str | None]:
        # No object anchor; skip _attach_anchor / _attach_events (event tasks
        # never attach events anyway, but returning (None, None) also skips
        # the per-type table lookup for the event itself since it's not the
        # object anchor the base helpers expect).
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

    def parse_repair(self, row: dict, payload: dict) -> DetectionResult:
        rationale = str(payload.get("rationale", "") or "").strip()
        confidence = float(payload.get("confidence", 0.0) or 0.0)
        inferred = payload.get("inferred_timestamp")
        current = row.get("actual_value")
        # Not flagged when the model returns null, or proposes the current
        # value (equivalent to null for our purposes).
        if not inferred or str(inferred) == str(current):
            return DetectionResult(
                flagged=False, rationale=rationale, confidence=confidence,
                suggested_value=None,
            )
        return DetectionResult(
            flagged=True, rationale=rationale, confidence=confidence,
            suggested_value=inferred,
        )

    def suppressed_target(self, row: dict) -> dict | None:
        # Fall back to resolving target_table from event_type when the row
        # (constructed by the candidate source) didn't already carry it —
        # this makes the task robust to being invoked from other paths too.
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
