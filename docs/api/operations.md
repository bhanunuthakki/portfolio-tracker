# Governed operations

One contract for work that changes state: **catalog → preview (plan digest) → run →
receipt**. Code: `services/operations.py`. It is WP2 of
[the headless capability scope](../design/headless_capability_layer_scope.md).

## Surfaces

| Surface | Catalog | Preview | Run | Run status |
|---|---|---|---|---|
| CLI `python -m portfolio_tracker.jobs.operations` | `catalog` | `preview NAME` | `run NAME` | `show RUN_ID` |
| HTTP `/api/v1/operations` | `GET` | `POST /{name}/preview` | none | `GET /runs/{id}` |
| Agent tools (MCP) | `operations` | `operations_preview` | none | `operations_runs` |

Runs start only from the CLI on the canonical host. An agent or a browser can preview
and read status, never commit.

- **Preview** writes nothing. It returns a value-free summary and a `plan_digest`.
- **Run** needs `--expected-plan-digest` (the preview's digest) and `--idempotency-key`.
  A repeated key returns the first run (`replayed: true`); the same key with another
  operation or digest is refused (`idempotency_key_reused`).
- Every run is recorded in `operation_runs` with its status (`succeeded`, `refused`,
  `failed`), a stable error code, and a receipt of revisions, counts, and file names.
  Receipts never contain balances or holdings.

## Catalog

| Operation | Effect | Interface | Backup |
|---|---|---|---|
| `schema_migrate` | local_write | service | written and verified by the run |
| `backup` | local_write | service | is the backup |
| `daily_refresh` | external_call | own CLI | no |
| `benchmarks` | external_call | own CLI | no |
| `reconcile_cashflow_manifest` | local_write | own CLI (preview, digest, `--commit`) | yes |
| `import_account_valuation_manifest` | local_write | own CLI (preview, digest, `--commit`) | yes |

`provider_transaction_corrections` keeps no HTTP or operations commit path.

## Database additions (`schema_migrate`)

The fast, tested path for schema changes:

1. **Develop.** Add the model and an Alembic revision. `tests/test_schema_drift.py`
   fails if the migrated schema and the models disagree. Add a focused migration test
   only when the revision moves data.
2. **Preview.** `preview schema_migrate --database-path <db>` copies the database into
   memory, runs every pending revision on the copy, and compares before and after. The
   live file is opened read-only.
   - `additive`: only new tables, nullable or defaulted columns, and indexes; every
     existing table keeps its columns, indexes, and row content; integrity and foreign-key
     checks pass.
   - `requires_approval`: anything else (removed or changed columns or indexes, changed
     rows, a NOT NULL column without a default).
   - `blocked`: the rehearsal failed or an integrity check failed.
   - `noop`: the database is already at head.
3. **Run.** `run schema_migrate --database-path <db> --expected-plan-digest <digest>
   --idempotency-key <key>`. The run rehearses again and needs the same digest, so
   the schema has not changed since the preview. It writes a backup to `backups/` next to
   the database and checks that the backup opens, passes `integrity_check`, and is at the
   previewed revision. Then it upgrades the live file and proves the live schema equals
   the rehearsed schema. A `requires_approval` plan also needs `--approve-non-additive`,
   which records the owner's approval.

On a failed post-check the run is `refused` with `post_check_failed`, and the receipt
names the backup file to restore from. Nothing is restored automatically.

Authority: `AGENTS.md` lets an agent run an `additive` plan for work the owner asked
for, after it reports the preview. Every other plan needs the owner's approval of that
exact preview.
