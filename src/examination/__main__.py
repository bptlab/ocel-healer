# src/examination/__main__.py

"""CLI for the examination agent.

Usage:
    python -m src.examination data/synthetic/order-management-corrupted.sqlite
    python -m src.examination path/to/log.sqlite --model qwen3:8b

Writes examination_profile.json, examination_guide.json and
examination_report.md into <db_dir>/examination/<db_stem>/ — next to the log,
which is where the dashboard and the repair agents look hints up.
"""

import argparse
import time
from pathlib import Path

from src.examination.examiner_agent import examine_database
from src.llm.client import MODEL


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db_path", help="OCEL 2.0 sqlite file to examine")
    parser.add_argument("--model", default=MODEL, help=f"LLM model (default: {MODEL})")
    parser.add_argument(
        "--base-dir",
        default=None,
        help="artifact directory (default: <db_dir>/examination — where repair hints are looked up)",
    )
    args = parser.parse_args()

    base_dir = Path(args.base_dir) if args.base_dir else None  # None → next to the db

    t0 = time.time()

    def progress(name: str, i: int, total: int) -> None:
        print(f"[{i}/{total}] {name} ({time.time() - t0:.0f}s)", flush=True)

    report = examine_database(
        args.db_path, model=args.model, base_dir=base_dir, on_progress=progress
    )
    print(f"Done in {time.time() - t0:.0f}s -> {report}")


if __name__ == "__main__":
    main()
