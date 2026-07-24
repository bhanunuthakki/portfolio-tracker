"""FastAPI app factory.

Run with:
    uvicorn portfolio_tracker.api.main:app --reload --port 8000
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from portfolio_tracker.api.routes import (
    cio_advisor,
    coaching,
    cockpit,
    decision_support,
    earnings_summary,
    human_capital,
    overrides,
    plaid,
    policy,
    portfolio,
    positions_v1,
    snaptrade,
    v1,
)
from portfolio_tracker.config import get_settings
from portfolio_tracker.services.v1_history import InvalidCursorError

# Legacy consumer endpoints superseded by /api/v1 (provider PRD Phase 2:
# deprecation metadata precedes removal). Responses gain `Deprecation: true`
# and a `Link: <successor>; rel="successor-version"` header; behavior is
# unchanged until the Phase 5 retirement gate.
DEPRECATED_PATHS: dict[str, str] = {
    "/api/portfolio/holdings": "/api/v1/portfolio-snapshot",
    "/api/portfolio/transactions": "/api/v1/transactions",
    "/api/portfolio/positioning": "/api/v1/analytics/positioning",
    "/api/portfolio/performance": "/api/v1/analytics/performance",
    "/api/portfolio/position-alpha": "/api/v1/analytics/position-performance",
    "/api/portfolio/beta": "/api/v1/analytics/risk",
    "/api/portfolio/drawdown": "/api/v1/analytics/risk",
    "/api/portfolio/exit-quality": "/api/v1/analytics/exit-quality",
    "/api/portfolio/data-quality": "/api/v1/data-quality",
    "/api/portfolio/cashflow-audit": "/api/v1/cash-flows",
    "/api/plaid/items": "/api/v1/accounts",
}


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Portfolio Tracker", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(plaid.router)
    app.include_router(snaptrade.router)
    app.include_router(portfolio.router)
    app.include_router(positions_v1.router)
    app.include_router(v1.router)
    app.include_router(overrides.router)
    app.include_router(policy.router)
    app.include_router(decision_support.router)
    app.include_router(earnings_summary.router)
    app.include_router(coaching.router)
    app.include_router(cockpit.router)
    app.include_router(human_capital.router)
    app.include_router(cio_advisor.router)

    @app.get("/api/health")
    def health() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok"}

    @app.exception_handler(InvalidCursorError)
    def invalid_cursor_handler(  # pyright: ignore[reportUnusedFunction]
        request: Request, exc: InvalidCursorError
    ) -> JSONResponse:
        """The v1 structured error shape (PRD §7.6): stable code, sanitized
        message, request id, retryability, recovery guidance."""
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "INVALID_CURSOR",
                    "message": str(exc),
                    "request_id": str(uuid.uuid4()),
                    "resource": request.url.path,
                    "retryable": False,
                    "recovery": "Restart pagination from the first page (omit `cursor`).",
                }
            },
        )

    @app.middleware("http")
    async def deprecation_headers(  # pyright: ignore[reportUnusedFunction]
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        successor = DEPRECATED_PATHS.get(request.url.path)
        if successor is not None:
            response.headers["Deprecation"] = "true"
            response.headers["Link"] = f'<{successor}>; rel="successor-version"'
        return response

    return app


app = create_app()
