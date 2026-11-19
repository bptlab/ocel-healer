from src.llm.actions import ActionResult
from src.llm.schemas import CanonicalValueOutput
from src.llm.tasks._base import ResolutionTask
from src.llm.tasks.duplicate_objects_on_attributes import remove_copies


class DuplicateEventsOnAttributes(ResolutionTask):
    """Event-side mirror of :class:`DuplicateObjectsOnAttributes`: resolved
    by rule (`remove_copies`); the LLM is never asked."""

    issue_key = "duplicate_events_on_attributes"
    family = "duplicate"
    kind = "event"
    OutputModel = CanonicalValueOutput
    min_confidence = 0.0

    TASK = """
        Two or more events of the same type share their time and every
        attribute value. This issue is repaired by rule: copies of the
        best-connected event are deleted.
    """

    def rule_action(self, conn, row: dict) -> ActionResult:
        return remove_copies(conn, row, "event")

    def parse_payload(self, row: dict, payload: dict) -> ActionResult:
        return ActionResult.unrouted("duplicate_events_on_attributes is repaired by rule.")
