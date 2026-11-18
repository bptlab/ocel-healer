"""Static guard for the dashboard's background LLM jobs.

marimo attributes state updates made by a mo.Thread to the cell that started
the thread, and never re-runs a cell for its own state updates. A cell that
both starts an LLM job (`start_llm_job`) and renders it (`get_llm_job`) would
therefore stay stuck on "running" forever. This test keeps the two apart.
"""

import ast
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parent.parent / "src" / "dashboard.py"


def _cells():
    tree = ast.parse(DASHBOARD.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            ast.unparse(d).startswith("app.cell") for d in node.decorator_list
        ):
            yield node


def _names_used(cell) -> set[str]:
    return {n.id for n in ast.walk(cell) if isinstance(n, ast.Name)}


def test_job_starting_cells_do_not_render_the_job():
    starters = [c for c in _cells() if "start_llm_job" in _names_used(c)]
    assert starters, "expected at least one cell that starts an LLM job"
    offenders = [c.name for c in starters if "get_llm_job" in _names_used(c)]
    assert not offenders, (
        f"cells {offenders} both start an LLM job and read get_llm_job; "
        "split them into a start cell and a view cell (see dashboard.py)"
    )


def test_every_job_kind_has_a_view():
    renderers = [c for c in _cells() if "get_llm_job" in _names_used(c)]
    rendered_kinds = {
        node.value
        for c in renderers
        for node in ast.walk(c)
        if isinstance(node, ast.Constant) and node.value in ("examine", "sweep", "ask")
    }
    assert rendered_kinds == {"examine", "sweep", "ask"}
