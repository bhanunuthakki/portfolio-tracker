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

1. Complete broker observations own observed opening and ending values.
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

## Reconstruction

For each account/security, quantities are reconstructed on a common
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

## Certification

An observed boundary is not inferred from the presence of one holding row. A
certified reconstruction ultimately requires:

- exact requested and returned boundaries;
- a complete ending broker observation;
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

## Safe writes and recovery

Reconciliation is preview-first. A private preview names the exact source
records, targets, decisions, assumptions, affected accounts/date range, and
row counts. Ordinary console output contains counts and opaque digests only.

Live application requires:

1. a distinct SQLite backup;
2. a successful restore/integrity check;
3. migration and reconciliation rehearsal on the restored copy;
4. an exact approved plan digest;
5. locked revalidation of database and source inputs;
6. one atomic commit; and
7. post-write equation, provenance, duplicate, and idempotency checks.

No live migration or correction is implied by code completion or a successful
dry run.
