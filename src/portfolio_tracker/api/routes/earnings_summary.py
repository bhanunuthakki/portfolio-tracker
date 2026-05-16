"""Cross-project bridge to the companion `earnings-summary` repo.

Surfaces a single read-only endpoint that streams the latest research
brief HTML for a given ticker. Used by the Holdings / Trade Analysis
tabs to link from a position to its quarterly thesis brief without
duplicating the brief content into portfolio-tracker's DB.

The connector handles availability + path-traversal defense; this route
is just a FastAPI shim that returns 404 when the brief doesn't exist
and a plain HTML response when it does.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse, JSONResponse

from portfolio_tracker.services import earnings_summary as svc

router = APIRouter(prefix="/api/earnings-summary", tags=["earnings-summary"])


@router.get("/status")
def status_endpoint() -> JSONResponse:
    """Probe the companion project's availability. Used by the frontend
    to decide whether to show the enrichment badges at all."""
    return JSONResponse(
        content={
            "available": svc.is_available(),
            "db_path": str(svc._db_path()),  # noqa: SLF001 — read-only debug field
            "output_dir": str(svc._output_dir()),  # noqa: SLF001
        }
    )


@router.get("/briefs")
def list_briefs(ticker: str) -> JSONResponse:
    """List every available brief date for a ticker, newest-first.
    Used by the decision form to populate a dropdown of "which brief
    was current at decision time?"."""
    dates = svc.all_brief_iso_dates(ticker)
    return JSONResponse(
        content={
            "ticker": ticker.upper().strip(),
            "briefs": [
                {
                    "iso_date": d,
                    # relative path (matches what we store in
                    # trade_decisions.linked_brief_path)
                    "path": f"research/{ticker.upper().strip()}/{d}_report.html",
                }
                for d in dates
            ],
        }
    )


@router.get("/brief/{ticker}")
def brief_passthrough(ticker: str) -> FileResponse:
    """Stream the latest {date}_report.html for a ticker.

    Path is resolved by the connector (which enforces the ticker char
    set and confirms the resolved file is under the configured output
    dir — defense in depth against ../ traversal).
    """
    path = svc.latest_brief_path(ticker)
    if path is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"No brief available for ticker {ticker!r}",
        )
    return FileResponse(path, media_type="text/html")
