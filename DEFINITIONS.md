# Definitions

**Scope:** project
**Owner:** portfolio-tracker
**Inherits:** none

Canonical domain vocabulary. Use these terms verbatim in code (variables,
functions, types, columns), comments, commit messages, and conversation.
Seeded with the **positioning** vocabulary; extend as other areas are
standardized.

## Positioning

- **Positioning** — the breakdown of the *current* book (latest snapshot of
  active accounts) along several axes: asset type, sector, region, account
  tax-treatment, concentration, and per-ticker correlation. Surfaced as a
  section on the Holdings page. Always value-weighted by current market value.

- **Asset type** — coarse instrument class of a security, derived from the
  broker `type` code plus the cash-equivalent flag. Canonical values:
  `Stock`, `ETF`, `Mutual fund`, `Crypto`, `Cash`, `Other`. ADRs are `Stock`.
  Money-market funds (flagged cash-equivalent) are `Cash`.

- **Sector** — GICS-style sector for an individual equity (e.g. `Technology`).
  Funds have no single sector and are bucketed as **`ETF/Fund`**; crypto as
  `Crypto`; cash as `Cash`; an unclassified equity as `Unclassified`.

- **Region** — coarse domicile cut. Canonical values: `US`, `International`,
  `Unknown`. Derived from the classified country (`United States` ⇒ `US`).

- **Tax treatment** — an account's tax location. Canonical values: `Taxable`,
  `Retirement / tax-advantaged` (IRA / 401(k) / Roth / HSA / 529 / …),
  `Unknown`. Derived from account subtype, with a name fallback for feeds that
  omit it.

- **Classification (security)** — the cached `(sector, region)` for a security,
  stored in `security_classifications`. **Source** is `auto` (yfinance
  enrichment) or `manual` (user override); the enrichment job never overwrites
  a `manual` row.

- **Concentration** — how concentrated the book is across consolidated-by-
  security positions: top-N weight (`top1`/`top5`/`top10`), position count,
  **HHI** (Herfindahl-Hirschman Index, Σ of squared percent weights, 0–10000),
  and **effective holdings** (`1 / Σ(weight_fraction²)` — "behaves like ~N
  equally-weighted positions").

- **Correlation / beta (per ticker)** — a security's daily-return correlation
  and beta to a benchmark (`SPY`, `QQQ`, or the policy mix) over the window,
  via the OLS routine in `services/beta.py`. Cash is excluded.

## Portfolio Data Service

- **Portfolio Data Service** — the backend-first operating mode of Portfolio
  Tracker. It owns linked-account ingestion, normalized portfolio records,
  source-data corrections, provenance, freshness, and deterministic portfolio
  calculations exposed through the versioned `/api/v1` HTTP contract consumed
  by `earnings-summary` and `wealthplan`. It does not own research, valuation,
  portfolio judgment, recommendations, or the Owner Decision journal. Defined
  by `docs/design/portfolio_data_service_prd.md`; Phase 0 rulings in
  `docs/design/phase0_decision_addendum.md`.

- **Tax treatment (detailed, v1 API)** — the five-way account tax location the
  `/api/v1` contract preserves distinctly: `taxable`, `pretax`, `roth`, `hsa`,
  `unknown` (field `tax_treatment`, with `tax_treatment_evidence` and
  `tax_treatment_confidence`). Refines the coarse Positioning **Tax
  treatment** above (which may still group `Retirement / tax-advantaged` for
  display); consumers requiring Roth/HSA distinction (wealthplan tax buckets)
  must use the detailed field, never account-name inference.

- **Canonical account** — the API-owned reconciliation of duplicate provider
  representations of one real account. Every v1 account carries
  `canonical_account_id`, `included_in_totals`, and `exclusion_reason`
  (`duplicate_of_canonical` / `inactive` / `operator_excluded`); consumers
  never deduplicate by comparing balances.

## Source Event

**Definition.** An immutable observation of one exact record in an authoritative
cash-flow source, identified by the source document or provider record, its
stable locator, and a content digest. A Source event preserves what the source
said; it does not decide whether the movement is external, internal, duplicated,
or usable in a return calculation.
**Lives in.** `cashflow_source_events`, linked to
`cashflow_source_attestations`.
**Not to be confused with.** An `investment_transactions` row is the normalized
economic record; a Reconciliation decision interprets and links the Source
event.

## Reconciliation Decision

**Definition.** An append-only, attributable resolution of one Source event. It
records the normalized transaction target, external-flow classification,
effective date and date basis, authority, confidence, assumptions, methodology,
approval, and supersession history. Exactly one non-superseded decision may
exist for a Source event.
**Lives in.** `cashflow_reconciliation_decisions` and the apply receipt in
`cashflow_reconciliation_runs`.
**Not to be confused with.** A Transaction override is a legacy mutable
classification input; it is not a complete provenance or decision history.

## Effective External Flow

**Definition.** The single deduplicated cash movement into or out of the tracked
portfolio that is eligible for Modified-Dietz weighting after current approved
Reconciliation decisions, transaction authority, account-universe scope, and
supersession are applied. It is a derived projection, not a second persisted
economic record.
**Lives in.** The canonical external-flow ledger consumed by performance and
benchmark calculations.
**Not to be confused with.** A Source event may be corroborating, duplicated,
internal, excluded, or unresolved and therefore need not become an Effective
external flow.

## Reconstruction Certification

**Definition.** The fail-closed determination that a requested return window has
supported opening and ending boundaries, a dated account universe, complete
source-event dispositions, an Effective external flow set, required prices, and
a reproducible calculation receipt. Certification describes evidence sufficiency;
it is not the return value itself.
**Lives in.** Portfolio performance prerequisite assessment and its equation
receipt.
**Not to be confused with.** A mathematically balanced equation or an approved
source date range alone does not certify the underlying reconstruction.

## Canonical Portfolio Return

**Definition.** The whole-portfolio result produced only by
`performance.modified_dietz` methodology version `2` over an explicit
`(start_date, end_date]` window, the dated account universe, the canonical
Effective external flow ledger, and cash-flow-matched benchmark books. For a
"latest" lookback, `end_date` is first resolved to the latest complete broker
observation and the requested 90-, 180-, 365-, or 730-day opening date is then
calculated from that returned date. Identical explicit parameters against an
unchanged database must produce the same `series` and, when available, the same
input-derived `equation_receipt.calculation_id`.
**Lives in.** `services/performance.py` and
`GET /api/v1/analytics/performance`; the versioned API is the canonical fetch
for application consumers and agent answers.
**Not to be confused with.** An offline estimate, a current-holdings backcast,
position-level alpha, or an unversioned verbal calculation. If the endpoint
returns `calculation_status=unavailable`, consumers preserve that result and
its reason codes rather than substituting another number as the Canonical
portfolio return. A separately disclosed estimate remains an estimate.
