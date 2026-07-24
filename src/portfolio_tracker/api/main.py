"""FastAPI app factory.

Run with:
    uvicorn portfolio_tracker.api.main:app --reload --port 8000
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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

    return app


app = create_app()
