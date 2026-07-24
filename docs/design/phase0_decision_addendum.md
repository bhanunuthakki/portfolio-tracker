# Portfolio Data Service Program — Phase 0 Decision Addendum

**Status:** Adopted for implementation on 2026-07-23 under the owner's directive to
execute the program; any decision here may be re-opened by the owner before its first
dependent removal ships. Identical copies live in all three repos' `docs/design/`.
**Closes:** the open decisions in `portfolio_data_service_prd.md` §17,
`portfolio_tracker_api_migration_prd.md` §15, and
`portfolio_intelligence_consolidation_prd.md` §16.

---

## 1. Shared contract decisions (bind all three repositories)

### SC-1 — Detailed Tax treatment field and enum

Field name: **`tax_treatment`**. Enum (lowercase strings, exactly):
`taxable | pretax | roth | hsa | unknown`.

- Carried on every v1 account resource, with companion fields
  `tax_treatment_evidence` (e.g. `subtype:roth ira`) and
  `tax_treatment_confidence` (`high | medium | low`).
- Derivation extends the existing `services/positions_v1.tax_treatment` inference,
  which already distinguishes Roth and HSA before collapsing them: `roth` substring →
  `roth`; `hsa` → `hsa`; 401k/IRA/deferred subtypes → `pretax`; brokerage → `taxable`;
  else `unknown`.
- **Refinement (ratified during M1 dual-read parity):** the provider owns the
  account-NAME fallback tier at `medium` confidence — required because SnapTrade
  omits `subtype` for some institutions (live evidence: Fidelity "BrokerageLink",
  "BrokerageLink Roth", "Health Savings Account", "…401(K) PLAN" all arrive with
  `subtype=None`). A bare "BrokerageLink" is the owner-confirmed self-directed
  401(k) window → `pretax`. Cash-account subtypes (`checking` etc.) → `taxable`
  (medium). A bare `individual`/`joint` subtype → `taxable` at `low` confidence
  (both consumers' legacy heuristics agreed; blocking it as `unknown` would have
  broken parity for zero benefit). Consumers must still block on `unknown`.
- The coarse 4-way lot enum currently emitted by `GET /api/v1/portfolio/positions`
  (`taxable/tax_deferred/tax_free/unknown`) has **no live consumer** (the
  earnings-summary client still reads legacy `/api/portfolio/*` + `/api/plaid/items`).
  It is upgraded in place to the 5-way enum as part of Slice 1; the positions-v1 doc
  is updated accordingly. Consumers map: wealthplan `pretax→PRETAX`, `roth→ROTH`,
  `hsa→HSA`, `taxable→TAXABLE`, `unknown→blocked` (no silent mapping).

### SC-2 — Canonical account identity and inclusion

Every v1 account resource exposes:

- `account_id` (stable Portfolio Tracker id), `canonical_account_id` (the id of the
  canonical representative; equals `account_id` when the account is itself canonical),
- `included_in_totals` (bool), and `exclusion_reason`
  (`null | duplicate_of_canonical | inactive | operator_excluded`),
- provider (`plaid | snaptrade`), institution, type/subtype, active state,
  holdings/valuation date, last successful sync.

Cross-provider duplicates (e.g. the same brokerage linked through both Plaid and
SnapTrade) are reconciled by the provider service; consumers never compare balances
to detect duplicates.

### SC-3 — Equity fraction placement

`equity_fraction` is **materialized in `GET /api/v1/portfolio-snapshot`** AND served
in full detail by `GET /api/v1/analytics/positioning` — same methodology name and
`methodology_version`, same cash-equivalent policy statement, same as-of dates. The
snapshot embeds the number plus methodology/version/warnings and links to the
positioning resource for the full input breakdown.

### SC-4 — Service discovery

Default base URL `http://127.0.0.1:8000`, overridden per consumer by the env var
**`PORTFOLIO_TRACKER_API_URL`** (the name the earnings-summary client already reads;
wealthplan adopts the same name). Loopback bind remains the default. The at-logon
`PortfolioTrackerApiServer` scheduled task remains the availability mechanism.

### SC-5 — Local bearer token

**Deferred** while the service is loopback-only. The contract reserves standard
`Authorization: Bearer` semantics for any future non-loopback use; no consumer sends
or requires a token today. Revisit before any non-loopback binding.

### SC-6 — OpenAPI artifact policy

The generated OpenAPI JSON is **checked in** at
`portfolio-tracker/docs/api/openapi.v1.json` and a pytest contract test regenerates
it from the app and fails on drift (CI = the repo's pre-push pytest gate). Generation
never inspects or serializes the live database. Sanitized fixtures live under
`portfolio-tracker/docs/api/fixtures/v1/` and are the single fixture suite both
consumers' contract tests load.

### SC-7 — Rollback windows and parity tolerances (shared)

- Rollback window for every cutover (wealthplan direct-DB, ES journal, surface
  cutover, bridge removal): **30 days** from the owner-approved gate.
- Money parity tolerance (dual-read aggregate comparisons): **$1 per aggregate**
  (consistent with the whole-dollar display rule).
- Equity-fraction parity tolerance: **0.005** absolute.
- Date parity: as-of dates must match exactly or carry an explained correction.

---

## 2. Portfolio Tracker decisions (provider PRD §17)

| # | Decision | Ruling |
| --- | --- | --- |
| 1 | Service name | **Portfolio Data Service**; added to `DEFINITIONS.md` |
| 2 | Discovery | SC-4 |
| 3 | OpenAPI | SC-6 |
| 4 | Bearer token | SC-5 |
| 5 | Rollback window | SC-7 (30 days) |
| 6 | Benchmark policy | **Split.** Benchmark selection/config needed for calculation stays an API-owned Portfolio Tracker setting (`policy_weights` rows that parameterize math). Owner-intent posture (target weights expressing strategy) migrates to Earnings Summary's Portfolio Posture in Phase 3 |
| 7 | Historical Cockpit/CIO artifacts | `action_queue` history + `monthly_briefs` (+ jobs metadata) = **import-provenance**; `chat_sessions`/`chat_turns` = **archive-only** (retained in the pre-migration backup, not imported) |
| 8 | Tax treatment | SC-1 |
| 9 | Canonical accounts | SC-2 |
| 10 | Equity fraction | SC-3 |

## 3. Wealthplan decisions (consumer PRD §15)

| # | Decision | Ruling |
| --- | --- | --- |
| 1 | Tax treatment enum | SC-1 |
| 2 | Canonical-account shape | SC-2 |
| 3 | Equity-fraction placement | SC-3 (read from the bulk snapshot) |
| 4 | Saved-plan adoption of a newer input | **Ask for confirmation.** Loading a saved plan fetches a current input and shows a visible, attributable prompt before replacing the plan's prior portfolio input; never silent |
| 5 | Last-valid input location | Gitignored `wealthplan/data/tracker_input.local.json`; same no-commit/no-log handling as saved plans; no extra encryption on the single-user machine |
| 6 | Parity tolerances | SC-7 |
| 7 | Rollback window | SC-7 (30 days) |
| 8 | HTTP client | Standard-library `urllib.request` with bounded timeouts if wealthplan has no HTTP dependency today; otherwise reuse the existing one. No new heavyweight dependency for four GET endpoints |
| 9 | Position table labels | Keep provider account labels (single-user localhost surface; matches current behavior) |
| 10 | Missing equity fraction | Permit the current **explicitly labeled fallback** allocation; blocked from being called live/current |

## 4. Earnings Summary decisions (consumer PRD §16)

| # | Decision | Ruling |
| --- | --- | --- |
| 1 | CIO chat history / monthly briefs | Chats **archive-only**; monthly briefs **import-provenance**, rendered under Portfolio → Record |
| 2 | Human-capital store | **`owner_profile_facts`** via the existing propose/affirm gate; no new table |
| 3 | `policy_weights` split | Rows parameterizing benchmark math stay in PT; rows expressing owner target posture migrate to Portfolio Posture / `positioning_intents` (exact row classification produced during M4 preview) |
| 4 | Journal reconciliation tolerance | Same ticker + same action (buy/sell/trim/add normalized) + trade date within **±3 calendar days** ⇒ reconcile to the existing `decisions` row; everything else imports as a new row; all reconciliations logged for review |
| 5 | Action-queue state | Reuse existing `alerts`/standup state machinery; **no dedicated queue table** initially; historical `action_queue` rows import as provenance artifacts only |
| 6 | Holdings-page research enrichment | Dropped from PT; replaced by a per-ticker link into the Earnings Summary workspace |
| 7 | Service discovery | SC-4 |
| 8 | Rollback windows | SC-7 |

---

## 5. Portfolio Tracker inventory and classification

Classification legend — **retain** (Portfolio Data Service keeps it), **expose**
(retain + publish via `/api/v1`), **migrate** (capability moves to Earnings Summary),
**deprecate** (superseded by v1; removed per deprecation policy after consumers cut
over), **delete** (removed at Phase 5 with no replacement needed).

### 5.1 API routes

| Route module (prefix) | Classification |
| --- | --- |
| `plaid.py` (`/api/plaid`) | retain (operator; item metadata superseded by v1 accounts for consumers → deprecate consumer use) |
| `snaptrade.py` (`/api/snaptrade`) | retain (operator) |
| `portfolio.py` (`/api/portfolio`) | expose→deprecate: deterministic analytics (performance, position-alpha, positioning, beta, drawdown, exit-quality, after-tax, transactions, holdings) re-served under `/api/v1`; legacy paths get deprecation metadata in Slice 2; ES-bridge enrichment fields delete at Phase 5 |
| `positions_v1.py` (`/api/v1/portfolio/positions`) | expose (folded into the v1 contract; enum upgraded per SC-1) |
| `overrides.py` (`/api/overrides`) | retain (operator corrections) |
| `policy.py` (`/api/policy`) | split per PT-6 |
| `decision_support.py` (`/api/decisions`, `/api/trade-tags`, `/api/earnings/upcoming`) | migrate (journal + tags to ES Phase 3; earnings/upcoming delete — ES owns earnings data) |
| `earnings_summary.py` (`/api/earnings-summary`) | delete (the bridge; Phase 5) |
| `coaching.py` (`/api/coaching`) | migrate |
| `cockpit.py` (`/api/cockpit`) | migrate |
| `human_capital.py` (`/api/human-capital`) | migrate |
| `cio_advisor.py` (`/api/cio-advisor`) | migrate |

### 5.2 Services

retain: `performance`, `position_alpha`, `positioning`, `positions_v1`, `beta`,
`brinson`, `drawdown`, `after_tax`, `exit_quality`, `data_quality`, `splits`,
`active_items`, `policy` (calc-config portion).
migrate: `cockpit`, `cio_advisor`, `coaching`, `trade_analysis`, `trade_timeline`
(presentation/judgment; their deterministic inputs stay in the retained services).
delete: `earnings_summary` (bridge).

### 5.3 Jobs

retain: `daily_refresh`, `snapshot`, `backfill`, `prices`, `benchmarks`, `splits`,
`daily_values`, `backup`, `classify_securities`, `dedupe_securities`, `scrub`,
`migrate_broker_to_snaptrade`.
deprecate→delete: `earnings_calendar` (ES owns earnings data), `email_brief`
(briefing is an ES capability per provider-PRD non-goals; stops at Phase 4).

### 5.4 Tables

retain: `items`, `accounts`, `securities`, `holdings_snapshots`,
`investment_transactions`, `prices`, `stock_splits`, `benchmarks`,
`portfolio_values_daily`, `snaptrade_users`, `cost_basis_overrides`,
`transaction_overrides`, `ticker_overrides`, `security_classifications`,
`tax_form_imports`, `tax_form_realized_lots`, `tax_form_dividends`,
`tax_form_interest`, `tax_form_retirement_distributions`.
migrate-active: `trade_decisions`, `trade_tags`, `human_capital_overlap`,
`policy_weights` (owner-intent rows only).
import-provenance: `action_queue`, `monthly_briefs`, `monthly_brief_jobs`.
archive-only: `chat_sessions`, `chat_turns`, `earnings_calendar`.

### 5.5 Frontend pages

retain (reduced ops console): `Accounts` (connections/coverage), `Transactions`
(operational source inspection), plus new/retained sync, data-quality, corrections,
DB-ops, and API panels; `Holdings` reduced to operational source inspection.
migrate→remove: `Dashboard`, `Contributions`, `Cockpit`, `ThesisHealth`, `Advisor`,
`Scorecard`, `Review`, `TradeAnalysis`, `TradeTimeline`, `Earnings`, and all
user-facing analytics presentation.

### 5.6 Direct cross-repo reads (to be eliminated)

- PT → ES: `services/earnings_summary.py` (+ `config.earnings_summary_db_path`,
  `earnings_summary_output_dir`) — delete at Phase 5 after ES surface parity.
- ES → PT: none direct (REST client only) — client upgraded to typed v1 in M3.
- WP → PT: `src/wealthplan/tracker.py` SQLite read + `PORTFOLIO_TRACKER_DB` config —
  replaced in M2.

---

## 6. Vocabulary actions

- `portfolio-tracker/DEFINITIONS.md`: add **Portfolio Data Service** (done with this
  addendum).
- `wealthplan`: add **Portfolio input snapshot** before M2 implementation if the term
  is used in code (adapter result naming).
- `earnings-summary/DEFINITIONS.md`: no new term required; **Legacy provenance
  artifact** to be added only if implementation types it.
