# Portfolio Data Service — `/api/v1` consumer guide

The versioned read contract for portfolio facts, consumed by `earnings-summary`
and `wealthplan`. Governing documents: `docs/design/portfolio_data_service_prd.md`
(requirements) and `docs/design/phase0_decision_addendum.md` (ratified contract
decisions, cited below as SC-n).

**Compatibility artifacts** (both checked in, drift-gated by pytest):

- OpenAPI: `docs/api/openapi.v1.json` — regenerate with
  `python -m portfolio_tracker.api.openapi_v1`
- Fixtures: `docs/api/fixtures/v1/*.json` — regenerate with
  `python -m portfolio_tracker.api.fixtures_v1`

Consumer contract tests should pin against the fixtures, not against a live
database. Every fixture value is synthetic.

## Discovery (SC-4)

Default base URL `http://127.0.0.1:8000`; consumers override with the
`PORTFOLIO_TRACKER_API_URL` environment variable. Loopback-only. No
authentication while loopback-only (SC-5); `Authorization: Bearer` is reserved
for future non-loopback use.

Probe `GET /api/v1/health` before decision-grade reads: it reports database
and migration state, per-provider link health (counts and times only — never
balances), the latest snapshot date, and staleness.

## Resources

| Endpoint | Purpose |
| --- | --- |
| `GET /api/v1/health` | Service, database, migration, provider, contract health |
| `GET /api/v1/accounts` | Normalized accounts: canonical identity, inclusion state, detailed Tax treatment with evidence, per-account value + observation date, freshness |
| `GET /api/v1/portfolio-snapshot` | Bulk consumer read model: accounts + consolidated positions + five-way tax-bucket totals + equity fraction, one consistent read |
| `GET /api/v1/portfolio/positions` | Consolidated positions with per-lot tax treatment (see `positions-v1.md`) |
| `GET /api/v1/transactions` | Cursor-paginated normalized transactions with override + effective TWR classification (default window 730 days) |
| `GET /api/v1/cash-flows` | Canonical whole-portfolio flow ledger for `(start, end]`: owner override → name rule → subtype rule, plus priced unmatched share transfers (`cash_flow.twr_classification` v2) |
| `GET /api/v1/position-snapshots` | Historical observed holdings rows with `origin` markers (`broker` vs `manual` gap-fill); default window 90 days |
| `GET /api/v1/securities` | Security master: identifiers, cash-equivalent flag, asset type, sector/region Classification with source |
| `GET /api/v1/data-quality` | Machine-readable findings (category, severity, recommended action) under the envelope |
| `GET /api/v1/analytics/positioning` | Positioning cuts (asset type / sector / region / account type, concentration, correlations) + equity fraction |
| `GET /api/v1/analytics/performance` | Modified-Dietz TWR vs cashflow-matched SPY/QQQ/policy counterfactuals over the canonical whole-portfolio flow ledger (`performance.modified_dietz` v2); `calculation_status=unavailable` suppresses derived fields when an in-kind external flow cannot be identified or valued |
| `GET /api/v1/analytics/position-performance` | Split-normalized invested-position price/trade return and per-ticker dollar alpha vs cash-flow-matched price-return counterfactuals (`position_alpha.split_normalized_price_trade_modified_dietz` v3); derived fields are null with stable reason codes when share movements or price provenance cannot be reconciled |
| `GET /api/v1/analytics/risk` | Beta/alpha/R², Sharpe/Sortino, tracking error + max drawdown/recovery together (`risk.beta_drawdown` v2); drawdown fails closed with the performance reason codes when its return index is unavailable |
| `GET /api/v1/analytics/beta` | The regression half of `risk` alone — for consumers that don't need drawdown |
| `GET /api/v1/analytics/drawdown` | The loss-shaped half of `risk` alone — max drawdown, underwater curve, recovery, Calmar, and structural availability |
| `GET /api/v1/analytics/exit-quality` | Sell-side quality: regret vs holding, exit alpha vs SPY (`exit_quality.repricing` v1) |

Wealthplan normally needs exactly one `portfolio-snapshot` read per refresh.

**Deferred:** `GET/POST /api/v1/sync-runs` requires a run-log schema migration
on the live database and ships with the Phase 3 migration batch (backup +
preview + owner approval). Until then, sync recency comes from `health` and
each account's `last_successful_sync_at`.

### Choosing a risk resource

`risk` returns beta and drawdown together in one call. The two halves have
very different costs — the beta regression walks the whole position-alpha
series, drawdown does not — so prefer `beta` or `drawdown` when you only
need one. All three agree over the same window; picking the narrower
resource is purely about not paying for work you discard.

### Analytics envelope semantics

For `analytics/*` responses, `meta.as_of` is the underlying **holdings
observation date**, not the query window end — a calculation run today over a
week-old book reads as stale (PRD §9.3). The calculation's own window travels
in the payload (`series.start_date`/`end_date` etc.).

The raw performance, position-performance, beta, and drawdown result objects
also carry required `methodology` and `methodology_version` literals. This lets
legacy endpoint consumers prove the calculation contract without depending on
the v1 envelope; v1 consumers must require the embedded markers to match meta.

### Pagination

`transactions`, `cash-flows`, and `position-snapshots` use opaque keyset
cursors: pass `cursor` from the previous page's `next_cursor` until it is
null. `limit` is 1–1000 (default 500). There is no hidden row cap — the
legacy 5,000-row transactions ceiling does not apply to v1. A malformed
cursor returns the structured `INVALID_CURSOR` error (below).

For `cash-flows`, `net_external_cashflow_in` always describes the complete
requested window, not the current page. The ledger uses the same valued-account
universe as performance and exposes provider, rule, component transaction, and
historical-price provenance. If an unmatched share transfer cannot be priced
from a close on or within 14 days before the event, `is_complete=false`, the
window total is null, and `issues` carries `share_transfer_price_unavailable`;
performance fails closed over that window instead of silently dropping value.

### Deprecation headers

Superseded legacy endpoints now return `Deprecation: true` and
`Link: </api/v1/...>; rel="successor-version"`. Behavior is unchanged until
the Phase 5 retirement gate; new consumer code must use the v1 successor.

## The response envelope

Every decision-support response carries a `meta` block:

```jsonc
"meta": {
  "schema_version": "1.0.0",          // semver; MAJOR change ⇒ fail closed
  "generated_at": "2026-07-23T06:00:00Z",
  "as_of": "2026-07-22",              // observation date; null ⇒ no data
  "currency": "USD",
  "source_providers": ["plaid", "snaptrade"],
  "account_coverage": {
    "included_account_ids": [1, 2, 3],
    "excluded_account_ids": [4],
    "lagging_account_ids": []          // included accounts whose snapshot < as_of
  },
  "last_successful_sync_at": "2026-07-22T12:00:00Z",
  "is_partial": false,                 // true ⇒ some included account is lagging
  "is_stale": false,                   // true ⇒ as_of older than 5 calendar days
  "warnings": [ {"code": "…", "message": "…", "scope": "…"} ],
  "methodology": "portfolio_snapshot.bulk",
  "methodology_version": "1",
  "links": { "accounts": "/api/v1/accounts", … }
}
```

### Data-state semantics

- **No data** — `as_of: null` + warning `NO_DATA`. Distinct from stale.
- **Stale** — `is_stale: true` + `STALE_HOLDINGS`: the whole book is older than
  the 5-day budget. Values are last-valid, dated — never re-labeled current.
- **Partial** — `is_partial: true` + `PARTIAL_COVERAGE`: at least one included
  account's provider refresh is lagging; totals may understate the book. Never
  merge a partial read field-by-field with an older complete one — the unit of
  fallback is one coherent snapshot.
- Stable warning codes (append-only): `NO_DATA`, `STALE_HOLDINGS`,
  `PARTIAL_COVERAGE`, `UNKNOWN_TAX_TREATMENT`, `NO_CANONICAL_LINK`,
  `CALCULATION_UNAVAILABLE`.

## Numeric and date conventions

- Money and quantities are **decimal strings** (`"12000.000000"`). Parse as
  Decimal; never float-accumulate.
- Percent fields state their unit: `percent_of_portfolio` and `weight_pct` are
  PERCENT (0–100); `equity_fraction` is a FRACTION (0–1) and carries
  `"unit": "fraction"` explicitly.
- Dates are ISO `YYYY-MM-DD`; timestamps ISO-8601 with timezone.

## Detailed Tax treatment (SC-1)

Field `tax_treatment`, enum exactly: `taxable | pretax | roth | hsa | unknown`.
Companions: `tax_treatment_evidence` (e.g. `"subtype:roth ira"`) and
`tax_treatment_confidence` (`high | medium | low`).

Rules for consumers:

- Never infer treatment from account display names.
- `unknown` must not be silently classified (wealthplan: block automatic
  bucket replacement; the account is identified in `warnings`).
- Wealthplan mapping: `taxable→TAXABLE`, `pretax→PRETAX`, `roth→ROTH`,
  `hsa→HSA`.

## Canonical accounts (SC-2)

Each account carries `canonical_account_id`, `included_in_totals`, and
`exclusion_reason` (`operator_excluded` today; `duplicate_of_canonical` /
`inactive` reserved). Consumers must sum only `included_in_totals=true`
accounts and must never detect duplicates by comparing balances. An excluded
account still reports its last observed value, dated, plus a
`NO_CANONICAL_LINK` warning when no counterpart link is modeled.

## Equity fraction (SC-3)

Served in both `portfolio-snapshot` and `analytics/positioning` — same
methodology (`equity_fraction.cash_equivalent`, version 1), same policy:

> Cash = securities classified Cash by broker type code or the
> `is_cash_equivalent` flag (money-market funds count as cash). Everything
> else counts as equity. No ticker allowlist.

`equity_fraction` is `null` (with `CALCULATION_UNAVAILABLE`) when the
denominator is zero — never silently `0`.

## Errors and retries

All v1 resources are read-only GETs; transient failures are safe to retry
with backoff. Contract errors use the structured shape (PRD §7.6):

```jsonc
{
  "error": {
    "code": "INVALID_CURSOR",        // stable, machine-readable
    "message": "cursor is not valid base64",
    "request_id": "…",
    "resource": "/api/v1/transactions",
    "retryable": false,
    "recovery": "Restart pagination from the first page (omit `cursor`)."
  }
}
```

Current catalogue: `INVALID_CURSOR` (400). Validation failures on query
parameters use FastAPI's standard 422 shape. Provider bodies, credentials,
holdings, and balances never appear in errors. The operator-mutation
idempotency guide ships with the sync-runs migration.

## Compatibility policy

- Additive optional fields → MINOR bump; consumers must tolerate unknown fields.
- Removing/renaming a field, changing an enum value or unit → MAJOR bump;
  consumers must reject a MAJOR they don't support (fail closed).
- Methodology changes increment `methodology_version` and never silently
  redefine a historical comparison.
- Warning codes are append-only.
