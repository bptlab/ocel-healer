from src.llm.actions import object_attribute_target
from src.llm.schemas import ImplausibleValueOutput
from src.llm.sampling import column_summary
from src.llm.tasks._base import DetectionResult, DetectionTask


class IncorrectAttributeValue(DetectionTask):
    """LLM detection for type-correct but semantically implausible attribute values.

    Runs on rows that already passed both `missing_attribute_value` (value is
    not NULL / empty) and `incorrect_attribute_datatype` (value's Python type
    matches the column's SQLite affinity). The LLM's job is to decide whether
    the value makes sense for its attribute — flagging things like negative
    prices, out-of-vocab enum labels, impossible dates, or magnitudes that
    fall far outside the peer distribution. Deterministic type/format wrongness
    is out of scope; that's the rule-based sibling's job.
    """

    issue_key = "incorrect_attribute_value"
    family = "attribute"
    OutputModel = ImplausibleValueOutput

    DETECT_TASK = """
        A row has an attribute value with the right SQL type and no NULL, but the
        value itself may be semantically wrong for the attribute. Decide only
        WHETHER it is wrong, given the attribute's name / meaning and the peer
        distribution. Do not propose a corrected value: that is a separate step.
    """

    DETECT_METHOD = """
        1. Read the attribute name (and the anchor's other attributes, if
           given) to infer what the column records.
        2. Compare `actual_value` against `peer_objects[*][attribute]` and
           `peer_summary` (the column's full range). A value that matches the peers' shape (sign, magnitude, vocabulary, casing,
           length) is plausible even when unusual.
        3. `is_violation: true` when the value contradicts the attribute's
           meaning — even if you can't tell the right value:
             - a sign no peer has (a negative value where every peer is positive),
             - a value outside a closed peer vocabulary,
             - text with a clearly different convention (a machine id where
               every peer has a human name),
             - a date that cannot correspond to what the column records,
             - a value orders of magnitude off from every peer.
        4. `is_violation: false` when the value looks fine or merely unusual,
           when peers are absent, or when the attribute's meaning is unclear.
    """

    DETECT_EXAMPLES = """
        violation = {attribute: 'attr_x', actual_value: -3.5, expected_type: 'REAL'}
        peer_objects = [{attr_x: 3.0}, {attr_x: 4.5}, {attr_x: 3.5}]
        → {"is_violation": true,
           "rationale": "every peer value of attr_x is positive; a negative value contradicts the attribute",
           "confidence": 0.9}

        violation = {attribute: 'attr_z', actual_value: 3, expected_type: 'INTEGER'}
        peer_objects = [{attr_z: 1}, {attr_z: 5}, {attr_z: 2}]
        → {"is_violation": false,
           "rationale": "3 matches the peer distribution's shape",
           "confidence": 0.95}
    """

    TASK = """
        This attribute value has been judged semantically wrong. Propose the
        corrected value from the attribute's meaning and the peer distribution.
    """

    INPUTS = """
        - violation.object_type    the anchor object's type
        - violation.ocel_id        the anchor object's id
        - violation.attribute      the attribute (column) name being checked
        - violation.expected_type  the column's SQL affinity (already satisfied)
        - violation.actual_value   the value currently in the cell
        - object.attributes        the anchor's other attributes for context
        - peer_objects             up to 20 same-type peers with their values
                                   for this attribute — the reference cohort
        - peer_summary             the column across ALL other elements of this
                                   type: count, min, median, max for numbers;
                                   the most common values for text
    """

    METHOD = """
        1. Read the attribute name (and the anchor's other attributes, if
           given) to infer what the column records.
        2. Use `peer_objects[*][attribute]` and `peer_summary` for the value's
           shape, unit, format and range; when the wrong value is an obvious distortion of a right one
           (a flipped sign, a shifted magnitude), undo the distortion.
        3. The value must match `expected_type`'s affinity and differ from
           `actual_value`.
        4. Return `suggested_value: null` only when nothing in the context
           points to a value.
    """

    EXAMPLES = """
        violation = {attribute: 'attr_x', actual_value: -3.5, expected_type: 'REAL'}
        peer_objects = [{attr_x: 3.0}, {attr_x: 4.5}, {attr_x: 3.5}]
        → {"suggested_value": 3.5,
           "rationale": "the value is a sign-flipped 3.5, which matches the peers",
           "confidence": 0.8}

        violation = {attribute: 'attr_w', actual_value: 'K9', expected_type: 'TEXT'}
        peer_objects = [{attr_w: 'A1'}, {attr_w: 'B2'}, {attr_w: 'C3'}]
        → {"suggested_value": null,
           "rationale": "'K9' is outside the peer vocabulary but nothing points to the right value",
           "confidence": 0.3}
    """

    def extend_context(self, conn, ctx: dict, row: dict) -> None:
        # Peers stratified on the column being judged so the cohort actually
        # constrains the plausibility check.
        target = row.get("attribute_name") or row.get("attribute")
        self._attach_peers(conn, ctx, row, target_col=target, k=20)
        anchor_id, anchor_type = self.anchor(row)
        summary = column_summary(conn, anchor_id=anchor_id, anchor_type=anchor_type,
                                 column=target, kind=self.kind) if anchor_id and target else None
        if summary:
            ctx["peer_summary"] = summary

    def parse_repair(self, row: dict, payload: dict) -> DetectionResult:
        rationale = str(payload.get("rationale", "") or "").strip()
        confidence = float(payload.get("confidence", 0.0) or 0.0)
        suggested = payload.get("suggested_value")
        # Null / same-as-current → not flagged. Type coercion isn't safe here
        # (`"19.99" == 19.99` is False even when they mean the same thing),
        # but treating identical scalars as "no change" is enough to filter
        # LLMs that echo the input.
        if suggested is None or suggested == row.get("actual_value"):
            return DetectionResult(
                flagged=False, rationale=rationale, confidence=confidence,
                suggested_value=None,
            )
        return DetectionResult(
            flagged=True, rationale=rationale, confidence=confidence,
            suggested_value=suggested,
        )

    def suppressed_target(self, row: dict) -> dict | None:
        return object_attribute_target(row)
