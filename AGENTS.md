# Portfolio Tracker — Project Rulebook

Layers on the global `C:\Users\Bhanu\.gemini\AGENTS.md`; it does NOT repeat global rules (safety, TDD, Deep Modules, code standards, pre-push order). Only repo-specific facts live here.

## What this is

Self-hosted, **single-user** personal investment tracker. Pulls holdings + transactions from Plaid and SnapTrade, snapshots daily, computes Modified-Dietz returns and risk metrics vs benchmarks, journals trades, and runs a deterministic CIO coaching panel. Runs entirely on localhost — no multi-tenant auth, no cloud except the aggregators + yfinance/Gmail. (This is why the Hardening Fleet caps at L1 here unless explicitly opted up.)

## Layout & run

- Backend: FastAPI + SQLAlchemy 2.0 + SQLite, source under `src/portfolio_tracker/` (`api/routes/`, `jobs/`, `services/`, `models.py`). Migrations in `alembic/`.
- Frontend: Vite + React + TS under `frontend/`.
- Backend dev: `uvicorn portfolio_tracker.api.main:app --reload --port 8000` (ASGI app = `portfolio_tracker.api.main:app`).
- Frontend dev: `cd frontend && npm run dev` → http://localhost:5173.
- Daily ingest CLIs: `python -m portfolio_tracker.jobs.<name>` (`daily_refresh`, `snapshot`, `backfill`, `prices`, `benchmarks`, `backup`, …).
- DB init / migrate: `alembic upgrade head`.

## Toolchain (read from `pyproject.toml` / `frontend/package.json`)

Pre-push, in order (global order applies):
1. `ruff format src` then `ruff check --fix src` — lint config `select`s E/F/I/N/UP/B/A/RET/SIM/RUF; RUF001-003 + E501 ignored (typographic symbols ×, −, σ are intentional).
2. `pyright` — **strict** mode, resolves deps from the root `.venv` (`venvPath="."`, `venv=".venv"`); pass `--pythonpath <interp>` from a worktree where `.venv` is absent.
3. `pytest` — `testpaths=["tests"]`, `pythonpath=["src"]` (the `tests/` dir is not yet created).
4. `cd frontend && npm run typecheck && npm run build` (typecheck = `tsc --noEmit` over both tsconfigs).

## Vocabulary

`DEFINITIONS.md` (repo root) is authoritative — use its terms verbatim (Positioning, Asset type, Sector, Region, Tax treatment, Classification, Concentration, Correlation/beta). Propose an addition there before coining any new domain term.

## Secret & data files — never commit, never log (global Safety Rules 1, 3, 4)

`credentials.json` and `token.json` (Gmail OAuth, repo root), `.env` / `.env.*` (Plaid/SnapTrade keys + Fernet key), and every `*.db` / `*.bak` (`portfolio.db`, `*.db.*.bak` — real holdings + balances) plus `CIO_CONTEXT.local.md` / `*.local.md` and `tax_forms/` are the files the global redaction + commit-halt + no-log rules cover here. All are gitignored; if any appears in a staged diff, halt. Access tokens are Fernet-encrypted at rest via `crypto.py` — never decrypt-and-print.

## Financial-data correctness and state changes

- Outputs are decision support, not personalized financial or tax advice.
  Display valuation/price timestamps, data provider, account coverage, and any
  stale or partial-ingest warning next to affected metrics.
- Reconcile aggregator identifiers and transactions before computing returns;
  never silently substitute the last good balance for a failed refresh.
- Any schema migration, backfill, account relink, or destructive correction
  requires a verified backup and an explicit preview of affected accounts,
  date range, and row counts. Ask before operating on the user's live database.
- Restore is part of backup verification: prove a backup opens and passes basic
  integrity checks without exposing holdings in logs.
- Keep deterministic portfolio math separate from LLM coaching. The panel may
  explain or challenge calculated results but may not invent holdings, prices,
  transactions, or tax facts.
