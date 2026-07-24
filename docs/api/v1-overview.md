# Portfolio Data Service — `/api/v1` consumer guide (Slice 1)

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

## Slice-1 resources

| Endpoint | Purpose |
| --- | --- |
| `GET /api/v1/health` | Service, database, migration, provider, contract health |
| `GET /api/v1/accounts` | Normalized accounts: canonical identity, inclusion state, detailed Tax treatment with evidence, per-account value + observation date, freshness |
| `GET /api/v1/portfolio-snapshot` | Bulk consumer read model: accounts + consolidated positions + five-way tax-bucket totals + equity fraction, one consistent read |
| `GET /api/v1/portfolio/positions` | Consolidated positions with per-lot tax treatment (see `positions-v1.md`) |
| `GET /api/v1/analytics/positioning` | Positioning cuts (asset type / sector / region / account type, concentration, correlations) + equity fraction, enveloped |

Wealthplan normally needs exactly one `portfolio-snapshot` read per refresh.

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

Slice 1 resources are read-only GETs; transient failures are safe to retry
with backoff. A structured error catalogue ships with Slice 2 alongside the
mutation endpoints' idempotency guide.

## Compatibility policy

- Additive optional fields → MINOR bump; consumers must tolerate unknown fields.
- Removing/renaming a field, changing an enum value or unit → MAJOR bump;
  consumers must reject a MAJOR they don't support (fail closed).
- Methodology changes increment `methodology_version` and never silently
  redefine a historical comparison.
- Warning codes are append-only.
