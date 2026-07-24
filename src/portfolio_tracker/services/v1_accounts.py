"""`GET /api/v1/accounts` — normalized accounts with canonical identity,
detailed Tax treatment, inclusion state, value, and freshness.

The SC-2 canonical-account contract (`docs/design/phase0_decision_addendum.md`):
consumers never deduplicate by comparing balances. Today the tracker models
cross-provider duplication at the Item level (`Item.is_data_active` retires a
redundant aggregator connection), so:

  * an account on an active item is its own canonical representative
    (``canonical_account_id == account_id``, ``included_in_totals=True``);
  * an account on a retired item is excluded (``included_in_totals=False``,
    ``exclusion_reason='operator_excluded'``) with ``canonical_account_id=None``
    and a ``NO_CANONICAL_LINK`` warning — the cross-provider counterpart link
    is not modeled yet, and the contract says so rather than guessing.

Values come from each account's own latest holdings snapshot (sum of
``institution_value``), so an excluded or lagging account still reports the
last thing its provider observed, dated.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from portfolio_tracker.models import Account, HoldingSnapshot, Item
from portfolio_tracker.services.positions_v1 import tax_treatment_detail
from portfolio_tracker.services.v1_common import (
    W_NO_CANONICAL_LINK,
    W_UNKNOWN_TAX_TREATMENT,
    V1AccountCoverage,
    V1Meta,
    V1Warning,
    build_meta,
)

EXCLUSION_OPERATOR: str = "operator_excluded"


class AccountV1(BaseModel):
    """One normalized account with provenance — the SC-1/SC-2 contract."""

    account_id: int
    canonical_account_id: int | None
    provider: str
    institution: str | None
    name: str
    official_name: str | None
    type: str
    subtype: str | None
    mask: str | None
    active: bool
    included_in_totals: bool
    exclusion_reason: str | None
    tax_treatment: str
    tax_treatment_evidence: str | None
    tax_treatment_confidence: str
    # Sum of institution_value over the account's own latest snapshot; None
    # when the account has never had a holdings row.
    value: Decimal | None
    value_currency: str
    holdings_as_of: date | None
    last_successful_sync_at: datetime | None
    warnings: list[V1Warning]


class AccountsV1Result(BaseModel):
    meta: V1Meta
    accounts: list[AccountV1]


def _account_snapshot_values(
    session: Session,
) -> dict[int, tuple[date, Decimal]]:
    """Each account's own latest snapshot date and total value on that date."""
    latest_rows = session.execute(
        select(HoldingSnapshot.account_id, func.max(HoldingSnapshot.snapshot_date)).group_by(
            HoldingSnapshot.account_id
        )
    ).all()
    out: dict[int, tuple[date, Decimal]] = {}
    for row in latest_rows:
        account_id: int = row[0]
        latest: date = row[1]
        total = session.execute(
            select(func.coalesce(func.sum(HoldingSnapshot.institution_value), 0))
            .where(HoldingSnapshot.account_id == account_id)
            .where(HoldingSnapshot.snapshot_date == latest)
        ).scalar_one()
        out[account_id] = (latest, Decimal(str(total if total is not None else 0)))
    return out


def build_accounts_result(
    session: Session,
    *,
    today: date | None = None,
    generated_at: datetime | None = None,
) -> AccountsV1Result:
    rows = session.execute(
        select(Account, Item).join(Item, Item.item_id == Account.item_id).order_by(Account.name)
    ).all()
    values = _account_snapshot_values(session)

    accounts: list[AccountV1] = []
    included: list[int] = []
    excluded: list[int] = []
    lagging: list[int] = []
    providers: set[str] = set()
    global_warnings: list[V1Warning] = []
    last_sync: datetime | None = None
    as_of: date | None = None

    # as_of = latest snapshot date across INCLUDED accounts (matches the
    # holdings/positions consolidation rule).
    for account, item in rows:
        if item.is_data_active and account.account_id in values:
            d = values[account.account_id][0]
            as_of = d if as_of is None or d > as_of else as_of

    for account, item in rows:
        detail = tax_treatment_detail(account.type, account.subtype)
        holdings_as_of, value = values.get(account.account_id, (None, None))
        acct_warnings: list[V1Warning] = []
        if item.is_data_active:
            included.append(account.account_id)
            canonical: int | None = account.account_id
            exclusion_reason = None
            providers.add(item.source)
            if item.last_refreshed_at is not None and (
                last_sync is None or item.last_refreshed_at > last_sync
            ):
                last_sync = item.last_refreshed_at
            if holdings_as_of is not None and as_of is not None and holdings_as_of < as_of:
                lagging.append(account.account_id)
        else:
            excluded.append(account.account_id)
            canonical = None
            exclusion_reason = EXCLUSION_OPERATOR
            acct_warnings.append(
                V1Warning(
                    code=W_NO_CANONICAL_LINK,
                    message=(
                        "Excluded account has no modeled cross-provider counterpart "
                        "link; do not re-add its value to totals."
                    ),
                    scope=f"account:{account.account_id}",
                )
            )
        if detail.treatment == "unknown":
            warning = V1Warning(
                code=W_UNKNOWN_TAX_TREATMENT,
                message=(
                    "Tax treatment could not be inferred from type/subtype; "
                    "consumers must not silently classify this account."
                ),
                scope=f"account:{account.account_id}",
            )
            acct_warnings.append(warning)
            if item.is_data_active:
                global_warnings.append(warning)
        accounts.append(
            AccountV1(
                account_id=account.account_id,
                canonical_account_id=canonical,
                provider=item.source,
                institution=item.institution_name,
                name=account.name,
                official_name=account.official_name,
                type=account.type,
                subtype=account.subtype,
                mask=account.mask,
                active=item.is_data_active,
                included_in_totals=item.is_data_active,
                exclusion_reason=exclusion_reason,
                tax_treatment=detail.treatment,
                tax_treatment_evidence=detail.evidence,
                tax_treatment_confidence=detail.confidence,
                value=value,
                value_currency=account.currency,
                holdings_as_of=holdings_as_of,
                last_successful_sync_at=item.last_refreshed_at,
                warnings=acct_warnings,
            )
        )

    coverage = V1AccountCoverage(
        included_account_ids=sorted(included),
        excluded_account_ids=sorted(excluded),
        lagging_account_ids=sorted(lagging),
    )
    meta = build_meta(
        as_of=as_of,
        source_providers=sorted(providers),
        coverage=coverage,
        last_successful_sync_at=last_sync,
        warnings=global_warnings,
        links={
            "portfolio_snapshot": "/api/v1/portfolio-snapshot",
            "positions": "/api/v1/portfolio/positions",
            "positioning": "/api/v1/analytics/positioning",
        },
        today=today,
        generated_at=generated_at,
    )
    return AccountsV1Result(meta=meta, accounts=accounts)
