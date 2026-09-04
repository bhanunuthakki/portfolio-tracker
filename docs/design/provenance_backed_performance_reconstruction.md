# Provenance-backed performance reconstruction

## Outcome

Portfolio Tracker measures the performance of the owner's actual portfolio
decisions over time. A return window uses the positions actually held during
that window and the external capital actually present on each date. It must
not project today's holdings backward as though they were always owned.

The primary calculation is whole-portfolio Modified Dietz. Position-level
analysis may explain that result, but it does not replace the whole-portfolio
return.

## Authorities

The system has one authority for each kind of fact:

1. Complete broker account-valuation observations own observed opening and
   ending whole-account values. Each observation preserves its exact effective
   date/timestamp, capture time, provider or statement source, source locator,
   source-record identity, and payload hashes.
2. `investment_transactions` owns normalized economic activity.
3. Cash-flow source attestations and source events own immutable evidence from
   statements, provider exports, and owner-approved reconciliations.
4. Append-only reconciliation decisions own the interpretation that links a
   source event to one normalized transaction and one effective classification.
5. The external-flow ledger is a derived projection. It is not another store.
6. Performance equation receipts freeze the exact valuation, flow, benchmark,
   provenance, and assumption inputs used for one calculation.

Private source filenames, account identities, provider transaction IDs, row
locators, amounts, and descriptions stay in the private database or ignored
mode-0600 artifacts. The committed tree contains only schemas, rules, tests,
and sanitized examples.

The public valuation-provenance lookup accepts an opaque observation key and
returns only source kind/provider, dates, completeness flags, capture time, and
payload digest. It deliberately omits the private source reference, row
locator, provider account locator, record ID, and balance values.

## Source precedence

- A matching aggregator transaction retains its provider identity and is the
  economic record. Statement evidence corroborates it.
- A first-party broker statement may create a supplemental normalized
  transaction only when no provider transaction represents the economic
  event.
- If a provider transaction arrives later, it supersedes the supplemental
  target in the effective ledger without deleting the historical evidence.
- Conflicting, ambiguous, provisional, or unresolved decisions never silently
  enter a certified flow ledger.
- Several independent source events may corroborate one economic transaction;
  one source event has exactly one current reconciliation decision.

Statement reconciliation manifest schema v4 may resolve an already captured
provider event only through a narrow append-only supersession. The private
manifest binds the exact statement/owner evidence row, provider source-event
and source-row digests, and the exact current provider decision key and payload
digest. Planning fails unless that current decision is the single approved,
provider-created `unresolved` decision and the evidence resolves to the same
normalized provider transaction. Apply revalidates the plan under the database
write lock, retains the old decision as superseded, creates the owner-approved
replacement, and records both decisions in the atomic reconciliation-run
receipt. It cannot replace a resolved provider decision or reinterpret a
different provider transaction.

### Account-mapping evidence

The private evidence inventory must explicitly provide
`account_identity_sha256`, `account_mapping_basis`,
`account_mapping_confidence`, and `account_mapping_evidence_sha256`. Missing
mapping provenance is invalid; the manifest builder never derives an `exact`
mapping from the selected database account. Basis is one of
`provider_account_id`, `statement_account_identifier`, or `owner_confirmed`,
and confidence is `exact`, `high`, or `provisional`. A provisional mapping is
retained but cannot certify source coverage.

`account_mapping_evidence_sha256` is the SHA-256 of this canonical JSON value
(sorted keys and compact separators):

```json
{
  "account_identity_sha256": "…",
  "account_mapping_basis": "statement_account_identifier",
  "account_mapping_confidence": "high",
  "identity_version": "cashflow_account_mapping.v1",
  "source_document_sha256": "…"
}
```

The builder recomputes that digest and verifies `account_identity_sha256`
against the selected database account. The execution manifest then carries the
four committed mapping inputs—account identity, basis, confidence, and source
document digest—so its existing manifest/attestation hashes bind the same
mapping assertion without adding a second persisted representation. Changing
any mapping input requires a new evidence digest and produces a different
manifest identity.

## Source-event identity and dates

A statement without broker transaction IDs is identified by the source
document SHA-256, stable account-identity SHA-256, exact row/page locator, and
normalized source-row SHA-256. This is evidence identity, not cross-document
economic-event identity.

The source activity, process, and settlement dates are retained separately.
The reconciliation decision records the ledger effective date, timezone, and
one unambiguous basis: `source_activity`, `source_process`,
`source_settlement`, `provider_posting`, or `owner_resolved`. A shifted provider
posting date may match a statement event only through an exact provider
identity and payload hash or a unique, bounded, explicitly recorded match. It
must never create a second supplemental event.

For a statement-backed manifest, Activity Date is the default Modified-Dietz
effective date for every cash flow, including a provider-matched row whose
aggregator posting date is later. The provider date remains retained through
the normalized target transaction. Pure aggregator-only events use
`provider_posting`. The choice is recorded per decision and may be superseded
rather than edited in place.

## Complete source disposition

A source range is certifying only when the source-specific parser proves its
candidate-row count and every candidate has exactly one disposition:

- provider exact;
- statement supplement;
- internal;
- excluded; or
- unresolved.

`unresolved` and provisional decisions keep the range non-certifying. A source
with zero candidates may certify only when the parser records zero candidates;
an empty hand-authored event list is insufficient.

An immutable `unresolved_classification` gap describes the provider capture at
ingestion time. The current coverage projection closes that gap only when every
source event implicated by its dates has exactly one approved, digest-valid,
non-provisional resolved decision. The historical gap row remains visible.
`provider_history_unavailable` and every other source gap remain uncovered and
cannot be closed by decision supersession.

For provider APIs, pagination continues until the provider-declared total is
fetched. Duplicate IDs, malformed records, count mismatches, changed economic
fields under an existing transaction ID, and unrecognized activity fail
closed. The resulting attestation proves complete delivery of what the
provider returned for the requested account/range; it explicitly does **not**
claim that the provider possesses the broker's complete archive. The persisted
`broker_archive_coverage` is `unasserted` unless a provider explicitly asserts
the full requested range or a statement/owner authority attests it. Statement
evidence can fill provider-archive uncertainty without being relabeled as
provider-delivered evidence. Arithmetic may remain available on complete
provider delivery, but its certification is `source_provisional` with
`broker_archive_coverage_not_complete` until archival coverage closes.

Provider source-event identity is based on provider, stable account identity,
and provider record ID—not on the requested sliding window. Overlapping daily
captures link to the same immutable event and current decision, keeping the
flow ID and equation receipt stable while each delivery attestation retains its
own range and record-set hash.

Provider transaction parser versions `plaid_investment_tx.v3` and
`snaptrade_account_activity.v3` apply explicit round-half-even normalization
to the database's declared six-decimal money and ten-decimal quantity grids.
This applies uniformly to SDK floats, strings, and Decimals, including direct
account totals and calculated holding values. The maximum rounding error is
half a storage quantum per normalized field: $0.0000005 for money and
0.00000000005 shares for quantity. Delivery and attestation hashes commit to
the normalized values and parser version, so this is a reproducible design
choice rather than implicit database truncation. Earlier v1/v2 attestations
remain readable and certifying because their stored values already satisfied
the same grids; v3 identifies the broader, explicit normalization rule for new
captures.

Migration 0031 also repairs a known legacy schema drift in which a database
could be stamped at revision 0023 while `policy_state` and
`policy_write_receipts` were absent. It creates only whichever of those 0023
tables are missing and seeds the required singleton state only when that table
must be recreated. Downgrading to 0030 retains them because they logically
belong to revision 0023.

An existing provider record ID with changed economics is never overwritten by
ordinary synchronization. The conflict produces an exact private correction
plan containing the provider record ID, changed field names, complete before
and after payloads, and hashes. Applying that plan requires an owner-approved
plan digest plus a byte-verified, integrity-checked SQLite backup and immutable
preview. The database mutation and an append-only
`provider_transaction_correction_receipts` row commit atomically. The receipt
retains the provider record locator, delivery-set hash, changed fields,
before/after payloads and hashes, approval time, and backup/preview hashes.
This is a narrow correction authority, not permission for provider sync to
edit historical rows silently.

An `ACATI` statement row with `Amount="--"`, an instrument, and a nonzero
quantity is an in-kind transfer, not a zero-dollar cash flow. Source coverage
continues to describe the full statement. Manifest schema v3 separately binds
the requested return scope as `(requested_return_start, requested_return_end]`.
An in-kind row on or before the opening boundary is not added as dated cash;
the opening positions own its value. An in-kind row inside that open-left,
closed-right scope makes manifest construction and independent validation fail
closed until its quantity is matched and valued. An out-of-scope in-kind row is
recorded as an explicit `unreconciled_difference` source gap, preventing a
future longer return window from silently treating that row as reconciled.
The requested dates are part of the execution-manifest and plan digests,
private preview, and applied-run receipt, but not the reusable statement
attestation identity. Robinhood attestations created before parser version
`robinhood_activity_csv.v4` are non-certifying because those versions did not
encode the cash-versus-in-kind distinction.

## Reconstruction

The calculation universe contains every active investment account plus any
account with activity in the requested window. A missing boundary or source
coverage for any included account makes the result unavailable; the calculator
does not silently narrow the book to the accounts that happened to have data.

Boundary values use one matched basis for both ends:

- complete whole-account totals for the identical account universe at both
  exact boundaries; or
- complete broker holdings at both ends, including a transaction-walked opening
  when the observed opening is unavailable.

The calculator never combines a whole-account total at one end with a
holdings-only value at the other. An index-ETF exclusion also requires holdings
support at both boundaries because a whole-account total cannot reveal the
excluded amount. When multiple observations exist for one account/date, the
latest capture is authoritative; a later incomplete capture cannot be hidden
by an older complete one.

When the caller omits an ending date, the window is anchored to the latest
complete broker observation—not the wall-clock date. Lookbacks such as 3m, 6m,
1y, and 2y must be calculated backward from that resolved ending observation.
An explicitly supplied date remains exact and fails closed if the corresponding
valuation boundary is absent.

For each account/security, modeled quantities are reconstructed on a common
split-adjusted basis:

```text
Q_open := Q_anchor - sum(Q_movements after opening through anchor)
```

Cash is reconstructed from the same complete normalized activity history.
Contributions and withdrawals are external flows. Buys, sells, dividends,
interest, and fees change positions or cash but are not external portfolio
flows. Transfers between accounts inside the contemporaneous portfolio
universe are internal; transfers across the universe boundary are external.

The whole-portfolio result is:

```text
investment gain = ending value - opening value - net external cash flow

Modified Dietz return = investment gain /
  (opening value + sum(time-weighted external cash flows))
```

The portfolio and every matched benchmark receive the identical dated
external-flow set. A deposit/reversal pair may net to zero while still changing
weighted capital between its two dates.

Benchmark refreshes separately assess the exact opening and ending source
marks required for SPY, QQQ, and every positive-weight policy component. A
refresh can successfully store historical rows while its requested endpoint is
still unavailable, for example before a same-day market close has been
published. That outcome is `partial`, with one diagnostic entry per symbol,
target date, required source date, and `missing` or `nonpositive` condition.
Weekend and known market-holiday boundaries resolve to the immediately prior
market close; an ordinary market session never silently falls back to an older
close. A null adjusted close is not itself a gap because the documented raw
close fallback remains eligible.

Provider snapshot jobs append account-valuation observations; they never infer
a broker total from the sum of holdings. Historical statement or provider-export
totals enter only through the strict manifest importer documented in
`docs/account_valuation_import.md`. Its dry-run plan binds the exact source
bytes, account mapping, row locators, values, completeness status, current
database state, and affected row count. Apply requires a verified restorable
backup, the approved plan digest, locked revalidation, and an atomic append-only
commit.

If a successful provider response supplies a direct account total and currency
but omits its own balance/holdings as-of timestamp, the observation is
structurally complete on the capture date. `as_of_at` remains null and the
source reference retains `cached_as_fetched_no_provider_as_of`; the calculator
does not manufacture a broker timestamp. Such a boundary is mathematically
usable, but `provider_valuation_as_of_unasserted` keeps the result
`source_provisional` even when broker-archive flow coverage is otherwise
complete. A disabled or unavailable provider state remains incomplete. Stored
observations are rehashed before selection or lookup; payload/key drift fails
closed.

## Certification

An observed boundary is not inferred from the presence of one holding row. A
certified reconstruction ultimately requires:

- exact requested and returned boundaries;
- a complete matched-basis opening and ending broker observation or a clearly
  labeled provisional transaction-walked opening;
- explicit account inclusion intervals;
- complete external-flow and full-activity coverage;
- one disposition per source candidate;
- no provider/supplemental duplicates;
- split-normalized position closure by account/security;
- account cash and total-balance closure at observed checkpoints;
- eligible historical prices on or before every valuation date;
- explicit cross-date transfer pairing and universe treatment;
- complete benchmark basis/provenance; and
- a receipt containing source-event, decision, transaction, price, and
  assumption identities.

Until full activity, account-lifecycle, cash/control-total, and price gates are
implemented and satisfied, a mathematically computable modeled opening is
reported as provisional rather than certified. No provisional label may be
upgraded merely because the arithmetic equation residual is zero.

`observed_certified` requires observed matched boundaries with provider- or
source-reported effective dates and complete broker archive coverage for the
cash-flow interval. `source_provisional` means the equation is available but
either the provider did not assert possession of the broker's complete archive
or a provider account total is dated to its successful capture because the
provider omitted its own effective timestamp. `modeled_provisional` means the
opening boundary itself is a transaction walkback. `unavailable` means a
required valuation, structural flow, source-delivery, price, or benchmark gate
failed.

The current implementation does not yet model historical account inclusion
intervals independently of current account state. It therefore fails closed
when the active-account universe lacks evidence, and a formerly active account
that has since been removed still requires explicit lifecycle evidence before
the relevant historical window can be certified.

## Canonical return retrieval

Application consumers and agent answers use
`GET /api/v1/analytics/performance` as the sole canonical return calculation.
They do not independently rebuild a headline return from holdings,
transactions, or a prior report.

For a reproducible fixed-window request, callers always supply all calculation
parameters explicitly:

```text
start_date=YYYY-MM-DD
end_date=YYYY-MM-DD
include_backfill=true
reserve_amount=0
exclude_index_etfs=false
```

For a "latest" 3-month, 6-month, 1-year, or 2-year lookback, the caller first
requests the endpoint with `include_backfill=true` to obtain the resolved
`series.end_date`, which is the latest complete broker observation. It then
subtracts exactly 90, 180, 365, or 730 calendar days and repeats the request
with both dates and all parameters explicit. The returned dates, methodology,
methodology version, calculation status, certification, and reason codes are
part of the answer.

For an unchanged database, two identical explicit requests produce the same
`series`. `meta.generated_at` is response-envelope timing metadata and is not
part of calculation identity. When the result is available,
`equation_receipt.calculation_id` is the deterministic identifier for its
valuation, flow-ledger, benchmark-price, option, and boundary inputs. When it
is unavailable, callers retain the stable failure reason codes and do not
substitute an offline estimate as the canonical result. Any separately useful
estimate must be labeled as such and must state its methodology and uncertainty.

## Safe writes and recovery

Reconciliation is preview-first. A private preview names the exact source
records, targets, decisions, assumptions, affected accounts/date range, and
row counts. Ordinary console output contains counts and opaque digests only.

Live application requires:

1. a distinct SQLite backup;
2. an explicit `--backup-path` and caller-bound `sha256:<digest>` reference;
3. read-only verification that the backup exists, is not the live database,
   matches that digest, passes SQLite `integrity_check` and
   `foreign_key_check`, and has the same sole Alembic revision as the live
   target;
4. migration and reconciliation rehearsal on the restored copy;
5. an exact approved plan digest;
6. locked revalidation of database and source inputs;
7. one atomic commit; and
8. post-write equation, provenance, duplicate, and idempotency checks.

The commit command is therefore shaped as follows; the digest must be computed
from the exact backup bytes after the backup is complete:

```shell
python -m portfolio_tracker.jobs.reconcile_cashflow_manifest \
  --source private/execution-manifest.json private/broker-source.csv \
  --preview-path private/approved-preview.json \
  --commit \
  --expected-plan-digest DIGEST_FROM_PREVIEW \
  --approved-at 2026-09-03T12:00:00-07:00 \
  --software-revision FULL_GIT_REVISION \
  --backup-path backups/portfolio-before-reconciliation.db \
  --backup-reference sha256:BACKUP_FILE_SHA256
```

The applied run receipt privately binds the resolved backup path and verified
content digest. Neither the path nor source data appears in the sanitized CLI
summary. A legacy `--backup-reference` without `--backup-path`, a descriptive
reference that is not `sha256:<digest>`, or any failed verification aborts
before reconciliation writes.

No live migration or correction is implied by code completion or a successful
dry run.
