# Portfolio Tracker Data-Service Mode — Product Requirements Document

**Status:** Proposed for owner approval on 2026-07-23.  
**Audience:** Repository owner and implementation agents in `portfolio-tracker`,
`earnings-summary`, and `wealthplan`.  
**Scope:** Product and migration requirements for turning Portfolio Tracker into the
authoritative localhost service for linked-account data and deterministic portfolio
facts.  
**Companion documents:**

- `earnings-summary/docs/design/portfolio_intelligence_consolidation_prd.md`
- `wealthplan/docs/design/portfolio_tracker_api_migration_prd.md`

**Document authority:** This PRD does not authorize a live database migration,
backfill, account relink, destructive correction, directive edit, or removal. Each
state-changing migration requires the repository's normal preview, backup,
verification, and approval controls.

---

## 1. Executive summary

Portfolio Tracker will become a backend-first, single-user localhost service whose
primary responsibility is to maintain a correct, reconciled, provenance-rich record
of:

- linked financial accounts;
- normalized accounts and Securities;
- holdings and daily snapshots;
- transactions and cash flows;
- prices, benchmarks, and corporate actions required for deterministic calculations;
- source-data corrections and classifications;
- synchronization health, coverage, freshness, and data-quality findings; and
- deterministic portfolio calculations that must remain coupled to those records.

The product will retain a small operational console for account linking, synchronization,
correction, data-quality review, backup, and restore. It will stop being the owner's
investment-analysis and decision workspace.

Earnings Summary will become the user-facing investing system. It will own valuation,
Concentration and Risk Budget interpretation, performance and benchmark presentation,
journaling, process-versus-outcome learning, decision support, research, and coaching.
It will consume Portfolio Tracker only through a stable, versioned HTTP API.

Wealthplan will remain the user-facing household scenario and life-decision simulator.
It will consume current investable assets, Tax treatment, account coverage, and
Positioning facts through the same HTTP API. It will continue to own manual household
facts, life events, scenarios, projection assumptions, and simulation methodology.

The architectural boundary is:

> Portfolio Tracker owns observed financial facts and deterministic derivations.
> Earnings Summary owns interpretation, recommendations, decisions, and the primary
> investing user experience. Wealthplan owns household scenarios and projections.

Portfolio Tracker must not read Earnings Summary's database after the migration.
Earnings Summary and Wealthplan must not read Portfolio Tracker's database after the
migration.

---

## 2. Proposed vocabulary

Before implementation code adopts the following term, add it to `DEFINITIONS.md`:

### Portfolio Data Service

The backend-first operating mode of Portfolio Tracker. It owns linked-account ingestion,
normalized portfolio records, source-data corrections, provenance, freshness, and
deterministic portfolio calculations exposed through versioned APIs. It does not own
research, valuation, portfolio judgment, recommendations, or the Owner Decision journal.

This PRD uses **Portfolio Data Service** as a proposed product name. Existing canonical
terms such as Positioning, Security, Classification, Concentration, and Correlation/beta
retain their definitions in `DEFINITIONS.md`.

---

## 3. Product intent

### 3.1 Product promise

Any authorized local consumer can obtain a current or historical portfolio state and
understand:

- what data was observed;
- which provider supplied it;
- which accounts were covered;
- when each source last succeeded;
- whether the result is stale or partial;
- which corrections or fallbacks were applied;
- which deterministic methodology produced a derived result; and
- whether the result is safe to use for decision support.

### 3.2 Primary consumers

1. **Earnings Summary**, the primary user-facing investing system.
2. **Wealthplan**, the household scenario and life-decision simulator.
3. Local scheduled jobs and maintenance tools.
4. Future localhost projects that need portfolio facts without database access.
5. The retained Portfolio Tracker operational console.

### 3.3 Product posture

- Single-user and localhost by default.
- Pull-only with respect to brokerages; no trade execution or order staging.
- API-first for cross-project use.
- Deterministic for financial calculations.
- Explicit about stale, missing, conflicting, or partial source data.
- Backward-compatible within a published major API version.

---

## 4. Goals and non-goals

### 4.1 Goals

#### G1 — Authoritative linked-account record

Maintain linked items, accounts, Securities, holdings, transactions, and daily snapshots
without requiring another project to understand Plaid or SnapTrade identifiers.

#### G2 — One supported cross-project boundary

Replace direct SQLite reads and ad hoc endpoint composition with a documented `/api/v1`
contract.

#### G3 — Pristine API documentation

Ship a complete OpenAPI contract, consumer guide, examples, calculation methodology,
freshness semantics, error catalogue, compatibility policy, and contract fixtures.

#### G4 — Correct deterministic facts

Keep portfolio calculations that depend on transaction reconciliation, cash-flow
classification, corporate actions, and snapshot precedence next to their source data.

#### G5 — Honest degradation

Never silently substitute a last-good holding, balance, price, or account result for a
failed refresh. Last-valid data may be returned only with explicit age, coverage, and
failure metadata.

#### G6 — Narrow operations experience

Retain only the UI required to connect accounts, run and inspect syncs, repair source
data, and operate the database safely.

#### G7 — Reversible migration

Move user-facing functionality to Earnings Summary through additive contracts,
dual-running verification, and explicit cutover gates before deleting old paths.

### 4.2 Non-goals

Portfolio Tracker will not own:

- DCF or other fundamental valuation;
- thesis status, research briefs, or earnings-research alerts;
- the decision queue or CIO coaching;
- the Owner Decision journal;
- process-quality or outcome-quality assessment;
- portfolio recommendations or capital-allocation judgment;
- human-capital portfolio interpretation;
- a general-purpose LLM interface;
- email briefing;
- multi-user tenancy or public hosting; or
- brokerage trade execution.

The migration will not:

- move Plaid or SnapTrade credentials into Earnings Summary;
- give Earnings Summary direct write access to Portfolio Tracker's database;
- duplicate deterministic return calculations in Earnings Summary;
- remove a legacy endpoint before its consumer has passed contract and parity tests; or
- operate destructively on the live database without a verified backup and approved
  preview.

---

## 5. Ownership boundary

### 5.1 Portfolio Tracker retains

| Capability | Required ownership |
| --- | --- |
| Aggregator connections | Plaid/SnapTrade configuration, encrypted tokens, link/relink/unlink, provider identifiers |
| Account record | Items, accounts, active/inactive state, source coverage, canonical cross-provider identity, inclusion state, and Tax treatment detailed enough to distinguish taxable, pretax, Roth, HSA, and unknown |
| Security master | Provider identifiers, ticker/CUSIP/ISIN mapping, cash-equivalent status, deduplication |
| Holdings | Current holdings, account-level lots, consolidated positions, daily snapshots |
| Transactions | Normalized investment transactions, cash flows, transfers, fees, dividends |
| Source corrections | Cost-basis, ticker, transaction, and Classification overrides |
| Supporting market data | Security prices, benchmark marks, splits, and other inputs required by retained deterministic calculations |
| Tax-source augmentation | Imported source records used to repair cost basis or transaction history |
| Data operations | Sync jobs, daily refresh, backfill tools, data-quality checks, backup and restore |
| Deterministic calculations | Performance, benchmark counterfactuals, Positioning facts, Concentration measures, Correlation/beta, drawdown, attribution, after-tax and exit-quality calculations |
| Service contract | Versioned API, generated OpenAPI, compatibility fixtures for Earnings Summary and Wealthplan, health and freshness metadata |

### 5.2 Earnings Summary receives

| Capability | Portfolio Tracker disposition |
| --- | --- |
| DCF and valuation | Remove companion-database reader and all valuation display |
| Thesis health and alerts | Remove bridge and UI |
| Cockpit/action queue | Migrate required history, then remove service, routes, tables, and UI |
| Trade/decision journal | Export and reconcile historical records, then remove mutation and presentation ownership |
| Process and outcome learning | Move all evaluation and presentation |
| CIO advisor and monthly brief | Remove after Earnings Summary parity |
| Coaching | Remove after Earnings Summary parity |
| Human-capital interpretation | Move to Earnings Summary |
| Portfolio analytics UI | Move presentation to Earnings Summary; retain calculation APIs |
| Policy/Portfolio Posture | Strategic interpretation moves; benchmark configuration needed for calculation remains an API-owned setting |

### 5.3 Wealthplan consumes without absorbing

| Capability | Ownership after migration |
| --- | --- |
| Current investable account values | Portfolio Tracker observes and serves; Wealthplan consumes as starting inputs |
| Cross-provider account deduplication | Portfolio Tracker owns canonical identity and inclusion state |
| Tax treatment | Portfolio Tracker serves explicit detailed treatment; Wealthplan maps it to its canonical tax bucket vocabulary |
| Equity fraction | Portfolio Tracker serves a versioned deterministic Positioning fact; Wealthplan uses it as a projection input |
| Cash, illiquid assets, and home equity | Wealthplan remains the manual authority |
| Household members and ownership split | Wealthplan remains the authority; the v1 integration remains household-aggregate unless Phase 0 approves a new requirement |
| Scenarios, life events, FI, survival rate, and depletion | Wealthplan remains the authority |
| Projection and tax methodology | Wealthplan remains the authority |

Wealthplan is a read-only consumer. It receives no account-linking, correction, sync,
or other mutation authority.

### 5.4 Important distinction

Moving a surface does not automatically move its calculation.

For example:

- Earnings Summary owns the Risk Budget experience.
- Portfolio Tracker owns current weights, Concentration, Correlation/beta, drawdown,
  benchmark-relative returns, and source-quality metadata.
- Earnings Summary combines those facts with DCF, thesis, owner context, and judgment.

---

## 6. Target product surface

### 6.1 Retained operational console

The Portfolio Tracker frontend is reduced to:

1. **Connections**
   - provider status;
   - link/relink/unlink;
   - data-active state;
   - last successful refresh;
   - current provider errors.
2. **Sync operations**
   - trigger a safe sync;
   - view run status and per-leg outcome;
   - see affected account coverage without exposing balances in logs.
3. **Data quality**
   - unresolved identifier conflicts;
   - stale or partial accounts;
   - missing cost basis;
   - missing price/benchmark coverage;
   - transaction reconciliation findings.
4. **Corrections**
   - ticker, cost-basis, transaction, and Classification overrides;
   - preview and audit information.
5. **Database operations**
   - migration status;
   - backup status;
   - restore verification status;
   - local health.
6. **API**
   - service version;
   - OpenAPI/documentation links;
   - consumer health and recent contract errors.

### 6.2 Removed or redirected surfaces

After cutover, the following Portfolio Tracker routes either redirect to Earnings
Summary or are removed:

- Dashboard;
- Holdings analysis beyond operational source inspection;
- Cockpit;
- Thesis health;
- Earnings research;
- trade analysis and trade timeline presentation;
- policy editor as an investment-planning surface;
- decision journal and tags;
- human-capital interpretation;
- CIO advisor;
- coaching;
- monthly briefs; and
- user-facing Positioning, performance, risk, and benchmark dashboards.

Raw and deterministic facts supporting these surfaces remain available by API where
required.

---

## 7. API product requirements

### 7.1 API structure

Provide one versioned service with focused resources plus one bulk read model. Do not
replace the service with one unstructured payload.

Required endpoint families:

| Endpoint family | Purpose |
| --- | --- |
| `GET /api/v1/health` | Process, database, migration, provider, and contract health |
| `GET /api/v1/accounts` | Normalized accounts, canonical cross-provider identity, inclusion state, detailed Tax treatment, provider provenance, active state, freshness |
| `GET /api/v1/positions` | Latest or as-of Positioning inputs and account-level lots |
| `GET /api/v1/position-snapshots` | Historical observed holdings with explicit date bounds |
| `GET /api/v1/transactions` | Cursor-paginated normalized transactions and corrections |
| `GET /api/v1/securities` | Security identity and Classification metadata |
| `GET /api/v1/cash-flows` | Deterministically classified external and internal cash flows |
| `GET /api/v1/sync-runs` | Run-level and leg-level ingestion status |
| `POST /api/v1/sync-runs` | Idempotent operator-triggered refresh |
| `GET /api/v1/data-quality` | Machine-readable issues, severity, affected scope, and recovery |
| `GET /api/v1/analytics/performance` | Modified-Dietz and benchmark-relative series |
| `GET /api/v1/analytics/position-performance` | Position-level return/alpha facts |
| `GET /api/v1/analytics/risk` | Correlation/beta, volatility, drawdown, and calculation inputs |
| `GET /api/v1/analytics/positioning` | Asset type, Sector, Region, detailed Tax treatment, equity fraction, cash-equivalent treatment, and Concentration facts |
| `GET /api/v1/portfolio-snapshot` | Consumer-optimized aggregate of current positions, account values, canonical account relationships, Tax treatment, Positioning summary, account coverage, freshness, warnings, and stable links to detailed resources |

Mutation endpoints for link/relink/unlink and source corrections remain operator-only
and must be documented separately from consumer read APIs.

### 7.2 Canonical account and allocation contract

The API, not each consumer, owns reconciliation of duplicate Plaid and SnapTrade
representations. Account resources must expose:

- a stable Portfolio Tracker account identifier;
- provider and provider-account identifiers without exposing credentials;
- canonical account identity and any source-account members;
- whether the account is included in portfolio totals;
- an explicit exclusion or deduplication reason;
- institution, account type, subtype, active state, and source dates;
- detailed Tax treatment that preserves taxable, pretax, Roth, HSA, and unknown as
  distinct states; and
- the evidence source and confidence for assigned Tax treatment.

Consumers must not infer Tax treatment from account display names once this contract is
available. They also must not recreate duplicate detection from approximate balances.

Positioning output must expose either a directly usable `equity_fraction` or sufficient
typed allocation facts to reproduce it exactly. In either case it must include:

- numerator and denominator scope;
- the cash-equivalent Classification policy;
- included and excluded accounts;
- price and holdings dates;
- partial and stale state;
- methodology name and version; and
- explicit behavior when allocation cannot be calculated.

The bulk snapshot must carry the account and allocation fields needed by both Earnings
Summary and Wealthplan. A consumer-specific endpoint or a second Wealthplan-only
portfolio API is not permitted unless the shared contract proves unable to represent a
general portfolio fact.

### 7.3 Response envelope

Every decision-support response must expose:

- `schema_version`;
- `generated_at`;
- `as_of`;
- `currency` for every currency-bearing aggregate;
- `source_providers`;
- `account_coverage`;
- `last_successful_sync_at`;
- `is_partial`;
- `is_stale`;
- `warnings`;
- `methodology`;
- `methodology_version`; and
- `links` to relevant detailed resources.

The response must distinguish:

- no data;
- not applicable;
- stale last-valid data;
- partial current data;
- provider failure;
- calculation failure; and
- unsupported historical coverage.

### 7.4 Numeric and date conventions

- Money and high-precision quantities remain decimal strings.
- Percent fields include an explicit unit contract: fraction or percent, never implied.
- Dates are ISO `YYYY-MM-DD`.
- Timestamps are ISO-8601 with timezone.
- Every time series states whether the date is observation date, transaction date,
  valuation date, or calculation date.
- Benchmark results name the benchmark and total-return/price-return basis.

### 7.5 Pagination and filtering

- Current-position endpoints may return the complete single-user book.
- Transactions, snapshots, sync runs, and data-quality history use cursor pagination.
- Historical endpoints require bounded date parameters or documented defaults.
- Consumers must never depend on a hard-coded 5,000-row cap.

### 7.6 Errors

Errors use a consistent structured shape with:

- stable machine-readable code;
- human-readable summary;
- request/trace identifier;
- affected resource;
- retryability;
- recovery guidance; and
- field-level details where applicable.

Provider exception bodies, credentials, tokens, holdings, balances, and raw personal
data must not appear in errors or logs.

### 7.7 Idempotency

Every state-changing API accepts or derives an idempotency key.

At minimum:

- sync: `{provider_scope}_{requested_at_or_business_date}_{mode}`;
- correction: `{correction_type}_{natural_target}_{normalized_payload_sha}`;
- link-token exchange: provider-supported natural identifier plus request identifier.

### 7.8 Local security

- Bind to loopback by default.
- Preserve encrypted aggregator tokens.
- Support a local bearer token for cross-process consumers before any non-loopback use.
- Separate consumer-read endpoints from operator mutations.
- Do not rely on CORS as authorization.

---

## 8. API documentation requirements

The API is not complete until all of the following ship:

1. Generated OpenAPI checked for drift in CI.
2. A human-authored API overview.
3. Resource-by-resource request/response examples.
4. A bulk-snapshot consumer tutorial.
5. Freshness, partial-data, and last-valid semantics.
6. Calculation methodology for every analytics endpoint.
7. Units and rounding reference.
8. Error catalogue and retry guidance.
9. Idempotency guide for operator mutations.
10. Compatibility and deprecation policy.
11. Changelog by API version.
12. Sanitized example fixtures.
13. A consumer contract-test package or fixture suite used by Earnings Summary and
    Wealthplan.
14. Local startup, health-check, and troubleshooting instructions.

OpenAPI generation and examples must never inspect or serialize the live database.

---

## 9. Data and calculation requirements

### 9.1 Source precedence

Document and test:

- provider identifier reconciliation;
- current holdings source precedence;
- forward snapshot versus transaction walk-back precedence;
- manual versus inferred cost basis;
- manual versus automatic Classification;
- price-source fallback;
- benchmark-source fallback;
- corporate-action normalization; and
- account-active filtering.

### 9.2 Deterministic methodology retained

Portfolio Tracker remains authoritative for:

- Modified-Dietz returns;
- benchmark cash-flow matching;
- position-level P&L and alpha;
- drawdown and recovery;
- Correlation/beta;
- volatility and Sharpe inputs;
- Positioning and Concentration measures;
- Brinson-style attribution where currently supported;
- cash-flow audit;
- after-tax comparison primitives; and
- exit-quality facts.

Earnings Summary may compose or explain these outputs but must not independently
recalculate them as competing facts.

### 9.3 Freshness

Freshness is field-aware:

- account holdings freshness;
- transaction freshness;
- price freshness;
- benchmark freshness;
- calculation freshness; and
- source coverage.

A current calculation over stale inputs is still stale and must say why.

---

## 10. State migration

### 10.1 Records moving to Earnings Summary

The migration inventory must include:

- trade decisions;
- trade tags;
- action-queue history required for provenance;
- accepted/dismissed/snoozed/execution state where meaningful;
- CIO sessions and monthly brief metadata if retained;
- human-capital interpretation records;
- policy/Portfolio Posture records that represent owner intent rather than calculation
  configuration; and
- process/outcome assessments.

Not every legacy row must become an active Earnings Summary object. Some may be imported
as immutable legacy provenance.

### 10.2 Migration contract

Every moved table requires:

- source table and natural key;
- destination table and natural key;
- field mapping;
- owner/source attribution;
- timestamp semantics;
- null/default policy;
- source-system and source-row identifier;
- idempotency key;
- validation queries;
- rejection/quarantine policy; and
- rollback procedure.

### 10.3 Live-data safety

Before each migration or destructive cleanup:

1. Produce a verified backup.
2. Prove the backup opens and passes integrity checks without printing holdings.
3. Preview affected tables, date ranges, and row counts.
4. Obtain explicit owner approval.
5. Run an idempotent import.
6. Compare source and destination counts and key invariants.
7. Keep the source tables read-only through the agreed rollback window.

---

## 11. Cross-repository dependency contract

| Dependency | Provider | Consumer | Blocking deliverable |
| --- | --- | --- | --- |
| Current positions | Portfolio Tracker | Earnings Summary | `/api/v1/positions` contract and fixture |
| Bulk current state | Portfolio Tracker | Earnings Summary | `/api/v1/portfolio-snapshot` |
| Transactions/cash flows | Portfolio Tracker | Earnings Summary | Cursor pagination, correction metadata, coverage |
| Performance/benchmark | Portfolio Tracker | Earnings Summary | Versioned methodology and parity tests |
| Risk/Positioning facts | Portfolio Tracker | Earnings Summary | Units, source dates, partial-data semantics |
| Sync health | Portfolio Tracker | Earnings Summary | Health and sync-run APIs |
| Starting investable assets | Portfolio Tracker | Wealthplan | Bulk snapshot with per-account values in today's dollars and explicit coverage |
| Tax bucket inputs | Portfolio Tracker | Wealthplan | Detailed Tax treatment preserving taxable, pretax, Roth, HSA, and unknown |
| Canonical accounts | Portfolio Tracker | Wealthplan | Cross-provider identity, inclusion state, and deduplication reason |
| Equity fraction | Portfolio Tracker | Wealthplan | Versioned Positioning methodology, cash-equivalent policy, dates, and warnings |
| Decision-history import | Both | Earnings Summary | Approved mapping, backup, preview, idempotent importer |
| Projection input provenance | Wealthplan | Wealthplan | Persisted API/schema/methodology versions, as-of dates, coverage, warnings, and input hash |
| Consumer compatibility | Portfolio Tracker | Earnings Summary and Wealthplan | OpenAPI artifact and sanitized fixtures |
| Legacy route removal | Earnings Summary | Portfolio Tracker | Production-like parity and owner cutover approval |

Portfolio Tracker work may proceed additively without either consumer. No removal or
data-ownership cutover may proceed until every affected consumer's acceptance gate
passes.

---

## 12. Delivery sequence

The phase identifiers below are shared with the companion Earnings Summary and
Wealthplan PRDs.

### Phase 0 — Ratify boundary and inventory

Portfolio Tracker deliverables:

- approve the ownership matrix;
- add the proposed Portfolio Data Service term to `DEFINITIONS.md`;
- inventory every route, table, job, page, scheduled task, and consumer;
- classify each as retain, expose, migrate, deprecate, or delete;
- record current API payload fixtures without live personal values;
- identify every Earnings Summary and Wealthplan direct database read; and
- ratify the canonical account, detailed Tax treatment, and equity-fraction contract
  required by both consumers.

Exit gate:

- all three PRDs use the same ownership and phase map;
- no disputed capability remains unclassified.

### Phase 1 — Add the v1 contract

Portfolio Tracker deliverables:

- add shared response metadata;
- add health, accounts, transactions, snapshots, sync-runs, data-quality, analytics,
  and bulk-snapshot v1 endpoints;
- preserve existing endpoints;
- publish generated OpenAPI and sanitized fixtures;
- add contract and methodology tests; and
- instrument latency and failure codes without logging portfolio data.

Consumer dependencies:

- none for additive implementation;
- Earnings Summary and Wealthplan must each review the fields and sanitized fixtures
  they depend on before Phase 1 exits.

Exit gate:

- Earnings Summary and Wealthplan can each implement a client using only documentation
  and fixtures;
- all new APIs are covered by schema and contract tests.

### Phase 2 — Consumer adoption and direct-DB removal

Portfolio Tracker deliverables:

- support the Earnings Summary and Wealthplan clients during dual-read parity;
- close contract gaps additively;
- add compatibility tests for the generated client;
- add deprecation metadata to superseded non-v1 consumer endpoints.

Earnings Summary dependencies:

- consume v1 for all live portfolio facts;
- replace direct reads of `portfolio.db`;
- prove equivalent current Positioning, performance, and journal-reconciliation inputs.

Wealthplan dependencies:

- consume v1 for live investable account values, detailed Tax treatment, canonical
  account inclusion, and equity fraction;
- compare aggregate outputs against the existing SQLite adapter without logging
  balances or account details;
- replace `PORTFOLIO_TRACKER_DB` and all production SQLite reads with a configurable
  Portfolio Tracker API URL.

Exit gate:

- repository-wide scans find no supported Earnings Summary or Wealthplan direct read of
  Portfolio Tracker's SQLite database;
- API and previous-path parity is within documented tolerances for both consumers.

### Phase 3 — Journal and owner-state migration

Portfolio Tracker deliverables:

- implement read-only export or migration fixtures;
- produce approved previews and backups;
- freeze legacy decision mutations at cutover;
- keep legacy rows queryable through the rollback window.

Earnings Summary dependencies:

- destination schema and importer;
- duplicate and conflict handling;
- owner-first journal parity;
- process/outcome evaluation parity.

Wealthplan dependencies:

- persist the immutable Portfolio Tracker input snapshot metadata used by each saved
  baseline or evaluated scenario;
- distinguish a current API read, an explicitly aged last-valid snapshot, and manual
  fallback inputs;
- leave manual household facts and projection methodology unchanged.

Exit gate:

- counts, natural keys, timestamps, and sampled histories reconcile;
- new journal writes occur only in Earnings Summary;
- saved Wealthplan results identify the starting-position source and vintage;
- rollback has been rehearsed.

### Phase 4 — User-experience cutover

Portfolio Tracker deliverables:

- ship the reduced operational console;
- replace migrated navigation with redirects or explicit links;
- stop scheduled CIO/coaching/Cockpit work;
- retain calculation and ingestion jobs.

Earnings Summary dependencies:

- portfolio, performance, risk, Concentration, journaling, and decision workflows pass
  production-like acceptance tests;
- stale/offline behavior is explicit;
- the owner approves Earnings Summary as the default investing interface.

Wealthplan dependencies:

- the Position surface shows source date, coverage, stale/partial warnings, and
  allocation methodology;
- reload behavior cannot label an aged or partial snapshot as live;
- scenario and projection tests pass with API-backed and explicit last-valid inputs.

Exit gate:

- no required investing workflow depends on a Portfolio Tracker analysis page; and
- no required Wealthplan workflow depends on direct filesystem access to Portfolio
  Tracker.

### Phase 5 — Legacy retirement

Portfolio Tracker deliverables:

- remove Earnings Summary database bridge;
- remove Cockpit, coaching, CIO advisor, human-capital interpretation, migrated journal,
  and obsolete frontend code;
- retire superseded endpoints according to the deprecation policy;
- remove direct-database compatibility support only after both consumers cut over;
- preserve migrations or archive export needed to read historical data;
- update scheduled tasks and runbooks.

Exit gate:

- all removals have a tested replacement;
- rollback window has expired with owner approval;
- backup and restore verification passes.

### Phase 6 — Service hardening

- enforce OpenAPI drift checks;
- publish API changelog;
- add consumer compatibility CI;
- run the Earnings Summary and Wealthplan fixture suites against the same API artifact;
- validate startup and health behavior;
- validate backup/restore;
- measure local latency and payload sizes;
- remove undocumented cross-repo filesystem assumptions.

---

## 13. Acceptance criteria

### 13.1 API

- A new consumer can integrate from the docs and fixtures without reading source code.
- Every response carries sufficient freshness and coverage metadata.
- Transactions and historical snapshots are not truncated by undocumented caps.
- Errors are stable, sanitized, and machine-readable.
- OpenAPI drift fails CI.
- Earnings Summary and Wealthplan contract tests run against the same Portfolio Tracker
  fixtures.

### 13.2 Data correctness

- No failed refresh is represented as current success.
- Provider identifiers reconcile before deterministic calculations run.
- Numeric units, currency, and dates are explicit.
- Calculation versions are exposed and tested.
- Backup restore is verified without exposing holdings in logs.

### 13.3 Product boundary

- Portfolio Tracker contains no active DCF, thesis, research-alert, coaching, or
  recommendation reader.
- Portfolio Tracker no longer owns the Owner Decision journal.
- The retained UI is operational rather than analytical.
- Earnings Summary obtains portfolio facts only through supported APIs.
- Wealthplan obtains live portfolio facts only through supported APIs and continues to
  own its household and scenario inputs.

### 13.4 Migration safety

- Every destructive step has an approved preview.
- Every moved dataset has idempotent import and rollback.
- Legacy rows remain readable during the rollback window.
- No unrelated user files, backups, credentials, or database artifacts are committed.

---

## 14. Testing and verification

### 14.1 Portfolio Tracker tests

- provider normalization and identifier reconciliation;
- response-envelope metadata;
- cursor pagination;
- stale/partial/failure matrices;
- decimal and percent-unit contracts;
- deterministic calculation regression fixtures;
- OpenAPI snapshot/drift;
- idempotent sync trigger;
- error redaction;
- loopback and mutation authorization behavior;
- migration export fixtures; and
- legacy-route deprecation headers.

### 14.2 Cross-repository tests

- sanitized Portfolio Tracker fixtures deserialize in Earnings Summary and Wealthplan;
- both consumer clients reject an incompatible major version;
- field additions remain backward-compatible;
- missing optional analytics degrade explicitly;
- a partial account refresh cannot produce a decision-grade portfolio snapshot;
- direct-database parity is proven in both consumers before the old paths are removed;
- Wealthplan fixtures preserve taxable, pretax, Roth, HSA, and unknown distinctly;
- canonical account fixtures prevent a consumer from double-counting cross-provider
  duplicates; and
- equity-fraction fixtures state cash-equivalent policy and methodology version.

### 14.3 Operational verification

- scheduled refresh still completes;
- API starts independently of the frontend;
- the retained console can operate accounts and corrections;
- Earnings Summary and Wealthplan can start or probe the service without assuming port
  `8000`;
- logs contain run IDs and statuses but no secrets, holdings, or balances;
- backups remain restorable after schema cleanup.

---

## 15. Failure modes

| Failure | Required behavior |
| --- | --- |
| One provider fails | Return explicit partial coverage; do not present the book as complete |
| Prices are stale | Mark affected calculations stale and name the price date/provider |
| Benchmark pull fails | Preserve last-valid series with age; do not call it current |
| Earnings Summary is offline | Portfolio ingestion and API continue normally |
| Portfolio Tracker is offline | Earnings Summary uses an explicitly aged last-valid snapshot or blocks decision-grade output |
| API schema mismatch | Consumer fails closed with a compatibility error |
| Migration row conflict | Quarantine and report counts; do not overwrite silently |
| Backup verification fails | Block schema migration, data move, and deletion |
| Legacy consumer remains | Block endpoint removal |
| Calculation methodology changes | Increment version and prevent false historical comparison |

---

## 16. Success measures

| Outcome | Measure |
| --- | --- |
| Supported boundary | Zero approved cross-project SQLite reads after Phase 2 |
| Documentation quality | Consumer implementation succeeds using docs and fixtures only |
| Data honesty | Every partial/stale response is machine- and human-identifiable |
| Reliability | Scheduled ingestion and API health are independently observable |
| Reuse | Earnings Summary uses one typed client and one bulk snapshot for common builds |
| Simplification | Portfolio Tracker's primary navigation contains only operational surfaces |
| No duplication | No research/valuation/decision business logic remains in Portfolio Tracker |
| Reversibility | Each cutover phase has a tested rollback until its gate expires |

Anti-metrics:

- number of endpoints;
- size of the bulk payload;
- percentage of legacy code deleted before parity;
- hiding failures to keep dashboards populated; or
- moving deterministic math merely to make repository ownership look cleaner.

---

## 17. Open implementation decisions

These decisions must be closed in Phase 0:

1. Final service name and `DEFINITIONS.md` wording.
2. Stable process discovery: fixed port, configurable URL, or local service registry.
3. Whether the OpenAPI artifact is checked in or generated and diffed in CI.
4. Local bearer-token requirement while loopback-only.
5. Exact rollback window for journal and UI cutover.
6. Whether strategic benchmark policy is fully moved or split between:
   - calculation configuration retained here; and
   - Portfolio Posture/owner intent moved to Earnings Summary.
7. Which historical Cockpit/CIO artifacts are imported as useful provenance versus
   archived only.
8. The exact detailed Tax treatment field and values that preserve Wealthplan's
   taxable, pretax, Roth, HSA, and unknown mapping without introducing a competing
   Portfolio Tracker domain term.
9. The canonical-account identity and exclusion model for overlapping Plaid and
   SnapTrade accounts.
10. Whether `equity_fraction` is materialized in the bulk snapshot or referenced from
    `/api/v1/analytics/positioning`.

---

## 18. Definition of done

Portfolio Tracker's transition is complete when:

1. Linked accounts, holdings, snapshots, transactions, Securities, corrections,
   provenance, and freshness remain correct and operable.
2. All supported cross-project reads, including Earnings Summary and Wealthplan, use
   documented `/api/v1` endpoints.
3. Deterministic portfolio calculations remain authoritative and versioned.
4. Earnings Summary owns the investing dashboard, valuation, risk interpretation,
   performance presentation, journal, process/outcome learning, and recommendations.
5. Portfolio Tracker's UI is a narrow operations console.
6. The API documentation and fixtures are sufficient for both current consumers and an
   independent future consumer.
7. Legacy state has been migrated or deliberately archived with verified counts.
8. Research, coaching, CIO, and decision code has been removed only after parity,
   cutover approval, and rollback expiry.
9. Backup and restore verification succeeds.
10. No credentials, live database files, backup files, holdings, or balances are
    committed or exposed in logs.
11. Wealthplan receives current investable assets, detailed Tax treatment, canonical
    account inclusion, and versioned equity fraction without direct database access or
    consumer-side name parsing and balance-based deduplication.
