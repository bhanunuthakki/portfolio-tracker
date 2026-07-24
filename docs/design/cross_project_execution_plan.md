# Portfolio Data Service Program — Cross-Project Execution Plan

**Status:** Recommended sequencing, 2026-07-23. Companion to the three PRDs; owns no
requirements of its own.
**PRDs:**

- Provider: `portfolio-tracker/docs/design/portfolio_data_service_prd.md`
- Consumer: `earnings-summary/docs/design/portfolio_intelligence_consolidation_prd.md`
- Consumer: `wealthplan/docs/design/portfolio_tracker_api_migration_prd.md`

---

## 1. Shape of the program

Three repositories, one provider, two consumers, and one hard rule: nothing is
removed until its replacement has passed a gate. The dependency graph is:

```
Phase 0 (joint ratification, one session)
        │
        ▼
PT Phase 1 — /api/v1 additive contract          ← the single critical path
        │
        ├────────────► Track A: wealthplan (small, independent, finishes first)
        │                 client → dual-read parity → cutover → provenance → retire SQLite
        │
        └────────────► Track B: earnings-summary (large, the long pole)
                          typed client → adoption → journal/owner-state migration
                          → surface parity → PT removes bridge + analysis surfaces
        │
        ▼
PT Phases 5–6 — legacy retirement + hardening (gated on BOTH tracks)
```

Key asymmetry: wealthplan needs only four resources (health, portfolio-snapshot,
accounts, analytics/positioning) and has no dependency on earnings-summary.
Earnings-summary needs the full analytics surface plus a data migration and a UI
absorption. So wealthplan's migration completes early and de-risks the contract for
the bigger consumer; earnings-summary's Phase 4 is the program's long pole and the
only thing gating Portfolio Tracker's slim-down.

## 2. Recommended order of work

### M0 — Joint Phase 0 ratification (one working session, all three repos)

Close every Phase 0 open decision in one sitting so no track stalls later:

1. Shared contract decisions (bind all three): detailed Tax treatment field/enum
   (taxable / pretax / Roth / HSA / unknown, distinct); canonical-account identity +
   exclusion-reason shape; equity-fraction placement (recommend: materialized in the
   bulk snapshot AND served by `analytics/positioning`, same methodology version);
   service discovery (recommend: default `http://127.0.0.1:8000` with per-consumer
   env override — the at-logon `PortfolioTrackerApiServer` task already keeps it up);
   local bearer token (recommend: defer while loopback-only, keep the field in the
   contract); OpenAPI artifact policy (recommend: checked-in, drift-diffed in CI).
2. Per-repo inventories: PT route/table/job classification; wealthplan direct-read
   surfaces; ES bridge-consumer and client-call-site classification; PT journal/
   advisory-history dispositions (migrate-active vs import-provenance vs archive).
3. Vocabulary: add **Portfolio Data Service** to `portfolio-tracker/DEFINITIONS.md`;
   add any consumer terms (e.g. Wealthplan's Portfolio input snapshot) before code.
4. Rollback windows and parity tolerances, written down before any comparison runs.

Deliverable: a short ratified addendum (checked into each repo's docs) so all three
PRDs point at the same answers.

### M1 — PT Phase 1, wealthplan-blocking subset first

Build `/api/v1` additively in two slices, ordered by consumer need:

- **Slice 1 (unblocks Track A):** response envelope, `health`, `accounts`,
  `portfolio-snapshot`, `analytics/positioning`, OpenAPI + sanitized fixtures,
  contract tests.
- **Slice 2 (unblocks Track B):** `transactions`, `positions`,
  `position-snapshots`, `cash-flows`, `securities`, `sync-runs`, `data-quality`,
  `analytics/performance`, `analytics/position-performance`, `analytics/risk`,
  exit-quality parity, error catalogue, idempotency, deprecation metadata.

Existing endpoints are untouched. Both consumers review the fields/fixtures they
depend on before each slice is declared done (the Phase 1 exit gate).

### M2 — Track A: wealthplan Phases 1→5 (start after Slice 1)

Client + fixtures → dual-read aggregate parity (sanitized: pass/fail, counts,
dates, deltas only) → cutover (`PORTFOLIO_TRACKER_DB` → `PORTFOLIO_TRACKER_API_URL`)
→ input provenance + saved-plan stamping → UX honesty states → retire SQLite
adapter, name-based Tax treatment inference, and balance-based dedup.

Do this first among the consumers: it is the smallest scope, it removes the
schema-drift/file-lock risk both the wealthplan PRD and the federation doc flag,
and its parity run is the cheapest end-to-end validation of the new contract
(canonical accounts, Tax treatment, equity fraction) before earnings-summary builds
on the same fields.

### M3 — Track B step 1: earnings-summary typed client + adoption (Phases 1–2)

Replace the `_f()`-coercion client with the typed v1 client; move every live read
(`advisor`, `allocation`, `ask`, `attribution`, `calibration`, dashboards) onto it
behind a temporary switch with dual-read parity on decision-grade outputs; land the
exit-quality/position-alpha joins Review needs.

**Interleaving rule with the Personal Investment Partner program:** do M3 before or
together with Partner P0 (Risk Budget history, Incremental Dollar Recommendation),
so P0 lands once on the stable v1 contract instead of being built on legacy
endpoints and migrated twice. Partner P1 (Investment Decision Card, Discovery) has
no PT dependency and can proceed in parallel at any time.

### M4 — Track B step 2: journal and owner-state migration (Phase 3)

The first destructive-adjacent step; full backup/preview/approval discipline in both
repos. Import `trade_decisions`/`trade_tags` into the `decisions` ledger
(reconcile-not-duplicate; owner-first view protected); migrate human-capital and
owner-intent posture; transfer CIO persona/context ownership (public rubric into ES
context, `CIO_CONTEXT.local.md` moved locally by the owner, never committed); freeze
legacy journal mutations in PT at cutover; retire ES's regex import of the tracker
context file.

### M5 — Track B step 3: surface parity and cutover (Phase 4)

Capability-by-capability, each with its own acceptance gate — thesis health,
Review ledger/analysis/timeline/scorecard, action-queue absorption into Today +
Senior Partner Brief, advisory parity via brief + Ask, coaching merge, posture and
human-capital surfaces. PT replaces migrated navigation with redirects, stops
scheduled CIO/coaching/Cockpit work, ships the reduced operations console.
Scheduled-task changes on both sides follow the ops rule: jobs run against the main
checkouts, updated with backups and previews.

### M6 — PT Phases 5–6: retirement and hardening (gated on M2 + M5)

Delete `services/earnings_summary.py` and its routes, the migrated
services/tables/pages, and superseded endpoints per the deprecation policy; then
harden: both consumers' fixture suites run in CI against the same OpenAPI artifact,
drift fails CI, backup/restore re-verified, latency measured.

## 3. Parallelism summary

| Can run in parallel | Must be serialized |
| --- | --- |
| M2 (wealthplan) and M3 (ES adoption) once their slices ship | M0 before everything |
| PT Slice 2 build alongside wealthplan's Slice-1 consumption | M1 Slice 1 before M2; Slice 2 before M3 completion |
| Partner P1 alongside all of Track B | M3 before M4 before M5 (each gate blocking) |
| ES cron/task updates alongside M5 surfaces | M6 strictly after both M2 and M5 gates + rollback expiry |

## 4. Standing guardrails (all milestones)

- Additive before destructive; every removal gated on a passed replacement gate and
  an expired, owner-approved rollback window.
- No methodology change rides along with a transport migration (wealthplan's 95%
  cap, ES valuation/projection math, PT calculation versions).
- No live values in fixtures, parity output, logs, or git; both `portfolio.db`
  files, `.env`, tokens, and `*.local.md` stay out of every diff.
- Every DB-touching step: verified backup, restore-tested, previewed row counts,
  explicit approval, idempotent import.
- Daily operations (PT daily refresh, at-logon API server, ES cron fleet) must
  survive every milestone; scheduled-task edits are previewed and reversible.
