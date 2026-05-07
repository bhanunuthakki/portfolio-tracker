# Portfolio Tracker

A self-hosted, single-user personal investment tracker. Aggregates holdings and
transactions from Plaid + SnapTrade, snapshots them daily, and benchmarks the
portfolio against SPY / QQQ using money-flow-matched returns.

Runs entirely on localhost. No multi-tenant auth, no cloud dependencies except
your aggregator(s) of choice and yfinance for prices.

## What it does

- **Aggregates accounts** from Plaid (most US brokerages) and SnapTrade
  (covers Fidelity, which Plaid often can't). One UI for both.
- **Consolidates positions by ticker** with weighted-average cost basis,
  expandable per-account drill-down.
- **Daily snapshot job** appends today's positions to a permanent local
  database — even after Plaid drops the underlying transactions at the
  24-month retention edge, your historical record stays.
- **24-month transaction backfill** so you can see deposits, sells,
  dividends, and contributions immediately after linking.
- **Money-flow-matched return chart** comparing the portfolio against
  synthetic SPY / QQQ portfolios that received the same contributions on the
  same dates. The gap shows true relative performance.
- **Data-quality report** surfacing every issue (missing cost basis,
  un-tickered securities, unreliable backfilled days, stale items) with
  inline forms to fix the ones you can.
- **Local SQLite backups** via a one-line command.

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

### How performance over time actually works

Neither Plaid nor SnapTrade returns a true time series of past portfolio value.
We build it two ways:

| Path | Source | Coverage | Accuracy |
|---|---|---|---|
| **Forward snapshot** | `jobs/snapshot.py` writes one row per `(account, security)` per day | from the day you start running it | high — observed, straight from the institution |
| **Backfill** | `services/performance.py` walks `investment_transactions` backward from today's snapshot, valuing each day with `prices` from yfinance | up to ~24 months (Plaid retention) | **unreliable** — see below |

The backfill reconstructs *positions* but not *cash*. When you bought stock
during the backfill window with cash already in the account, reversing those
buys collapses the apparent starting portfolio because the funding cash is
invisible. The result is a wildly understated `V_start` and inflated returns.

**The pragmatic stance:** forward snapshots are observations; backfill is a
model. Run the snapshot job nightly. After ~1 week of forward data, the chart
auto-switches from backfill to forward-observed and the numbers become trustable.

### Money-flow-matched return (% comparison)

Both the portfolio and the synthetic SPY / QQQ benchmark portfolios use the
same `V_start` and receive the same cashflows on the same dates. Each day's
% return is Modified Dietz:

```
return_pct(d) = (V(d) − V_start − ΣC(≤d)) / (V_start + Σ C_i · w_i)
                                              where w_i = (d − d_i) / (d − d_0)
```

The synthetic SPY portfolio invests `V_start` in SPY at the start date and
each cashflow in SPY at its date — `V_spy(d) = total_shares × spy_close(d)`.
Same for QQQ. Both lines start at 0% and diverge by real performance.

When `V_start` is a backfill artifact, all three lines are inflated; the
chart includes a prominent warning explaining this and what's still
trustworthy (current value, contributions, market returns).

### Cashflow direction inference

Brokers report `cash` transaction signs inconsistently (some from the
investor's perspective, some from the cash account's). The audit endpoint
classifies each `(type, subtype)` as inflow / outflow / internal by NAME:

| Subtype | Treated as |
|---|---|
| `deposit`, `contribution`, `rollover`, `wire`, `ach` | external inflow |
| `withdrawal` | external outflow |
| `transfer` (cash type) | sign-based (uses Plaid's amount) |
| `transfer` (top-level type) | external inflow if Plaid amount is negative; outflow if positive — except internal subtypes (`assignment`, `merger`, `spin off`, `split`) which are skipped |
| `dividend`, `interest`, `buy`, `sell`, `fee` | internal — affects value, not basis |

Inspect at `GET /api/portfolio/cashflow-audit`.

### Local data preservation

Plaid retains investment transactions for **only 24 months**. Two layers
protect against losing data:

1. **Application never deletes.** Every `holdings_snapshots`, `investment_transactions`, `prices`, and `benchmarks` row is appended forever. After 5 years of nightly snapshots, you'll have 5 years of data even though Plaid only ever shows 2.
2. **Daily SQLite backup** to `backups/` — `python -m portfolio_tracker.jobs.backup --keep 365` writes a transactionally-consistent copy and prunes old ones. Point that folder at iCloud / Dropbox / OneDrive for off-machine durability.

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

#### Plaid (free Trial plan covers personal use up to 10 institutions)

Sign up at <https://dashboard.plaid.com/signup>. Plaid auto-approves Trial in
US/CA. Pull `client_id` + production secret from Team Settings → Keys.

#### SnapTrade (optional, recommended for Fidelity)

Plaid's Fidelity coverage is patchy. SnapTrade is a separate aggregator with
better Fidelity support. Free Developer plan: <https://snaptrade.com/signup>.
Pull `clientId` + `consumerKey`.

### 3. Generate a Fernet key (encrypts access tokens at rest)

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 4. Configure `.env`

```bash
cp .env.example .env
# Edit .env, fill in: PLAID_CLIENT_ID, PLAID_SECRET, PLAID_ENV=production,
#                    FERNET_KEY, optionally SNAPTRADE_CLIENT_ID + SNAPTRADE_CONSUMER_KEY
```

`.env` is gitignored. Never commit it.

### 5. Initialize the database

```bash
alembic upgrade head
```

### 6. Frontend

```bash
cd frontend
npm install
cd ..
```

## Daily use

Start both servers in separate terminals:

```bash
# Terminal 1 — backend
uvicorn portfolio_tracker.api.main:app --reload --port 8000

# Terminal 2 — frontend
cd frontend && npm run dev
```

Open <http://localhost:5173>.

### Linking accounts

- **Plaid**: Accounts page → `+ Mine` (or `+ Spouse's` for separate phone-cache profiles) → Plaid Link opens → log in to your brokerage.
- **SnapTrade**: Accounts page → `+ Mine` (under "via SnapTrade") → portal opens in new tab → log in → return → click `Sync mine`.

### Pulling data

After linking at least one Item:

```bash
python -m portfolio_tracker.jobs.snapshot       # write today's holdings snapshot
python -m portfolio_tracker.jobs.backfill       # 24mo of investment transactions (Plaid items)
python -m portfolio_tracker.jobs.benchmarks --start 2024-01-01
python -m portfolio_tracker.jobs.prices --start 2024-01-01
python -m portfolio_tracker.jobs.scrub          # drop any non-investment accounts that slipped in
```

The SnapTrade `Sync` button does its own snapshot + 24mo backfill inline — the
`snapshot` and `backfill` jobs only touch Plaid items.

### Daily schedule

```cron
# Linux/macOS — 8 PM ET, after market close (weekdays)
0 20 * * 1-5  cd /path/to/portfolio-tracker && .venv/bin/python -m portfolio_tracker.jobs.snapshot
0 20 * * 1-5  cd /path/to/portfolio-tracker && .venv/bin/python -m portfolio_tracker.jobs.benchmarks --start 2024-01-01
0 21 * * *    cd /path/to/portfolio-tracker && .venv/bin/python -m portfolio_tracker.jobs.backup --keep 365
```

Windows: Task Scheduler with `.\.venv\Scripts\python.exe -m portfolio_tracker.jobs.snapshot` daily at 8 PM, working directory set to the project root.

## Manual overrides

Some institutions don't expose `cost_basis` (notably SoFi via Plaid). The
**Holdings** page surfaces these as data-quality findings with inline forms:

- **Cost basis override** — enter total dollars paid (price × shares + fees) for an `(account, security)` pair. Once saved, weighted-avg cost and unrealized P&L populate.
- **Ticker override** — for un-tickered securities (mutual funds with internal codes, foreign listings), enter a yfinance-compatible symbol. Re-run `jobs.prices` to populate history.

## Pre-push checklist

```bash
ruff format src && ruff check --fix src
pyright && basedpyright
cd frontend && npm run typecheck && npm run build
```

## Troubleshooting

**Plaid Link won't open OAuth institutions (Chase, Schwab, Capital One).**
Add `http://localhost:5173/oauth-redirect` to Team Settings → API → Allowed
redirect URIs.

**SnapTrade portal succeeds but `Sync` returns 404.** The user_secret was
lost mid-flow (early bug, fixed). Click portal again — auto-recovery
deletes the orphaned SnapTrade user and re-registers, persisting the secret
to the `snaptrade_users` table.

**`investments_transactions_get` returns 0.** Plaid sometimes needs ~2
minutes to ingest historical transactions for a freshly linked Item. Wait
and re-run `jobs.backfill`.

**yfinance can't find a ticker.** Set a `ticker_override` via the Holdings
page → Data Quality finding, then re-run `jobs.prices`. The job has a
Stooq fallback for tickers yfinance fails on, but exotic listings may still
require manual handling.

**Vite says ready but browser shows ERR_CONNECTION_REFUSED.** IPv4/IPv6
binding mismatch. The shipped `vite.config.ts` binds `host: true` (all
interfaces) and proxies to `127.0.0.1:8000` to avoid this.

## License

MIT.
