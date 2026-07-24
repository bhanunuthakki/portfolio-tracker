"""Generate the checked-in `/api/v1` OpenAPI artifact (Phase 0 ruling SC-6).

The artifact `docs/api/openapi.v1.json` is the compatibility contract both
consumers' fixture suites pin against. A pytest check regenerates it from the
app and fails on drift, so contract changes are always visible in review.

Generation builds a dedicated FastAPI app containing only the v1 routers — it
never inspects or serializes the live database (PRD §8).

Regenerate after changing any v1 route or model:

    python -m portfolio_tracker.api.openapi_v1
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from portfolio_tracker.services.v1_common import V1_SCHEMA_VERSION

ARTIFACT_PATH = Path(__file__).resolve().parents[3] / "docs" / "api" / "openapi.v1.json"


def build_v1_openapi() -> dict[str, Any]:
    """The OpenAPI document for the v1 surface only, deterministically."""
    from portfolio_tracker.api.routes import positions_v1, v1

    app = FastAPI(
        title="Portfolio Data Service API",
        version=V1_SCHEMA_VERSION,
        description=(
            "Versioned consumer-read contract for portfolio facts. "
            "See docs/api/v1-overview.md for envelope and freshness semantics."
        ),
    )
    app.include_router(v1.router)
    app.include_router(positions_v1.router)
    return app.openapi()


def render_v1_openapi() -> str:
    """Stable serialization: sorted keys, two-space indent, trailing newline."""
    return json.dumps(build_v1_openapi(), indent=2, sort_keys=True) + "\n"


def main() -> None:
    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(render_v1_openapi(), encoding="utf-8", newline="\n")
    print(f"Wrote {ARTIFACT_PATH}")


if __name__ == "__main__":
    main()
