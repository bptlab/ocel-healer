from src.llm.actions import ActionResult
from src.llm.schemas import DuplicateResolutionOutput
from src.llm.tasks._base import ResolutionTask
from src.llm.tasks.duplicate_objects_on_ids import drop_conflicting_type, duplicate_rows_context


class DuplicateEventsOnIds(ResolutionTask):
    """Event-side mirror of :class:`DuplicateObjectsOnIds`.

    Multiple rows in the `event` table share the same `ocel_id`. When all
    duplicates carry the same `ocel_type` the fix is deterministic (keep
    one, delete the rest); otherwise the LLM picks the surviving type and
    the rows of the other type are deleted.
    """

    issue_key = "duplicate_events_on_ids"
    family = "duplicate"
    kind = "event"
    OutputModel = DuplicateResolutionOutput
    min_confidence = 0.0  # deterministic path ignores LLM confidence

    TASK = """
        Multiple rows in the `event` table share the same `ocel_id` but
        differ on `ocel_type`. Decide which single row is canonical and
        which rows should be deleted. The shared id stays — only one type
        survives.
    """

    INPUTS = """
        - violation.ocel_id      the shared id of the duplicate rows
        - duplicate_rows         list of {ocel_id, ocel_type,
                                 has_attribute_row} for each duplicate
                                 row; has_attribute_row=true means the
                                 type's attribute table holds this id
        - event                  the anchor record (one of the duplicates)
        - related_objects        the objects this event touches; their
                                 types and qualifiers indicate the right
                                 activity
    """

    METHOD = """
        1. Prefer rows with a non-null, non-empty `ocel_type`. A row with
           an explicit type is almost always canonical.
        2. If several rows carry a type, prefer the one whose activity
           name is consistent with the touched objects' types + qualifiers
           in `related_objects`.
        3. Set `canonical_id` to the shared `ocel_id` (the value all
           duplicates share) — that id stays in the table.
        4. Set `ids_to_delete` to a flat list of `ocel_id` strings, one
           entry per row to remove. Because all duplicates share the same
           id, entries are typically identical strings — that's expected;
           the downstream `DELETE FROM event WHERE ocel_id IN (...)` uses
           them as-is.
        5. If the rows disagree on type, set `canonical_type` to the
           surviving `ocel_type`, spelled exactly as in `duplicate_rows`.
           A row with has_attribute_row=true is strong evidence for its
           type.
    """

    EXAMPLES = """
        duplicate_rows = [{ocel_id: 'e-1', ocel_type: 'place order'},
                          {ocel_id: 'e-1', ocel_type: null}]
        related_objects = [{ocel_type: 'order', ocel_qualifier: 'order'}]
        → {"canonical_id": "e-1",
           "ids_to_delete": ["e-1"],
           "canonical_type": "place order",
           "rationale": "kept the row with ocel_type='place order'; related order object qualifies this event as a place-order activity. Marked the NULL-type row for removal.",
           "confidence": 0.9}
    """

    def extend_context(self, conn, ctx: dict, row: dict) -> None:
        # The detector emits one `ocel_id`; `ocel_ids` is accepted for joined lists.
        dup_ids = row.get("ocel_ids") or row.get("ocel_id") or ""
        ids = [i.strip() for i in str(dup_ids).split(",") if i.strip()]
        if not ids:
            return
        ctx["duplicate_rows"] = duplicate_rows_context(conn, ids, "event")
        # Related objects for the anchor event, as a lightweight activity signal.
        anchor_id = ids[0]
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

    def parse_payload(self, row: dict, payload: dict) -> ActionResult:
        ocel_id = row.get("ocel_id")
        # ocel_types is the de-duped, comma-joined list from the detector.
        unique_types = [
            t.strip() for t in (row.get("ocel_types") or "").split(",")
            if t.strip() and t.strip().lower() != "none"
        ]

        # Pure I6(a): all duplicates share the same type → deterministic delete.
        if ocel_id and len(unique_types) == 1:
            return ActionResult.delete(
                target_table="event",
                target_pk={"ocel_id": ocel_id},
                reason=(
                    f"All duplicate rows for '{ocel_id}' share type '{unique_types[0]}'. "
                    "Keeping one row (MIN rowid), deleting the rest."
                ),
            )

        # Types differ → drop the rows of a type the LLM did not keep.
        action = drop_conflicting_type(row, payload, unique_types, "event")
        if action is not None:
            return action

        # No usable canonical type → surface LLM suggestion as unrouted noop for manual review.
        canonical = payload.get("canonical_id")
        rationale = (payload.get("rationale") or "").strip()
        if not canonical:
            return ActionResult.unrouted(
                rationale or "LLM could not determine a canonical event ID",
                target_table="event",
            )
        ids_to_delete = payload.get("ids_to_delete") or []
        ids_str = ", ".join(repr(i) for i in ids_to_delete)
        return ActionResult.unrouted(
            f"{rationale}  |  Canonical ID: {canonical!r}.  "
            f"Suggested DELETE: DELETE FROM event WHERE ocel_id IN ({ids_str}).",
            target_table="event",
        )
