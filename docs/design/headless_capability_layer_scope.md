# Headless Capability Layer — Scope

Status: owner-approved scope (2026-11-12). Decisions are recorded in section 6.

## 1. Goal

Make every Portfolio Tracker (PT) and Earnings Summary (ES) report, analysis, and data
process available without its current UI. A caller (script, agent, or a GUI made on request)
gets the same grounded facts, the same deterministic calculations, and the same safe write
path that the shipped screens use. A new UI then becomes a renderer only. It does not query
the database, calculate, or decide.

Smallest expected change: close the remaining gaps in the existing `/api/v1` contract, add a
typed operations contract for processes, add one read-only agent adapter, and split ES
"view data" from ES HTML rendering. Do not build a new platform.

## 2. Premise check — what already exists

PT is already most of the way there. The Portfolio Data Service program
(`portfolio_data_service_prd.md`, `phase0_decision_addendum.md`) built a headless read layer.

| Area | Current state | Headless? |
| --- | --- | --- |
| PT facts | 18 `/api/v1` GET resources, `meta` envelope (as-of, coverage, stale/partial, warnings, methodology version), checked-in OpenAPI + synthetic fixtures, drift-gated by pytest | Yes |
| PT analyses | performance, position-performance, risk, beta, drawdown, exit-quality, positioning are in v1 with fail-closed `calculation_status` and equation receipts | Yes |
| PT analyses not in v1 | `after-tax`, `brinson`, `holdings/by-account` exist only on legacy `/api/portfolio/*`. ES still calls legacy `after-tax`. | Partial |
| PT operator writes | `/api/overrides/*` (cost-basis, ticker, transaction, classification), `PUT /api/policy`, Plaid/SnapTrade link and sync. Unversioned, no idempotency key, no run record. | Partial |
| PT processes | 15 job CLIs (`daily_refresh`, `backfill`, `prices`, `benchmarks`, `backup`, `reconcile_cashflow_manifest`, `import_account_valuation_manifest`, …). Two already use preview → plan digest → `--commit --expected-plan-digest --backup`. No HTTP, no run log. `GET/POST /api/v1/sync-runs` is deferred until a live migration is approved. | No |
| ES views | 176 Flask routes. About 99 response sites return server-rendered HTML (`/api/peek/*`, `/api/panel/*`). Pydantic models exist in `src/report/models.py`, but most routes join data assembly and HTML in one call. | No |
| ES brief | `src/earnings_brief.py` persists pre-earnings briefs in `llm_artifacts` with an input hash. The peek serves it as HTML. | Data exists, not exposed |
| Agent access | No MCP server or tool catalog in either repo. | No |

Conclusion: the main work is not "make data headless". It is (a) processes, (b) three
missing analyses, (c) an agent adapter, and (d) ES view data.

## 3. Target shape — four layers

```
 Facts         PT /api/v1 (portfolio)          ES fact resolver (company data)
   │           deterministic, versioned, meta envelope, fail-closed
 Operations    catalog → preview(plan, digest) → owner approval → commit → receipt
   │           one contract for sync, refresh, backfill, corrections, imports, backup
 View data     typed JSON "view documents": brief, earnings readout, holding panel,
   │           performance report. Each field cites its fact source + methodology.
 Renderers     shipped PT console · ES cockpit · email · ad-hoc GUI on request
               render only: format, layout, chart. No math, no DB, no new claims.
```

Agent adapter: one MCP server per repo, generated from the OpenAPI document. It exposes
facts and view data as read tools, and operations as **preview-only** tools. For live data it
runs on the canonical Windows host and is started over the approved key-only SSH terminal
route (stdio over SSH). It reads the tracker on Windows loopback. No new listener or proxy.

## 4. Work packages

### WP1 — Close PT read gaps (S, no migration)

- `GET /api/v1/analytics/benchmark-estimate` (methodology `benchmark_comparison.estimate`
  v1): flow-adjusted portfolio return vs any stored benchmark (default VOO, QQQ) over
  6M/1Y/2Y lookbacks, with a scenario band next to the canonical calculation status.
  Taken over from the "Portfolio performance vs VOO/QQQ" session. VOO needs a live
  `benchmarks` backfill run before it resolves.

- Add `GET /api/v1/analytics/after-tax`, `GET /api/v1/analytics/attribution` (Brinson), and
  account-level lots on `/api/v1/portfolio/positions` (or a `by_account` view) under the
  existing envelope, with `methodology` + `methodology_version`.
- Add `GET /api/v1/settings/benchmark-policy` (calculation-config read only, per PT-6 split).
- Add legacy → successor entries to `DEPRECATED_PATHS`. Regenerate OpenAPI and fixtures.
- Move ES `portfolio_tracker_client` from legacy `after-tax` to v1.

### WP2 — Operations contract (M; run log needs a live migration)

- `GET /api/v1/operations` — catalog: name, effect class (`read`, `local_write`,
  `external_call`, `destructive`), required inputs, preview support, backup requirement.
- `POST /api/v1/operations/{name}/preview` — returns affected accounts, date range, row
  counts, and a plan digest. No write.
- `POST /api/v1/operations/{name}/runs` — requires the exact plan digest, an idempotency
  key (PRD §7.7), and a verified backup for state-changing runs. Returns a run ID.
- `GET /api/v1/operations/runs/{id}` — status, per-leg outcome, receipt. No balances.
- Implement by wrapping the existing job functions. Start with the two jobs that already
  have preview/digest. Keep `provider_transaction_corrections` without an HTTP commit path,
  as the v1 overview requires.
- The run-log table is the deferred `sync-runs` migration. It needs backup, preview, and
  owner approval before it runs on the live database.

### WP3 — Agent adapter (S–M)

- A small MCP server (stdio) in PT that maps each v1 GET to a tool, using the OpenAPI
  schemas. Live use: the Mac client starts it on Windows through the SSH terminal route;
  the server calls `127.0.0.1:8000` there. Resources: `v1-overview.md`, `DEFINITIONS.md`, methodology notes.
- Operations tools return previews only. Commit stays a human action in the console or CLI.
- Tool output keeps the full `meta` block so an agent cannot drop staleness or warnings.
- Same pattern later in ES over its view-data endpoints.

### WP4 — ES view data (L, incremental)

- For each HTML route, separate `build_<view>(…) -> ViewModel` from `render_<view>(model)`.
  Serve the model at `/api/views/<view>` (JSON). The HTML route calls the same builder.
- Order by value: pre-earnings brief and earnings readout → holding panel → overview →
  alerts → DCF evidence. Stop when the remaining routes have no ad-hoc demand.
- Each view model carries a source manifest: fact keys, observation versions, PT `meta`
  for portfolio facts, and claim-level citations for LLM text (ES invariant).
- Parity test per view: HTML from the model equals the current golden output.

### WP5 — Ad-hoc GUI recipe (S)

A short procedure plus one checker, not a framework:

- Input: one or more fact or view-data responses, saved with their request URLs.
- The renderer may format, sort, filter, and chart. It may not compute new financial values.
  A derived display value (for example a sum) must equal a value that the service returns.
- Show `as_of`, coverage, provider, and every warning next to the affected numbers.
  `calculation_status=unavailable` shows the reason code, never a blank or zero.
- Checker: every number in the rendered page traces to a response field. Fail if not.

## 5. Grounding and authority rules

1. Facts and calculations stay in PT and the ES fact resolver. Renderers and agents only read.
2. LLM narrative may explain or challenge returned values. It may not add holdings, prices,
   transactions, or tax facts (existing rule).
3. Reads are free. Writes go through preview → digest → approval → commit → receipt.
4. Wealthplan stays read-only.
5. Live host: the Windows host owns the API on `127.0.0.1:8000`. Live reads and processes
   use the approved key-only SSH terminal route to that host. A Mac agent or GUI must not call
   Mac loopback, expose port 8000, or copy the live database.
6. Local only: ad-hoc GUIs render and process locally. No hosted page (for example a
   claude.ai artifact) receives holdings or balances.

## 6. Owner decisions (ratified 2026-11-12)

1. **Live access route:** the SSH terminal route to the Windows host. Not an ES proxy.
   Reason: the live database values stay on the canonical host.
2. **Agent write authority:** preview-only. Commit stays a human action.
3. **Single-dashboard rule:** ephemeral on-request views are allowed; a retained view moves
   into ES.
4. **Rendering and processing:** local only.
5. **Run-log migration (WP2):** the plan is approved. The live run still needs a verified
   backup, a target preview, and owner approval of that exact preview.
6. **Primary deliverable:** the benchmark comparison estimate leads WP1. Source: the
   handoff from the "Portfolio performance vs VOO/QQQ" session, not yet confirmed directly.

## 7. Recommended order

WP1 → WP3 (read-only) → WP5 gives grounded ad-hoc portfolio views with no live migration.
Then WP4 starting with the earnings brief. WP2 waits for decision 5.

## 8. Non-goals

- No new calculation engine, query language, or GraphQL layer.
- No multi-user auth, tenancy, or public listener.
- No change to methodology versions or historical comparisons.
- No removal of shipped UIs; they become renderers over the same contracts.

## 9. Acceptance evidence

- OpenAPI and fixture drift tests pass with the new resources.
- Every legacy `/api/portfolio/*` route has a v1 successor; ES calls no legacy route.
- MCP tool output matches the HTTP response byte-for-byte for each v1 fixture.
- Each converted ES view passes golden parity from its JSON model.
- One ad-hoc GUI (for example a performance report) passes the trace checker.
- Operations: a preview digest mismatch refuses commit; a repeated idempotency key returns
  the first run; no response or log contains balances or credentials.
