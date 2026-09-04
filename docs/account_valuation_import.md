# Historical account-valuation imports

`portfolio_tracker.jobs.import_account_valuation_manifest` is the only manual
writer for broker-reported historical whole-account totals. It does not infer
totals from holdings, prices, transactions, or a reconciliation residual.

## Source and mapping contract

One manifest maps one immutable brokerage statement or provider export to one
active investment account. Before preview, the job verifies:

- `source_document_sha256` against the exact source file bytes;
- `account_identity_sha256` against the local account ID, item ID, provider
  account ID, and item provider;
- an explicit mapping basis and an `exact` or `high` confidence value;
- the exact set of manifest and row fields, row count, decimal precision,
  account currency, IANA source timezone, completeness, and empty-account
  semantics;
- each row's `source_value_sha256`, which commits to the source document,
  source locator, effective date/timestamp, total, cash, currency, and status.

`as_of_at` must contain a timezone offset when the source reports a timestamp.
It must be `null` when the source reports only a date; the importer never
manufactures an end-of-day timestamp. `captured_at` is the actual manifest
capture time and becomes `fetched_at`. Each stored `source_reference` retains
the human source name, precise locator, mapping basis/confidence, and source
timezone. `source_record_id` retains the source document digest and locator;
the approved preview retains the exact manifest digest.

## Preview and apply

Preview is the default and performs no database writes:

```shell
python -m portfolio_tracker.jobs.import_account_valuation_manifest \
  --manifest private/valuation-manifest.json \
  --source private/broker-statement.pdf \
  --preview private/valuation-preview.json
```

The owner-only preview reports the exact source row count and classifies every
row as `missing_insert`, `existing_exact`, or `conflict`. Its plan digest binds
the manifest bytes, evidence bytes, account identity, all normalized values,
and the current account-valuation database state.

Apply requires a file-backed SQLite backup created after preview and before
apply. The backup must open read-only, pass `PRAGMA integrity_check`, contain
the account-valuation schema, and match the previewed account identity and
observation-key set. Apply takes a SQLite write lock, reparses the exact source
and manifest, rebuilds the plan, verifies the exact preview, and commits all
new observations atomically:

```shell
python -m portfolio_tracker.jobs.import_account_valuation_manifest \
  --manifest private/valuation-manifest.json \
  --source private/broker-statement.pdf \
  --preview private/valuation-preview.json \
  --backup backups/portfolio-before-valuation-import.db \
  --expected-plan-digest DIGEST_FROM_PREVIEW \
  --commit
```

An exact replay is a no-op. A changed interpretation of the same document
locator is a conflict that must be resolved by creating and approving a new,
truthful manifest/source record rather than overwriting prior evidence.
