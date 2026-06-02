"""Migrate a Plaid-tracked broker to SnapTrade.

Why this exists: Plaid caps investment-transaction history at 24 months.
SnapTrade typically goes back as far as the broker keeps. For a high-
activity, long-held account (your Robinhood individual, in this case),
swapping the data source unlocks years of pre-2024 history that backfills
the chart, fixes the "data incomplete" rows on the Trade Analysis card,
and improves long-window risk metrics.

Sequence
========
The script assumes you've already linked the same broker via SnapTrade's
connection portal (`/api/snaptrade/connection-portal-url`). It then:

  1. Runs a *deep* SnapTrade sync (default 10-year lookback) so SnapTrade
     has data BEFORE we destroy the Plaid copy.
  2. Verifies every Plaid account on the target item has a SnapTrade
     counterpart matched by mask. If any are missing, bails before
     touching anything.
  3. Backs up the Plaid item, accounts, snapshots, and transactions to
     `backups/migration_<item>_<ts>.json` so the operation is reversible.
  4. Deletes Plaid-sourced rows in FK-safe order.
  5. Re-bootstraps `portfolio_values_daily` so the chart picks up the
     longer history.

Defaults to dry-run. Pass `--commit` to actually mutate the database.

Usage
=====
    # See what would happen
    python -m portfolio_tracker.jobs.migrate_broker_to_snaptrade \\
        --plaid-item-id 1 --snaptrade-profile primary

    # Actually do it
    python -m portfolio_tracker.jobs.migrate_broker_to_snaptrade \\
        --plaid-item-id 1 --snaptrade-profile primary --commit

    # Customize the deep-sync window
    python -m portfolio_tracker.jobs.migrate_broker_to_snaptrade \\
        --plaid-item-id 1 --snaptrade-profile primary \\
        --lookback-days 1825 --commit
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from portfolio_tracker.db import SessionLocal
from portfolio_tracker.models import (
    Account,
    HoldingSnapshot,
    InvestmentTransaction,
    Item,
    ItemSource,
)

_BACKUP_DIR = Path(__file__).resolve().parents[3] / "backups"
_DEFAULT_LOOKBACK_DAYS = 3650  # 10 years
_BACKUP_LIMIT_BYTES = 100 * 1024 * 1024  # 100 MB sanity cap


def run(
    plaid_item_id: int,
    snaptrade_profile: str,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    commit: bool = False,
) -> int:
    """Migrate a Plaid item to SnapTrade. Returns 0 on success.

    Always validates and shows the plan first. With `commit=False` (the
    default), exits before any mutation. With `commit=True`, performs the
    backup → delete → bootstrap sequence.
    """
    # Imported lazily so the module is import-safe even if the SnapTrade
    # SDK fails to load (e.g., on a machine without snaptrade installed).
    from portfolio_tracker.api.routes.snaptrade import (
        SnapTradeProfile,
    )
    from portfolio_tracker.api.routes.snaptrade import (
        sync as snaptrade_sync,
    )
    from portfolio_tracker.jobs import daily_values

    profile_enum = SnapTradeProfile(snaptrade_profile)

    with SessionLocal() as session:
        # ---- 1. Validate Plaid item ----------------------------------
        item = _validate_plaid_item(session, plaid_item_id)
        plaid_accounts = list(
            session.execute(select(Account).where(Account.item_id == item.item_id)).scalars().all()
        )
        plaid_masks = {_normalize_mask(a.mask) for a in plaid_accounts if a.mask}

        print(f"Plaid item {item.item_id}: {item.institution_name}")
        for a in plaid_accounts:
            print(
                f'  acct {a.account_id} "{a.name}" type={a.type}/{a.subtype or "-"} mask={a.mask}'
            )

        if not plaid_masks:
            raise RuntimeError(
                "Plaid accounts have no masks; can't match to SnapTrade. "
                "Bailing — investigate manually."
            )

        # ---- 2. Deep SnapTrade sync ----------------------------------
        print(
            f"\nRunning deep SnapTrade sync (profile={profile_enum.value}, "
            f"lookback={lookback_days}d)..."
        )
        result = snaptrade_sync(session, profile_enum, lookback_days=lookback_days)
        print(
            f"  items_synced={result.items_synced} "
            f"accounts={result.accounts_synced} "
            f"holdings={result.holdings_written} "
            f"txs={result.transactions_written}"
        )

        # ---- 3. Verify mask coverage on SnapTrade --------------------
        snaptrade_account_rows = session.execute(
            select(Account, Item)
            .join(Item, Item.item_id == Account.item_id)
            .where(Item.source == ItemSource.SNAPTRADE.value)
        ).all()
        snaptrade_masks: dict[str, Account] = {
            _normalize_mask(a.mask): a for a, _ in snaptrade_account_rows if a.mask is not None
        }
        unmatched = plaid_masks - set(snaptrade_masks.keys())
        if unmatched:
            raise RuntimeError(
                f"SnapTrade is missing accounts for Plaid masks {sorted(unmatched)}. "
                f"Confirm the broker is linked via the SnapTrade portal and "
                f"that the same accounts are visible there."
            )
        print(f"\nAll {len(plaid_masks)} Plaid masks matched on SnapTrade side:")
        for m in sorted(plaid_masks):
            ms_acct = snaptrade_masks[m]
            print(f'  mask {m}: snaptrade acct {ms_acct.account_id} "{ms_acct.name}"')

        # ---- 4. Show deletion plan -----------------------------------
        plaid_account_ids = [a.account_id for a in plaid_accounts]
        n_snaps = (
            session.execute(
                select(func.count())
                .select_from(HoldingSnapshot)
                .where(HoldingSnapshot.account_id.in_(plaid_account_ids))
            ).scalar()
            or 0
        )
        n_txs = (
            session.execute(
                select(func.count())
                .select_from(InvestmentTransaction)
                .where(InvestmentTransaction.account_id.in_(plaid_account_ids))
            ).scalar()
            or 0
        )
        print("\nWill delete:")
        print(f"  - {n_snaps} holdings_snapshots rows")
        print(f"  - {n_txs} investment_transactions rows")
        print(f"  - {len(plaid_accounts)} accounts")
        print(f"  - 1 item (item_id={item.item_id})")

        if not commit:
            print("\n=== DRY RUN — no changes made ===")
            print("Re-run with --commit to execute.")
            return 0

        # ---- 5. Backup -----------------------------------------------
        backup_path = _backup_plaid_data(session, item, plaid_accounts, plaid_account_ids)
        print(f"\nBacked up to {backup_path}")

        # ---- 6. Delete in FK-safe order ------------------------------
        session.execute(
            delete(HoldingSnapshot).where(HoldingSnapshot.account_id.in_(plaid_account_ids))
        )
        session.execute(
            delete(InvestmentTransaction).where(
                InvestmentTransaction.account_id.in_(plaid_account_ids)
            )
        )
        session.execute(delete(Account).where(Account.item_id == item.item_id))
        session.execute(delete(Item).where(Item.item_id == item.item_id))
        session.commit()
        print("Plaid data deleted.")

        # ---- 7. Re-bootstrap daily values ----------------------------
        print("\nRe-bootstrapping portfolio_values_daily (this can take a minute)...")

    # Open a fresh session for bootstrap so daily_values doesn't see a
    # stale cache; the previous session is closed at this point.
    written = daily_values.run(bootstrap=True)
    print(f"  wrote {written} daily-value rows")

    print("\nMigration complete. Reload the dashboard to see the longer history.")
    return 0


def _validate_plaid_item(session: Session, item_id: int) -> Item:
    item = session.get(Item, item_id)
    if item is None:
        raise RuntimeError(f"item {item_id} not found")
    if item.source != ItemSource.PLAID.value:
        raise RuntimeError(
            f"item {item_id} is source={item.source}, not 'plaid' — refusing "
            f"to migrate. Check `--plaid-item-id`."
        )
    return item


def _normalize_mask(s: str | None) -> str | None:
    """Reduce broker mask strings to a comparable canonical form.

    Different aggregators emit different mask formats:
      * Plaid: bare 4-digit suffix (e.g., "1234")
      * SnapTrade Fidelity: asterisk-prefixed last 4 (e.g., "*****1234")
      * SnapTrade Robinhood: full account number; only the last 4 are stable

    All three encode the same trailing 4 digits — so we normalize to
    the *last 4 digits* of the numeric portion. That's enough to match
    accounts in practice. Returns the full digit string if shorter than
    4 (shouldn't happen for real brokerage masks, but defensive).
    """
    if s is None:
        return None
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return None
    return digits[-4:]


def _backup_plaid_data(
    session: Session,
    item: Item,
    accounts: list[Account],
    account_ids: list[int],
) -> Path:
    """Dump everything we're about to delete to a JSON file.

    Restoration is manual (load JSON, INSERT rows back) but the data is
    preserved so we can recover from a bad migration. Sanity-caps backup
    size at 100 MB — anything bigger probably indicates a bug, e.g.,
    account_ids that accidentally include too much.
    """
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    path = _BACKUP_DIR / f"migration_item{item.item_id}_{timestamp}.json"

    snaps = (
        session.execute(select(HoldingSnapshot).where(HoldingSnapshot.account_id.in_(account_ids)))
        .scalars()
        .all()
    )
    txs = (
        session.execute(
            select(InvestmentTransaction).where(InvestmentTransaction.account_id.in_(account_ids))
        )
        .scalars()
        .all()
    )

    payload = {
        "exported_at_utc": timestamp,
        "purpose": "migration_plaid_to_snaptrade",
        "item": _row_to_dict(item),
        "accounts": [_row_to_dict(a) for a in accounts],
        "holdings_snapshots": [_row_to_dict(s) for s in snaps],
        "investment_transactions": [_row_to_dict(t) for t in txs],
    }
    serialized = json.dumps(payload, indent=2, default=str)
    if len(serialized.encode("utf-8")) > _BACKUP_LIMIT_BYTES:
        raise RuntimeError(
            f"Backup size exceeds {_BACKUP_LIMIT_BYTES // (1024 * 1024)} MB — "
            f"refusing to write. Investigate the row counts above."
        )
    path.write_text(serialized, encoding="utf-8")
    return path


def _row_to_dict(row) -> dict:
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plaid-item-id",
        type=int,
        required=True,
        help="ID of the Plaid item to retire (see `items` table).",
    )
    parser.add_argument(
        "--snaptrade-profile",
        choices=["primary", "spouse"],
        required=True,
        help="Which SnapTrade profile owns the broker connection.",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=_DEFAULT_LOOKBACK_DAYS,
        help=(
            f"How far back to deep-sync from SnapTrade. Default "
            f"{_DEFAULT_LOOKBACK_DAYS} (10 years). Capped at 3650 by the "
            f"sync route."
        ),
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually mutate the database. Without this flag, dry-run only.",
    )
    return parser


if __name__ == "__main__":
    args = _build_argparser().parse_args()
    raise SystemExit(
        run(
            plaid_item_id=args.plaid_item_id,
            snaptrade_profile=args.snaptrade_profile,
            lookback_days=args.lookback_days,
            commit=args.commit,
        )
    )
