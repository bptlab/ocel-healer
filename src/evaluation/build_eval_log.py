"""Build the evaluation log: the order-management baseline plus event attributes.

The order-management baseline stores no event attributes (every
`event_<Type>` table has only `ocel_id` and `ocel_time`), so the
event-attribute issues can't be injected into it. This script derives two
attributes from data the log already contains, so they are consistent with
the rest of the log and a repair can recover them:

    resource  TEXT, every event table — the employee linked to the event via
              E2O (qualifier priority: employee, sales person, packer,
              shipper, forwarder), or 'system' for events without one
              (place order, pay order, payment reminder).
    amount    REAL, event_PayOrder only — the initial price of the paid order.

Deterministic: the same baseline always yields the same evaluation log.

Usage:
    python -m src.evaluation.build_eval_log [--src PATH] [--dst PATH]
"""

import argparse
import shutil
import sqlite3
from pathlib import Path

from src.order_management_corruption._common import DEFAULT_CLEAN_PATH

DEFAULT_EVAL_PATH = str(Path(DEFAULT_CLEAN_PATH).with_name("order-management-eval.sqlite"))

EMPLOYEE_QUALIFIERS = ["employee", "sales person", "packer", "shipper", "forwarder"]
NO_EMPLOYEE_RESOURCE = "system"


def build_eval_log(src: str = DEFAULT_CLEAN_PATH, dst: str = DEFAULT_EVAL_PATH) -> str:
    """Copy `src` to `dst` and add the `resource` / `amount` event attributes."""
    shutil.copy2(src, dst)
    priority = " ".join(
        f"WHEN '{q}' THEN {i}" for i, q in enumerate(EMPLOYEE_QUALIFIERS)
    )
    placeholders = ", ".join("?" * len(EMPLOYEE_QUALIFIERS))
    with sqlite3.connect(dst) as conn:
        # Temporary: the correlated lookups below are quadratic without it.
        conn.execute("CREATE INDEX tmp_eo_event ON event_object (ocel_event_id)")
        tables = [r[0] for r in conn.execute("SELECT ocel_type_map FROM event_map_type")]
        for m in tables:
            table = f"event_{m}"
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN resource TEXT')
            conn.execute(
                f"""
                UPDATE "{table}" SET resource = COALESCE((
                    SELECT eo.ocel_object_id FROM event_object eo
                    WHERE eo.ocel_event_id = "{table}".ocel_id
                      AND eo.ocel_qualifier IN ({placeholders})
                    ORDER BY CASE eo.ocel_qualifier {priority} END, eo.ocel_object_id
                    LIMIT 1
                ), ?)
                """,
                (*EMPLOYEE_QUALIFIERS, NO_EMPLOYEE_RESOURCE),
            )
        conn.execute('ALTER TABLE "event_PayOrder" ADD COLUMN amount REAL')
        conn.execute(
            """
            UPDATE "event_PayOrder" SET amount = (
                SELECT o.price FROM event_object eo
                JOIN object_Orders o
                  ON o.ocel_id = eo.ocel_object_id AND o.ocel_changed_field IS NULL
                WHERE eo.ocel_event_id = "event_PayOrder".ocel_id
                  AND eo.ocel_qualifier = 'order'
                LIMIT 1
            )
            """
        )
        conn.execute("DROP INDEX tmp_eo_event")
        conn.commit()
        conn.execute("VACUUM")
    return dst


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", default=DEFAULT_CLEAN_PATH)
    ap.add_argument("--dst", default=DEFAULT_EVAL_PATH)
    args = ap.parse_args()
    print(build_eval_log(args.src, args.dst))


if __name__ == "__main__":
    main()
