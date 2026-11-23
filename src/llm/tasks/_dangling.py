"""Shared repair logic for `dangling_e2o_relationship` / `dangling_o2o_relationship`.

A dangling reference is usually a typo of an existing id, so the existing ids
most similar to the dangling one are the candidates. A clear best match is
repaired by rule; close calls go to the LLM with those candidates; a
reference that resembles no existing id is an orphan and is deleted.
"""

from src.llm.actions import ActionResult
from src.llm.sampling import clear_match, closest_ids


def missing_sides(row: dict, sides: dict[str, dict]) -> list[str]:
    side = row.get("missing_side")
    return list(sides) if side == "both" else [side] if side in sides else []


def side_candidates(conn, row: dict, spec: dict) -> list[dict]:
    expected = row.get("ocel_qualifier") if spec.get("typed_by_qualifier") else None
    context = row.get(spec["context_column"]) if spec.get("context_column") else None
    return closest_ids(conn, row.get(spec["column"]), kind=spec["kind"],
                       expected_type=expected, context_id=context)


def dangling_rule(conn, row: dict, *, table: str, sides: dict[str, dict],
                  pk_cols: list[str]) -> ActionResult | None:
    """Repair what the candidates decide on their own; None hands the row to
    the LLM. With both ends missing, the clear end is fixed first and the
    other end comes back as a one-sided issue."""
    pk = {c: row.get(c) for c in pk_cols}
    if any(v is None for v in pk.values()):
        return None
    steps, unclear = [], []
    for side in missing_sides(row, sides):
        col = sides[side]["column"]
        candidates = side_candidates(conn, row, sides[side])
        if not candidates:
            return ActionResult.delete_all(
                target_table=table, target_pk=pk,
                reason=f"{pk[col]!r} resembles no existing id: deleting the orphan relation.",
            )
        best = clear_match(candidates)
        if best is None:
            unclear.append(side)
            continue
        steps.append(ActionResult.update(
            target_table=table, target_pk=dict(pk), column=col,
            old_value=pk[col], new_value=best,
            reason=f"{pk[col]!r} is a near-miss of the existing id {best!r} "
                   f"(similarity {candidates[0]['similarity']:.2f}).",
        ))
        pk[col] = best
    if not steps:
        if len(unclear) > 1:
            return ActionResult.decline(
                "Neither end matches one existing id clearly; fix one end by hand."
            )
        return None
    if len(steps) == 1:
        return steps[0]
    return ActionResult.batch(steps=steps, reason=" ".join(s.reason for s in steps))
