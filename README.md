# Portfolio Tracker

Self-hosted, single-user personal investment tracker. Aggregates holdings and
transactions from Plaid + SnapTrade, snapshots them daily, computes risk and
return metrics against multiple benchmarks, and provides a structured journal
for trade decisions.

Runs entirely on localhost. No multi-tenant auth, no cloud dependencies except
your aggregator(s) and yfinance.

> **Anonymized public template.** This repository is scrubbed of personal data
> — no names, addresses, account numbers, balances, holdings, or tax records,
> in either the current tree or its git history (history was rewritten to
> purge it). All portfolio-specific data and one-off scripts live locally
> (gitignored); the repo ships generic templates instead (`.env.example`,
> `CIO_CONTEXT.example.md`, `scripts/import_1099_example.py`). See
> **Privacy — what stays local** below. If you fork it, keep it that way —
> never commit PII.

## What it does

**Aggregation**
- Plaid for most US brokerages, SnapTrade for the ones Plaid handles poorly
  (Fidelity especially). One UI, both data paths normalized into the same
  tables.
- Per-Item `is_data_active` flag — keep redundant connections linked (so the
  Plaid Item slot stays occupied) while excluding their data from every
  aggregation. Useful when the same brokerage is reachable through two
  aggregators.
- Consolidated holdings rolled up by ticker with weighted-average cost
  basis, drill-down per account.

**Time series**
- Forward-snapshot writer (`jobs.snapshot`) appends today's positions
  permanently, so you keep history past Plaid's 24-month transaction
  retention floor.
- Walk-back reconstruction for any date older than the earliest snapshot,
  including ACATS in/out events, option assignment/expiration, and dividend
  reinvestments — anything SnapTrade emits as `cash/*` that changes share
  quantity.
- Cached `portfolio_values_daily` table so chart reads are O(days), not
  O(transactions).

**Returns vs benchmarks**
- Modified Dietz on a money-flow-matched basis. Same `V_start`, same
  cashflows, same dates across the portfolio and synthetic SPY/QQQ/policy
  portfolios — the gap between lines is true relative performance.
- User-defined **policy benchmark**: enter target weights (`/api/policy`),
  the chart adds a synthetic line for "what your intended allocation would
  have done." Missing-data weights renormalize automatically.
- Two carve-out toggles on the chart: subtract a fixed cash reserve (e.g.
  $30k in SGOV) and/or strip broad-market US ETFs (VTI/VOO/SPY/IVV/RSP) so
  you can see the active stock-picking portion in isolation.

**Risk metrics** (`/api/portfolio/beta`)
- Beta + alpha + R² (regression vs any benchmark), Sharpe, Sortino,
  Information Ratio, tracking error, annualized σ. Same carve-out toggles
  apply.

**Trade analysis & journaling**
- Per-ticker lifetime P&L (winners / losers / open). Detects ACATS-in /
  pre-history shares and flags rows as `cost_basis_unreliable` so the
  numbers aren't trusted past their data.
- **Pre-trade decision log** — write a thesis before clicking buy/sell;
  attach an outcome later.
- **Trade tags** — curated vocabulary (`panic_sold`, `held_too_long`,
  `thesis_validated`, etc.) attached to per-ticker holding windows for
  pattern mining.
- **CIO coaching panel** (`/api/coaching/tips`) — deterministic
  red-flag tips against the rubric in
  [`CIO_CONTEXT.md`](CIO_CONTEXT.md): IRR below the 10–12% bar on
  3+ year holds, concentration against human-capital buckets
  (Big-Tech/Ads, Startup/VC), stale theses, multiples-detachment trim
  candidates, drawdowns without thesis audits. Edit `CIO_CONTEXT.md`
  to retrain the agent; numeric thresholds live in
  `services/coaching.py`.
- **Earnings calendar** — yfinance pull, color-banded by proximity, only
  for held tickers.

**Data quality** (`/api/portfolio/data-quality`)
- Surfaces missing cost basis, untickered securities, missing prices,
  anomalous backfill days, stale items, sparse forward snapshots,
  overlapping aggregator connections, and missing policy-ticker benchmarks.
  Each finding has a recommended fix.

**Operations**
- One-shot `daily_refresh` orchestrates Plaid snapshot → SnapTrade sync →
  cache rebuild → benchmarks pull → earnings refresh.
- `dedupe_securities` merges duplicate `securities` rows that arose from
  Plaid + SnapTrade ingesting the same ticker under different opaque IDs.
- `migrate_broker_to_snaptrade` repoints accounts from Plaid to SnapTrade
  while preserving history.
- Daily SQLite backup (`jobs.backup --keep 365`) — point the folder at
  iCloud/Dropbox/OneDrive for off-machine durability.

## Architecture

```
backend  (FastAPI + SQLite)             frontend  (Vite + React + TS)
─────────────────────────────────       ────────────────────────────────
src/portfolio_tracker/                  frontend/src/
  api/routes/   REST endpoints            pages/        Dashboard, Holdings, …
  jobs/         Daily ingest CLIs         components/   Chart, ui primitives, …
  services/     Performance + DQ          api/          typed fetch wrapper
  models.py     SQLAlchemy schema         types.ts      mirrors Pydantic
  plaid_client.py
  snaptrade_client.py
  crypto.py     Fernet at-rest
alembic/        DB migrations
```

**Table convention**: every data table is sortable on every column, ascending
and descending. Headers use the shared `<SortableTh>` primitive
(`frontend/src/components/ui.tsx`) backed by the `useTableSort` hook — never a
bespoke clickable `<th>`. This holds for all current data tables (Holdings,
Transactions, Contributions, Trade analysis, Trade timeline, the Dashboard's
per-position alpha) and is a standing requirement for any table added later.
The two editable config grids (policy weights, human-capital buckets) are the
deliberate exception — their rows are inputs, not data.

## How the V series actually gets built

Neither Plaid nor SnapTrade returns a true time series of past portfolio
value. The pipeline is three-tiered, in priority order:

| Source | Coverage | Notes |
|---|---|---|
| `holdings_snapshots` (forward) | from the day you start running `jobs.snapshot` | Authoritative — straight from the institution. |
| `portfolio_values_daily` (cache) | reflects past walk-backs, plus today's snapshot | Avoids re-walking transactions on every chart read. Forward snapshots always win on overlap. |
| Walk-back reconstruction | up to ~24 months back (Plaid retention) or as far as SnapTrade's history reaches (~5 years for some) | Tracks two parallel state machines: positions (reverses every BUY/SELL/TRANSFER + share-moving `cash/*` event) and a cash adjustment series (so V_start doesn't collapse on net deployment). |

**Active-item filter**: every aggregation query goes through
`active_account_ids()`, which returns only accounts whose Item has
`is_data_active=True`. Set an Item inactive on the Accounts page when you
have the same brokerage connected through two aggregators.

**ACATS as cashflow**: outgoing share-side ACATS events have `amount=$0`
in the txn record, but represent value leaving the portfolio. The
performance pipeline values them at the close on the transfer date and
adds them to the cashflow series, so a $136k transfer doesn't show as a
phantom market loss.

## Cashflow direction inference

Brokers report `cash` transaction signs inconsistently. Each `(type, subtype)`
is classified by NAME, not sign:

| Subtype | Treated as |
|---|---|
| `deposit`, `contribution`, `rollover`, `wire`, `ach` | external inflow |
| `withdrawal` | external outflow |
| `transfer` (cash type) | sign-based (Plaid amount) |
| `transfer` (top-level) | external in/out by Plaid sign — except internal subtypes (`assignment`, `merger`, `spin off`, `split`, `stock distribution`) which are skipped |
| `external_asset_transfer_in/out` | net residual after matching internal moves → external in/out, valued at close |
| `optionassignment`, `optionexpiration`, `rei` | internal — share-moving with no external cash effect |
| `dividend`, `interest`, `buy`, `sell`, `fee` | internal — affects value, not basis |

Inspect via `GET /api/portfolio/cashflow-audit`.

## Data preservation

Plaid retains investment transactions for **only 24 months**. Two layers
protect against losing data:

1. **Application never deletes.** Every `holdings_snapshots`,
   `investment_transactions`, `prices`, and `benchmarks` row is appended
   forever. After 5 years of nightly snapshots you'll have 5 years of
   data even though Plaid only ever shows 2.
2. **Daily SQLite backup** to `backups/` —
   `python -m portfolio_tracker.jobs.backup --keep 365` writes a
   transactionally-consistent copy and prunes old ones.

## Privacy — what stays local

This repo is meant to be shareable **without exposing anyone's personal
financial data**. The application code is generic; your portfolio-specific
data and one-off scripts stay on your machine, gitignored.

Kept local (never committed):

- **`.env`** — Plaid/SnapTrade credentials, Fernet key. Template: `.env.example`.
- **`portfolio.db`**, **`backups/`** — your holdings, transactions, balances.
- **`tax_forms/`** — source 1099 PDFs (SSN digits, account numbers, addresses).
- **`CIO_CONTEXT.local.md`** — your CIO persona (name, employer, income).
  Template: `CIO_CONTEXT.example.md`. The advisor reads
  `CIO_CONTEXT.local.md` → `CIO_CONTEXT.md` → `CIO_CONTEXT.example.md`, so a
  fresh clone works on the generic example until you add your own.
- **`scripts/private/`** — one-shot importers hardcoded to your accounts
  (e.g. broker 1099 transcriptions). Template: `scripts/import_1099_example.py`.

Rule of thumb for any change: no real names, addresses, account
numbers/masks, dollar balances tied to a person, or local machine paths in
tracked files. CUSIPs (public security IDs) and illustrative round numbers
are fine.

## One-time setup

### 1. Backend

```bash
git clone https://github.com/<you>/portfolio-tracker.git
cd portfolio-tracker
python -m venv .venv

# Linux/macOS
source .venv/bin/activate
# Windows PowerShell
.\.venv\Scripts\Activate.ps1

pip install -e ".[dev]"
```

### 2. Aggregator credentials

**Plaid** (free Trial covers personal use up to 10 institutions): sign up at
<https://dashboard.plaid.com/signup>, pull `client_id` + production secret
from Team Settings → Keys.

**SnapTrade** (optional, recommended for Fidelity and for >24mo Robinhood
history): free Developer plan at <https://snaptrade.com/signup>, pull
`clientId` + `consumerKey`.

### 3. Generate a Fernet key (encrypts access tokens at rest)

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 4. Configure `.env`

```bash
cp .env.example .env
# Fill in: PLAID_CLIENT_ID, PLAID_SECRET, PLAID_ENV=production, FERNET_KEY,
# optionally SNAPTRADE_CLIENT_ID + SNAPTRADE_CONSUMER_KEY.
```

`.env` is gitignored. Never commit it.

### 5. Initialize the database

```bash
alembic upgrade head
```

### 6. Frontend

```bash
cd frontend && npm install && cd ..
```

## Daily use

```bash
# Terminal 1 — backend
uvicorn portfolio_tracker.api.main:app --reload --port 8000

# Terminal 2 — frontend
cd frontend && npm run dev
```

Open <http://localhost:5173>.

### Linking accounts

- **Plaid**: Accounts page → `+ Mine` (or `+ Spouse's` for separate
  phone-cache profiles) → Plaid Link opens.
- **SnapTrade**: Accounts page → `+ Mine` under "via SnapTrade" → portal
  opens in a new tab → return → click `Sync mine`.

### Pulling data

After linking at least one Item:

```bash
python -m portfolio_tracker.jobs.daily_refresh    # everything below, in one shot
# OR individually:
python -m portfolio_tracker.jobs.snapshot         # today's Plaid holdings
python -m portfolio_tracker.jobs.backfill         # 24mo of Plaid transactions
python -m portfolio_tracker.jobs.benchmarks --start 2024-01-01
python -m portfolio_tracker.jobs.prices --start 2024-01-01
python -m portfolio_tracker.jobs.earnings_calendar
python -m portfolio_tracker.jobs.scrub            # drop any non-investment accounts
```

The SnapTrade `Sync` button does its own snapshot + multi-year backfill
inline.

### Daily schedule

```cron
# Linux/macOS — 8 PM ET, weekdays
0 20 * * 1-5  cd /path/to/portfolio-tracker && .venv/bin/python -m portfolio_tracker.jobs.daily_refresh
0 21 * * *    cd /path/to/portfolio-tracker && .venv/bin/python -m portfolio_tracker.jobs.backup --keep 365
```

Windows: see `scripts/SCHEDULING.md`.

## Manual overrides

Some institutions don't expose `cost_basis` (notably SoFi via Plaid). The
**Holdings** page surfaces these as data-quality findings with inline forms:

- **Cost basis override** — total dollars paid (price × shares + fees) for
  an `(account, security)` pair. Optional `acquired_at` records the date
  the shares were originally bought (useful for ACATS-in lots that
  predate the receiving broker's snapshot history) — Trade Timeline and
  Position Alpha use it as the SPY counterfactual anchor instead of the
  receiving broker's first-snapshot proxy. Leave blank if unknown; the
  fallback (`min(earliest_snapshot, earliest_activity)`) preserves the
  prior behavior.
- **Ticker override** — for un-tickered securities (mutual funds with
  internal codes, foreign listings), enter a yfinance-compatible symbol,
  re-run `jobs.prices`.

## Maintenance jobs

```bash
# Merge duplicate `securities` rows when Plaid and SnapTrade keyed the
# same instrument under different opaque IDs (one-shot, idempotent):
python -m portfolio_tracker.jobs.dedupe_securities --commit

# Re-anchor Plaid-linked accounts onto SnapTrade equivalents (preserves
# history; flips Item to inactive on the old aggregator):
python -m portfolio_tracker.jobs.migrate_broker_to_snaptrade --commit
```

## Pre-push checklist

```bash
ruff format src && ruff check --fix src
pyright && basedpyright
cd frontend && npm run typecheck && npm run build
```

## Troubleshooting

- **Plaid Link won't open OAuth institutions** (Chase, Schwab, Capital One):
  add `http://localhost:5173/oauth-redirect` to Team Settings → API →
  Allowed redirect URIs.
- **SnapTrade portal succeeds but Sync 404s**: lost `user_secret` mid-flow
  (early bug, fixed). Click portal again — auto-recovery deletes the
  orphaned user and re-registers, persisting the secret.
- **`investments_transactions_get` returns 0**: Plaid sometimes needs ~2
  minutes to ingest historical transactions for a freshly linked Item.
  Wait, re-run `jobs.backfill`.
- **yfinance can't find a ticker**: set a `ticker_override`, re-run
  `jobs.prices`. The job has a Stooq fallback.
- **Vite shows ERR_CONNECTION_REFUSED**: IPv4/IPv6 binding mismatch.
  `vite.config.ts` binds `host: true` and proxies `127.0.0.1:8000`.
- **Phantom V steps in the historical chart**: usually a duplicate
  `securities` row (run `dedupe_securities`) or an ACATS that wasn't in
  the cashflow series (already handled, but check
  `/api/portfolio/cashflow-audit` for unclassified subtypes).

## License

MIT.
