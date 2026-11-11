"""Re-check option direction after broker trade history is captured.

Run after the Plaid transaction capture so a position opened today is signed
from today's fills. Idempotent: only positive rows proven short are flipped.

    python -m portfolio_tracker.jobs.reconcile_option_direction
"""

from __future__ import annotations

from datetime import date

from portfolio_tracker.db import SessionLocal
from portfolio_tracker.services.option_direction import apply_inferred_short_signs


def run(snapshot_date: date | None = None) -> int:
    day = snapshot_date or date.today()
    with SessionLocal() as session:
        flipped = apply_inferred_short_signs(session, day)
        session.commit()
    if flipped:
        from portfolio_tracker.jobs import daily_values

        daily_values.run(start_date=day, end_date=day)
    return flipped


if __name__ == "__main__":
    print(f"option direction: {run()} row(s) corrected")
