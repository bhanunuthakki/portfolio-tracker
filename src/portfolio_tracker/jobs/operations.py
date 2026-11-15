"""CLI for governed operations: catalog, preview, run, show.

    python -m portfolio_tracker.jobs.operations catalog
    python -m portfolio_tracker.jobs.operations preview schema_migrate --database-path DB
    python -m portfolio_tracker.jobs.operations run schema_migrate --database-path DB \
        --expected-plan-digest <digest> --idempotency-key <unique key>
    python -m portfolio_tracker.jobs.operations show <run_id> --database-path DB

`--database-path` defaults to the configured DATABASE_URL. Output is JSON with
revisions, counts, and file names only. A run exits 0 only when it succeeds or
replays a successful run.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from portfolio_tracker.services import operations


def _database_path(value: str | None) -> Path:
    if value:
        return Path(value)
    from portfolio_tracker.config import get_settings

    url = get_settings().resolved_database_url
    if not url.startswith("sqlite:///"):
        raise SystemExit("operations support SQLite databases only")
    return Path(url.removeprefix("sqlite:///"))


def _print(value: Any) -> None:
    sys.stdout.write(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="portfolio_tracker.jobs.operations")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("catalog")
    preview = sub.add_parser("preview")
    preview.add_argument("name")
    preview.add_argument("--database-path")
    run = sub.add_parser("run")
    run.add_argument("name")
    run.add_argument("--database-path")
    run.add_argument("--expected-plan-digest", required=True)
    run.add_argument("--idempotency-key", required=True)
    run.add_argument(
        "--approve-non-additive",
        action="store_true",
        help="Owner approval for a schema plan classified requires_approval.",
    )
    show = sub.add_parser("show")
    show.add_argument("run_id")
    show.add_argument("--database-path")
    args = parser.parse_args(argv)

    try:
        if args.command == "catalog":
            _print([op.model_dump() for op in operations.catalog()])
            return 0
        db_path = _database_path(args.database_path)
        if args.command == "preview":
            _print(operations.preview(args.name, db_path).model_dump(mode="json"))
            return 0
        if args.command == "show":
            found = operations.get_run(db_path, args.run_id)
            _print(found.model_dump(mode="json") if found else {"error": "run_not_found"})
            return 0 if found else 1
        inputs = {"approve_non_additive": True} if args.approve_non_additive else {}
        result = operations.run(
            args.name,
            db_path,
            expected_plan_digest=args.expected_plan_digest,
            idempotency_key=args.idempotency_key,
            inputs=inputs,
        )
        _print(result.model_dump(mode="json"))
        return 0 if result.status == "succeeded" else 1
    except operations.OperationError as exc:
        _print({"error": exc.code})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
