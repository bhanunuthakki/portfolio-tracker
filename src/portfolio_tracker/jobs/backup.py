"""Local SQLite backup job.

Why this matters for a Plaid-backed tracker:

  * Plaid retains investment-transactions data for ~24 months. Anything older
    is permanently inaccessible from their API.
  * Our `investment_transactions` table is the canonical record of every
    buy / sell / dividend / cashflow we've ever ingested. If this table is
    lost, the historical TWR backfill can't be reconstructed.
  * Same goes for `holdings_snapshots` — once we've recorded a daily position
    snapshot, that's an artifact ONLY we have. Plaid won't replay it.

The DB itself is durable on disk (SQLite file), but a single accidental
`rm portfolio.db` or disk failure wipes everything. This job creates an
atomic, transactionally-consistent copy in `backups/portfolio_<date>.db`
using SQLite's online backup API (works while the API server is running).

Idempotent per-day: re-running on the same day overwrites that day's file.
Old backups beyond `--keep` days are NOT auto-pruned — your call.

Run manually:
    python -m portfolio_tracker.jobs.backup
    python -m portfolio_tracker.jobs.backup --keep 90       # auto-prune
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import typer

from portfolio_tracker.config import PROJECT_ROOT, get_settings


def run(keep_days: int | None = None) -> Path:
    """Take a consistent snapshot of the SQLite DB. Returns the backup path."""
    db_path = _resolve_sqlite_path()
    if not db_path.exists():
        raise FileNotFoundError(f"database not found at {db_path}")

    backups_dir = PROJECT_ROOT / "backups"
    backups_dir.mkdir(exist_ok=True)
    target = backups_dir / f"portfolio_{date.today().isoformat()}.db"

    # SQLite's online backup API: copies the DB even while connections are open.
    # Equivalent to `VACUUM INTO`, but works on older SQLite versions too.
    src = sqlite3.connect(str(db_path))
    try:
        dest = sqlite3.connect(str(target))
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()

    if keep_days is not None:
        _prune(backups_dir, keep_days)

    return target


def _resolve_sqlite_path() -> Path:
    url = get_settings().resolved_database_url
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        raise ValueError(f"backup job only supports SQLite (got DATABASE_URL prefix in {url!r})")
    return Path(url[len(prefix) :])


def _prune(backups_dir: Path, keep_days: int) -> None:
    """Delete backup files whose embedded ISO date is older than keep_days."""
    today = date.today()
    for path in sorted(backups_dir.glob("portfolio_*.db")):
        try:
            iso_part = path.stem.removeprefix("portfolio_")
            backup_date = date.fromisoformat(iso_part)
        except ValueError:
            continue
        if (today - backup_date).days > keep_days:
            path.unlink()


def main(
    keep: int | None = typer.Option(None, help="If set, delete backup files older than KEEP days."),
) -> None:
    target = run(keep_days=keep)
    size_mb = target.stat().st_size / (1024 * 1024)
    print(f"backup written: {target}  ({size_mb:.2f} MB)")
    if keep is not None:
        print(f"pruned files older than {keep} days from {target.parent}")


if __name__ == "__main__":
    typer.run(main)
