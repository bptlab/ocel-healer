from src.detection.error_detection import _OCEL_RESERVED, _column_info, _event_type_tables
from src.llm.actions import ActionResult
from src.llm.schemas import InferredObjectOutput
from src.llm.tasks._base import ResolutionTask


class MissingEvent(ResolutionTask):
    """An `event_object` row references an event id that doesn't exist in
    the `event` table. Propose a complete new event row: the missing
    top-level entry plus an initial-state row in the per-type sub-table.

    Counterpart to ``MissingObject``. Both trip
    ``dangling_e2o_relationship`` from the opposite side; the dashboard
    routes each to its own cell.
    """

    issue_key = "missing_event"
    family = "relation"
    OutputModel = InferredObjectOutput  # reused: {ocel_type: str|None, attributes: dict}
    min_confidence = 0.0  # low-confidence proposals still get shown for review

    TASK = """
        An event_object row references an event that is missing from the
        database. The referring object and the qualifier suggest a known
        event type. Propose:
          - `ocel_type`: the event type the missing event belongs to (must
            be one of `candidate_types`), or null if the evidence is too
            weak.
          - `attributes`: the inferred `ocel_time` (key: "ocel_time") plus
            a value for every column `type_attributes` lists for the chosen
            type. Match the timestamp format of the linked object's other
            events. Leave a key out if you can't infer it — the resolver
            inserts NULL and the missing-value detectors pick it up.
        The resolver will INSERT one row into `event` and one row into
        `event_<Type>` from your answer.
    """

    INPUTS = """
        - violation.ocel_event_id     the missing event id (as it appears
                                      in event_object)
        - violation.ocel_object_id    the object that references the
                                      missing event
        - violation.ocel_qualifier    the qualifier under which the object
                                      references it (often names the role
                                      the object plays in the activity)
        - related_object              the referring object: its ocel_id
                                      and ocel_type
        - object_events               up to 8 OTHER events touching the
                                      same object, each with ocel_id,
                                      ocel_type, ocel_time, qualifier —
                                      the lifecycle window the missing
                                      event should slot into
        - candidate_types             the closed list of valid event types
        - event_links                 up to 10 surviving event_object rows
                                      of the missing event: the objects it
                                      touched, their types and qualifiers
        - type_attributes             per candidate type: its attribute
                                      columns
    """

    METHOD = """
        1. Read `violation.ocel_event_id` first — many event ids embed the
           activity keyword (e.g. `place_order:e-991000`, `pick_item:…`).
           An id keyword pins the type with high confidence.
        2. If the id is opaque (a bare id like `e-771000`), fall back to
           the qualifier + the referring object's type. `packer` on a
           `packages` object → `create package` or `send package`;
           `shipper` on a `packages` object → `send package`; `order` on
           an `orders` object → one of the order-lifecycle events.
        3. Use `object_events` to pick between multiple plausible types:
           if `place order` is already present for this object, the
           missing event is unlikely to be another `place order`.
        4. For `attributes.ocel_time`, bracket the missing event between
           the timestamps of the object's other events (same object
           lifecycle order as `missing_event_timestamp`). Match the
           format verbatim.
        5. Fill the chosen type's other columns from `event_links`: a
           column that holds a person (e.g. `resource`) usually names the
           employee linked in `event_links`.
        6. Return `ocel_type: null` when the evidence is genuinely opaque
           (no id keyword, no qualifier hint, no lifecycle gap).
    """

    EXAMPLES = """
        violation = {ocel_event_id: 'place_order:e-991000',
                     ocel_object_id: 'o-990100', ocel_qualifier: 'order'}
        related_object = {ocel_id: 'o-990100', ocel_type: 'orders'}
        object_events = [
          {ocel_type: 'confirm order', ocel_time: '2023-04-03 12:05:00'},
          {ocel_type: 'pay order',     ocel_time: '2023-04-03 13:00:00'},
        ]
        candidate_types = ['place order', 'pay order', 'confirm order', ...]
        → {"ocel_type": "place order",
           "attributes": {"ocel_time": "2023-04-03 12:00:00"},
           "rationale": "id embeds 'place_order' and the object lacks a place order event; picked a timestamp before confirm order",
           "confidence": 0.9}

        violation = {ocel_event_id: 'e-771000',
                     ocel_object_id: 'p-660001', ocel_qualifier: 'packer'}
        related_object = {ocel_id: 'p-660001', ocel_type: 'packages'}
        object_events = [
          {ocel_type: 'send package', ocel_time: '2023-04-04 09:00:00'}
        ]
        candidate_types = ['create package', 'send package', ...]
        event_links = [{ocel_object_id: 'Detlef Wetzeler',
                        ocel_type: 'employees', qualifier: 'packer'}, ...]
        type_attributes = {'create package': ['resource'], ...}
        → {"ocel_type": "create package",
           "attributes": {"ocel_time": "2023-04-04 08:00:00",
                          "resource": "Detlef Wetzeler"},
           "rationale": "qualifier 'packer' on a package + already has send package → the missing event is likely 'create package', which precedes send",
           "confidence": 0.75}
    """

    # Key used to smuggle the event_type→table map from build_context (which
    # has a live conn) to parse_payload (which doesn't). Stashed on the
    # `row` dict; the leading underscore signals internal-use-only so the
    # LLM prompt renderer ignores it.
    _TYPE_MAP_KEY = "_event_type_map"
    _COLUMNS_KEY = "_event_type_columns"

    def anchor(self, row: dict) -> tuple[str | None, str | None]:
        # No object-side anchor; we build custom context below.
        return (None, None)

    def build_context(self, conn, row, *, use_hints=True):
        ctx: dict = {"issue_key": self.issue_key, "violation": dict(row)}
        type_map = dict(_event_type_tables(conn))
        ctx["candidate_types"] = list(type_map.keys())
        # Stash the map so parse_payload can look up the target sub-table.
        row[self._TYPE_MAP_KEY] = type_map

        # The columns a complete row of each type has. Kept to names only:
        # example rows made the prompt too long for small models.
        columns = {t: [c for c, _ in _column_info(conn, table)] for t, table in type_map.items()}
        row[self._COLUMNS_KEY] = columns
        ctx["type_attributes"] = columns

        # What the missing event touched, not just the reported row.
        links = conn.execute(
            "SELECT eo.ocel_object_id, o.ocel_type, eo.ocel_qualifier "
            "FROM event_object eo LEFT JOIN object o ON o.ocel_id = eo.ocel_object_id "
            "WHERE eo.ocel_event_id = ? LIMIT 10",
            (row.get("ocel_event_id"),),
        ).fetchall()
        ctx["event_links"] = [
            {"ocel_object_id": oid, "ocel_type": otype, "qualifier": q}
            for oid, otype, q in links
        ]

        # The referring object (via the E2O row).
        object_id = row.get("ocel_object_id")
        if object_id:
            got = conn.execute(
                "SELECT ocel_id, ocel_type FROM object WHERE ocel_id = ? LIMIT 1",
                (object_id,),
            ).fetchone()
            if got:
                ctx["related_object"] = {"ocel_id": got[0], "ocel_type": got[1]}

            # Other events touching this same object — the lifecycle window
            # the missing event should slot into. Joins through the per-type
            # event sub-tables to attach ocel_time.
            other_events = conn.execute(
                "SELECT e.ocel_id, e.ocel_type, eo.ocel_qualifier "
                "FROM event_object eo "
                "JOIN event e ON e.ocel_id = eo.ocel_event_id "
                "WHERE eo.ocel_object_id = ? "
                "  AND eo.ocel_event_id != ? "
                "LIMIT 8",
                (object_id, row.get("ocel_event_id")),
            ).fetchall()
            enriched = []
            for eid, etype, qual in other_events:
                ts = None
                table = type_map.get(etype)
                if table:
                    got = conn.execute(
                        f'SELECT ocel_time FROM "{table}" WHERE ocel_id = ? LIMIT 1',
                        (eid,),
                    ).fetchone()
                    if got:
                        ts = got[0]
                enriched.append({
                    "ocel_id": eid, "ocel_type": etype,
                    "ocel_time": ts, "qualifier": qual,
                })
            if enriched:
                ctx["object_events"] = enriched

        if use_hints:
            self._attach_examination_hints(conn, ctx, row)
        self.extend_context(conn, ctx, row)
        return ctx

    def parse_payload(self, row: dict, payload: dict) -> ActionResult:
        rationale = str(payload.get("rationale", "") or "").strip()
        ocel_type = payload.get("ocel_type")
        if not ocel_type:
            return ActionResult.decline(
                rationale or "LLM could not infer an event type for the missing referent."
            )

        type_map = row.get(self._TYPE_MAP_KEY) or {}
        target_table = type_map.get(ocel_type)
        if not target_table:
            return ActionResult.unrouted(
                f"Cannot resolve per-type event sub-table for ocel_type={ocel_type!r}",
                target_table="event",
            )

        attrs = payload.get("attributes") or {}
        # Only the chosen type's columns: a stray name would make the insert fail.
        known = (row.get(self._COLUMNS_KEY) or {}).get(ocel_type)
        extra = {
            k: v for k, v in attrs.items()
            if k not in _OCEL_RESERVED and (known is None or k in known)
        }

        event_id = row.get("ocel_event_id")
        inserts = [
            {"table": "event", "columns": {"ocel_id": event_id, "ocel_type": ocel_type}},
            {"table": target_table, "columns": {
                "ocel_id": event_id,
                "ocel_time": attrs.get("ocel_time"),
                **extra,
            }},
        ]
        return ActionResult.insert(inserts=inserts, reason=rationale)

    def suppressed_target(self, row: dict) -> dict | None:
        # INSERT actions have no single-column override target.
        return None
