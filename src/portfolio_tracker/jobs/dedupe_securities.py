"""One-shot dedupe of duplicate (ticker, ticker) security rows.

Both Plaid and SnapTrade ingest paths assign their own internal IDs to
the same instrument (`upsert_security` keys on `plaid_security_id`,
which differs across aggregators). Result: a single ticker (e.g., NU)
ends up as multiple `security_id` rows. Transactions land under one ID,
historical prices under another, today's holdings under a third.

Symptoms this fixes:
  * Walk-back valuation drops positions whose `security_id` has no
    `prices` rows. The walk-back reconstructs the right share count but
    multiplies by zero, so V on past dates is silently understated by
    the value of those phantom-priced positions. Day-to-day this looks
    like step-ups when other tickers (with intact prices) come in or
    out of the rolling state.
  * Trade analysis splits each ticker's history across rows.
  * Cost-basis / ticker overrides land on whichever ID was active at
    the time and silently miss the other.

Strategy: pick the lowest-ID, most-priced security per ticker as
canonical. Reassign every FK referencing a duplicate to the canonical.
Merge prices (skip duplicates on PK collision). Delete the leftover
security rows.

Run once:
    python -m portfolio_tracker.jobs.dedupe_securities --commit

Defaults to dry-run.
"""

from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict
from pathlib import Path

from portfolio_tracker.config import get_settings


def find_duplicates(cur: sqlite3.Cursor) -> dict[str, list[int]]:
    """Group security_ids by ticker (only tickers with >1 row)."""
    cur.execute(
        "SELECT ticker, security_id FROM securities "
        "WHERE ticker IS NOT NULL AND TRIM(ticker) != ''"
    )
    by_ticker: dict[str, list[int]] = defaultdict(list)
    for ticker, sid in cur.fetchall():
        by_ticker[ticker].append(sid)
    return {t: ids for t, ids in by_ticker.items() if len(ids) > 1}


def pick_canonical(cur: sqlite3.Cursor, sids: list[int]) -> int:
    """Canonical = sid with most price rows. Tiebreak: lowest sid."""
    candidates = []
    for sid in sids:
        cur.execute("SELECT COUNT(*) FROM prices WHERE security_id = ?", (sid,))
        candidates.append((sid, cur.fetchone()[0]))
    candidates.sort(key=lambda r: (-r[1], r[0]))
    return candidates[0][0]


def reassign(cur: sqlite3.Cursor, dup_sid: int, canonical: int) -> dict[str, int]:
    """Repoint every FK from `dup_sid` to `canonical`.

    SQLite-friendly: we issue plain UPDATE statements ignoring uniqueness
    failures by deleting the conflicting source rows first. Each table
    has its own conflict shape, handled below.
    """
    counts: dict[str, int] = {}

    # holdings_snapshots: PK is (snapshot_date, account_id, security_id).
    # Collisions are rare (would require both sids to have a row for the
    # same account+date). For ones that collide, prefer the canonical
    # (delete the dup row). For non-collisions, repoint security_id.
    cur.execute(
        """
        DELETE FROM holdings_snapshots
        WHERE security_id = ?
          AND (snapshot_date, account_id) IN (
              SELECT snapshot_date, account_id
              FROM holdings_snapshots
              WHERE security_id = ?
          )
        """,
        (dup_sid, canonical),
    )
    counts["holdings_collision_dropped"] = cur.rowcount
    cur.execute(
        "UPDATE holdings_snapshots SET security_id = ? WHERE security_id = ?",
        (canonical, dup_sid),
    )
    counts["holdings_repointed"] = cur.rowcount

    # investment_transactions: PK is plaid_investment_transaction_id (a
    # string). Repoint security_id without conflict risk.
    cur.execute(
        "UPDATE investment_transactions SET security_id = ? WHERE security_id = ?",
        (canonical, dup_sid),
    )
    counts["transactions_repointed"] = cur.rowcount

    # prices: PK is (security_id, date). Insert dup's rows that don't
    # already exist on canonical, then delete dup's rows.
    cur.execute(
        """
        INSERT OR IGNORE INTO prices (security_id, date, close)
        SELECT ?, date, close FROM prices WHERE security_id = ?
        """,
        (canonical, dup_sid),
    )
    counts["prices_merged"] = cur.rowcount
    cur.execute("DELETE FROM prices WHERE security_id = ?", (dup_sid,))

    # cost_basis_overrides: PK is (account_id, security_id). Delete dup's
    # row only if a canonical row already exists for the same account.
    cur.execute(
        """
        DELETE FROM cost_basis_overrides
        WHERE security_id = ?
          AND account_id IN (
              SELECT account_id FROM cost_basis_overrides WHERE security_id = ?
          )
        """,
        (dup_sid, canonical),
    )
    counts["cb_override_collision_dropped"] = cur.rowcount
    cur.execute(
        "UPDATE cost_basis_overrides SET security_id = ? WHERE security_id = ?",
        (canonical, dup_sid),
    )
    counts["cb_overrides_repointed"] = cur.rowcount

    # ticker_overrides: PK is security_id. Delete dup if canonical has one.
    cur.execute(
        """
        DELETE FROM ticker_overrides
        WHERE security_id = ?
          AND EXISTS (SELECT 1 FROM ticker_overrides WHERE security_id = ?)
        """,
        (dup_sid, canonical),
    )
    counts["ticker_override_collision_dropped"] = cur.rowcount
    cur.execute(
        "UPDATE ticker_overrides SET security_id = ? WHERE security_id = ?",
        (canonical, dup_sid),
    )
    counts["ticker_overrides_repointed"] = cur.rowcount

    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Apply changes. Default is dry-run (rolls back at the end).",
    )
    args = parser.parse_args()

    settings = get_settings()
    db_path = Path(settings.database_url.replace("sqlite:///", ""))
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()

    dups = find_duplicates(cur)
    if not dups:
        print("No duplicate securities found.")
        return

    print(f"Found {len(dups)} ticker(s) with >1 security_id row:")
    total_aggregate: dict[str, int] = defaultdict(int)
    sids_to_drop: list[int] = []

    for ticker, sids in sorted(dups.items()):
        canonical = pick_canonical(cur, sids)
        dup_sids = [s for s in sids if s != canonical]
        for d in dup_sids:
            counts = reassign(cur, d, canonical)
            sids_to_drop.append(d)
            for k, v in counts.items():
                total_aggregate[k] += v
        print(
            f"  {ticker}: canonical={canonical}  merged_dups={dup_sids}"
        )

    # Drop duplicate rows last. With FKs satisfied (we repointed everyone),
    # this is safe.
    for sid in sids_to_drop:
        cur.execute("DELETE FROM securities WHERE security_id = ?", (sid,))
    print(f"\nDropped {len(sids_to_drop)} duplicate security row(s).")

    print("\nAggregate impact:")
    for k, v in sorted(total_aggregate.items()):
        print(f"  {k}: {v}")

    if args.commit:
        conn.commit()
        print("\nCOMMITTED.")
    else:
        conn.rollback()
        print("\nDRY RUN — rolled back. Pass --commit to apply.")
    conn.close()


if __name__ == "__main__":
    main()
