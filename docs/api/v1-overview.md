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
| `GET /api/v1/cash-flows` | Canonical whole-portfolio flow ledger for `(start, end]`: approved provenance decisions first, then legacy aggregator-only owner override → name rule → subtype rule, plus priced unmatched share transfers; reports structural validity separately from approved source-history coverage (`cash_flow.twr_classification` v2) |
| `GET /api/v1/position-snapshots` | Historical observed holdings rows with `origin` markers (`broker` vs `manual` gap-fill); default window 90 days |
| `GET /api/v1/securities` | Security master: identifiers, cash-equivalent flag, asset type, sector/region Classification with source |
| `GET /api/v1/data-quality` | Machine-readable findings (category, severity, recommended action) under the envelope |
| `GET /api/v1/valuation-observations/{observation_key}` | Resolve a performance receipt key to sanitized valuation-source metadata; raw provider account locators and balance values are intentionally omitted |
| `GET /api/v1/analytics/positioning` | Positioning cuts (asset type / sector / region / account type, concentration, correlations) + equity fraction |
| `GET /api/v1/analytics/performance` | Modified-Dietz TWR vs cashflow-matched SPY/QQQ/policy counterfactuals over the canonical whole-portfolio flow ledger (`performance.modified_dietz` v2); `calculation_status=unavailable` suppresses derived fields unless exact requested opening/ending valuations, approved source evidence for every valued account over `(start, end]`, priceable external flows, and exact same-day or immediately previous U.S.-market closes are all available |
| `GET /api/v1/analytics/position-performance` | Split-normalized invested-position price/trade return and per-ticker dollar alpha vs cash-flow-matched price-return counterfactuals (`position_alpha.split_normalized_price_trade_modified_dietz` v3); derived fields are null with stable reason codes when share movements or price provenance cannot be reconciled |
| `GET /api/v1/analytics/risk` | Beta/alpha/R², Sharpe/Sortino, tracking error + max drawdown/recovery together (`risk.beta_drawdown` v2); drawdown fails closed with the performance reason codes when its return index is unavailable |
| `GET /api/v1/analytics/beta` | The regression half of `risk` alone — for consumers that don't need drawdown |
| `GET /api/v1/analytics/drawdown` | The loss-shaped half of `risk` alone — max drawdown, underwater curve, recovery, Calmar, and structural availability |
| `GET /api/v1/analytics/exit-quality` | Sell-side quality: regret vs holding, exit alpha vs SPY (`exit_quality.repricing` v1) |

Wealthplan normally needs exactly one `portfolio-snapshot` read per refresh.

An available performance result includes `series.equation_receipt`, the atomic
whole-portfolio bridge used for the headline: opening value, dated net external
flows, ending value, investment gain, shared Modified-Dietz denominator,
portfolio return, and SPY/QQQ/configured-policy counterfactual gains, returns,
dollar alpha, and percentage-point alpha. Opaque SHA-256 identifiers bind the
receipt to its flow-ledger, valuation, and resolved benchmark-price inputs.
Opening and ending observation keys can be resolved through
`/api/v1/valuation-observations/{observation_key}` to recover the source kind,
provider, capture time, and payload digest without exposing a statement/API
reference, source-row locator, or provider account locator.
Each benchmark equation also exposes the target date, actual source close date,
positive close value, whether resolution used the same day or the immediately
previous U.S. market close, and `return_basis` (`total_return_adjusted` or the
explicit `raw_price_fallback` used for legacy benchmark rows); a missing close
on a market session fails closed. The receipt retains the daily flow aggregate
consumed identically by the portfolio, SPY, QQQ, and policy equations and every
operative flow ID. Each operative row carries its source-event IDs, attestation
keys, active decision keys, authority, confidence, assumption code, and
effective-date basis. Date basis is one of `source_activity`, `source_process`,
`source_settlement`, `provider_posting`, or `owner_resolved`; statement-backed
decisions use the source activity date even when the linked provider row posts
later. The modeled cash walkback consumes that same canonical date (including
activity on the broker-observation anchor date), so the portfolio path and all
matched benchmark books do not place one economic flow on different days.
`reconstruction_certification` is independent of mathematical availability:
`observed_certified` means the opening and ending boundaries are complete broker
snapshots and broker-archive flow coverage is complete; `source_provisional`
means the calculation is available from complete provider delivery but the
provider did not assert possession of the broker's complete archive;
`modeled_provisional` means the opening is a transaction walkback; and
`unavailable` means the series did not pass the existing fail-closed gates.
A modeled opening remains provisional until position activity, account
lifecycle, broker cash/account-total closure, and eligible on-or-before-date
historical prices are all proven; the current schema does not claim those
additional closure proofs. Available source-provisional results carry
`broker_archive_coverage_not_complete`, and `source_coverage` exposes global,
per-account, and per-attestation archive status/ranges separately from provider
delivery completeness. Unavailable results set the receipt and all derived point fields to null and
return stable reason codes such as `portfolio_start_value_unavailable`,
`portfolio_end_value_unavailable`, `spy_benchmark_price_unavailable`,
`qqq_benchmark_price_unavailable`, `policy_benchmark_price_unavailable`, and
`unpriceable_holding_snapshot`. Certified modeled boundaries are recomputed
from current transactions, overrides, prices, and the complete observed anchor;
unversioned `portfolio_values_daily` backfill cache rows are not consumed.

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

For `analytics/*` responses, `meta.as_of` is the underlying broker observation
date, not the calculation run date. For an available performance result this
is the exact ending valuation boundary, whether it came from complete holdings
or complete whole-account totals; a calculation run today over a week-old book
therefore reads as stale (PRD §9.3). The calculation's own window travels in
the payload (`series.start_date`/`end_date` etc.).

The raw performance, position-performance, beta, and drawdown result objects
also carry required `methodology` and `methodology_version` literals. This lets
legacy endpoint consumers prove the calculation contract without depending on
the v1 envelope; v1 consumers must require the embedded markers to match meta.
Performance additionally identifies the exact `valuation_account_ids` and the
opening/ending value provenance. A requested ending boundary must be a complete
broker snapshot; a modeled opening is allowed only from a complete full-book
snapshot anchor. Partial or unsupported boundaries return stable reason codes
with `calculation_status=unavailable` rather than silently shortening the
window.

### Pagination

`transactions`, `cash-flows`, and `position-snapshots` use opaque keyset
cursors: pass `cursor` from the previous page's `next_cursor` until it is
null. `limit` is 1–1000 (default 500). There is no hidden row cap — the
legacy 5,000-row transactions ceiling does not apply to v1. A malformed
cursor returns the structured `INVALID_CURSOR` error (below).

For `cash-flows`, `net_external_cashflow_in` always describes the complete
requested window, not the current page. The ledger uses the same return-account
universe as performance—all active investment accounts plus any active account
with an in-window investment transaction—and exposes transaction origin, provider, rule,
component transaction, source-event/attestation/decision lineage, and
historical-price provenance. Statement supplementals are explicitly labeled
`statement_supplement` with a brokerage-statement provider; they are never
presented as aggregator-derived. `structural_is_complete` says whether the stored
rows can be classified and valued. `source_coverage` separately reports whether
every return account has current, owner-approved evidence for every date in the
end-of-day flow window `(start, end]`, including evidence reference/hash,
capture and approval times, supersession, and explicit gaps. `is_complete` is
true only when both checks pass; absence of ledger issues never implies source
coverage. Draft, superseded, malformed, partial, or missing attestations do not
count. When either check fails, the window total is null and performance returns
unavailable with a stable reason such as
`external_flow_source_coverage_incomplete` or
`external_share_movement_price_unavailable`. A provenance-managed transaction is
operative only through a current approved, non-provisional decision. An
approved provider decision may supersede a statement supplemental without
double-counting the old target. Multiple independent source events may
corroborate one target only when account, currency, effective date,
classification, and signed economics agree. Missing, conflicting, unresolved,
unapproved, provisional, or digest-drifted current decisions fail structurally.
Legacy aggregator transactions with no provenance records continue through the
deterministic classification rules.

Only an enhanced attestation can certify coverage: its declared candidate count
must equal the persisted source-event count, its canonical source-event-set hash
must match, and every event must have exactly one approved current resolved,
non-provisional decision whose payload digest recomputes exactly. Legacy
document-only attestations remain visible but are non-certifying. Stable
`validation_reason_codes` identify the failed gate.

Migration `0025` creates empty attestation tables deliberately. It does not
infer historical coverage from the rows already stored. Recording or replacing
an attestation is an operator-controlled reconciliation write that requires the
private source artifact, its SHA-256 digest, the exact account/date scope, and
owner approval.
Approved evidence is retained through append-only supersession. Accounts with
attestations cannot be hard-deleted until an explicit evidence-retention action
is taken; database backup and restore remain the recovery boundary.

### Deprecation headers

Superseded legacy endpoints now return `Deprecation: true` and
`Link: </api/v1/...>; rel="successor-version"`. Behavior is unchanged until
the Phase 5 retirement gate; new consumer code must use the v1 successor.

## The response envelope

Every decision-support response carries a `meta` block:

```jsonc
"meta": {
  "schema_version": "1.4.0",          // semver; MAJOR change ⇒ fail closed
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
