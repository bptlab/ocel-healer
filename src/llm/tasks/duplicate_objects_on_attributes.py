from src.llm.actions import ActionResult
from src.llm.schemas import CanonicalValueOutput
from src.llm.sql_utils import table_for_type
from src.llm.tasks._base import ResolutionTask


def _relations(conn, kind: str, ocel_id: str) -> set[tuple]:
    """Every relation of one element, written from its own side so two
    elements' sets can be compared."""
    if kind == "event":
        return set(conn.execute(
            "SELECT 'e2o', ocel_object_id, ocel_qualifier FROM event_object "
            "WHERE ocel_event_id = ?", (ocel_id,),
        ).fetchall())
    return set(conn.execute(
        "SELECT 'e2o', ocel_event_id, ocel_qualifier FROM event_object WHERE ocel_object_id = ? "
        "UNION SELECT 'out', ocel_target_id, ocel_qualifier FROM object_object WHERE ocel_source_id = ? "
        "UNION SELECT 'in', ocel_source_id, ocel_qualifier FROM object_object WHERE ocel_target_id = ?",
        (ocel_id, ocel_id, ocel_id),
    ).fetchall())


def remove_copies(conn, row: dict, kind: str) -> ActionResult:
    """Delete the elements of a duplicate-on-attributes group that are mere
    copies of another member.

    Equal attribute values alone don't make a duplicate (every item of a
    product shares its weight and price), so only members whose relations
    are a subset of the kept member's count as copies: a clone without
    links, or one whose links were copied along. Members with relations of
    their own are left alone. The kept member is the best-connected one,
    ties going to the shorter (un-suffixed) id."""
    ids = sorted({i.strip() for i in str(row.get("ocel_ids", "")).split(",") if i.strip()})
    if len(ids) < 2:
        return ActionResult.decline("Duplicate group lists fewer than two ids.")
    rels = {i: _relations(conn, kind, i) for i in ids}
    keep = max(ids, key=lambda i: (len(rels[i]), -len(i)))
    copies = [i for i in ids if i != keep and rels[i] <= rels[keep]]
    if not copies:
        return ActionResult.decline(
            f"The {kind}s {', '.join(ids)} share attribute values but each has "
            "relations of its own, so they look like distinct elements; not merged."
        )
    type_table = table_for_type(conn, row.get(f"{kind}_type"), kind=kind)
    own_rel_cols = (
        [("event_object", "ocel_event_id")] if kind == "event" else
        [("event_object", "ocel_object_id"), ("object_object", "ocel_source_id"),
         ("object_object", "ocel_target_id")]
    )
    steps = []
    for c in copies:
        steps += [
            ActionResult.delete_all(target_table=table, target_pk={col: c})
            for table, col in own_rel_cols
        ]
        if type_table:
            steps.append(ActionResult.delete_all(target_table=type_table, target_pk={"ocel_id": c}))
        steps.append(ActionResult.delete_all(target_table=kind, target_pk={"ocel_id": c}))
    return ActionResult.batch(
        steps=steps,
        reason=f"Keeping {keep!r}; deleting {', '.join(map(repr, copies))}, "
               f"which only repeat its attributes and relations.",
    )


class DuplicateObjectsOnAttributes(ResolutionTask):
    """Resolved by rule (`remove_copies`); the LLM is never asked."""

    issue_key = "duplicate_objects_on_attributes"
    family = "duplicate"
    OutputModel = CanonicalValueOutput
    min_confidence = 0.0

    TASK = """
        Two or more objects of the same type share every attribute value.
        This issue is repaired by rule: copies of the best-connected object
        are deleted.
    """

    def rule_action(self, conn, row: dict) -> ActionResult:
        return remove_copies(conn, row, "object")

    def parse_payload(self, row: dict, payload: dict) -> ActionResult:
        return ActionResult.unrouted("duplicate_objects_on_attributes is repaired by rule.")
