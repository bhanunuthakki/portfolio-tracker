"""Group cashflow classifications by the RULE that produced them.

The flat transaction list is the wrong unit for auditing "is my contributions
vs. gains tagging right?". On a real book it runs to hundreds of rows, and the
reviewer has to hold the classification logic in their head while scrolling.

But those rows are not hundreds of independent decisions. They are a dozen
rules firing over and over: one name-hint catches every "DEPOSIT Cash in USD",
one subtype rule catches every `cash/contribution`. Audit the rule and you have
audited every row it produced. That collapses ~160 judgements into ~12.

Two things make the collapsed view actually decidable:

* **Why**, stated per group. A classification is only checkable if the reviewer
  can see which rule chose it — an override they set, a word in the
  description, or the (type, subtype) pair. `effective_classification` resolves
  those in precedence order and returns only the answer, so this module
  re-derives the reason alongside it.

* **How much it moves the number.** Groups are ranked by their signed dollar
  effect on the return, not by row count, because those orderings disagree
  sharply. A biweekly $50 transfer is 20 rows and rounding error; a single
  mis-tagged $60k ACATS is one row and points of return. Sorting by count buries
  exactly the row that matters.

Nothing here writes. Fixing a group is the existing bulk-override path, which
already takes a list of transaction ids — so each group carries its ids.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.models import Account, InvestmentTransaction
from portfolio_tracker.schemas import CashflowRuleAuditOut, CashflowRuleGroupOut
from portfolio_tracker.services.active_items import active_account_ids, valued_account_ids
from portfolio_tracker.services.performance import (
    _classify_by_name,  # pyright: ignore[reportPrivateUsage]
    _is_cashflow_eligible_type,  # pyright: ignore[reportPrivateUsage]
    _load_transaction_overrides,  # pyright: ignore[reportPrivateUsage]
    _signed_cashflow,  # pyright: ignore[reportPrivateUsage]
    effective_classification,
)

# Descriptions are shown, never grouped on. The grouping key is the RULE —
# (decision_source, classification, type, subtype) — and nothing else.
#
# An earlier version folded a digit-masked description into the key, on the
# theory that "ACH deposit of $#" and "ACH withdrawal of $#" are different
# rules. They aren't; they are the same name-hint rule reaching opposite
# conclusions, and `classification` already separates those. What the
# description actually varies by is the security: "Cash dividend of $# from
# VTI" and "…from SGOV" split into two groups, then two hundred. On the live
# book that produced 119 groups against 158 rows — a summary the same length as
# the thing it summarises, which is no summary at all. Keying on the rule alone
# gives ~15.
#
# Descriptions still matter for judging a group, so each carries samples and a
# `distinct_patterns` count that reveals when a group is heterogeneous enough to
# deserve a look at the underlying rows.
_NUMERIC_RUN = re.compile(r"\d[\d,]*(?:\.\d+)?")
_WHITESPACE = re.compile(r"\s+")
_PATTERN_MAX_CHARS = 60
_MAX_SAMPLES = 4


def _name_pattern(name: str | None) -> str:
    if not name:
        return ""
    masked = _NUMERIC_RUN.sub("#", name)
    collapsed = _WHITESPACE.sub(" ", masked).strip()
    return collapsed[:_PATTERN_MAX_CHARS]


@dataclass
class _Group:
    source: str
    classification: str
    tx_type: str
    subtype: str | None
    reason: str
    count: int = 0
    net_cashflow: Decimal = Decimal(0)
    gross_amount: Decimal = Decimal(0)
    first_date: date | None = None
    last_date: date | None = None
    accounts: set[str] = field(default_factory=set[str])
    patterns: set[str] = field(default_factory=set[str])
    samples: list[str] = field(default_factory=list[str])
    tx_ids: list[str] = field(default_factory=list[str])
    counts_toward_return: bool = True


def _decision_source(
    tx_type: str,
    subtype: str | None,
    override: str | None,
    name: str | None,
) -> tuple[str, str]:
    """Which rule decided this row, and a sentence explaining it.

    Mirrors the precedence in `_signed_cashflow` exactly. If that order ever
    changes and this doesn't, the audit would explain classifications with a
    rule that didn't actually fire — so the two must be read together.
    """
    if override is not None:
        return "override", "You set this classification manually."
    if _is_cashflow_eligible_type(tx_type):
        hint = _classify_by_name(name)
        if hint is not None:
            return (
                "name",
                "Matched a word in the description — the subtype alone was ambiguous.",
            )
    subtype_norm = (subtype or "").lower().strip()
    if tx_type == "transfer" and subtype_norm == "transfer":
        return (
            "sign",
            "Bare transfer/transfer with no direction word: direction taken from the "
            "amount's sign, which brokers report inconsistently. The least reliable rule.",
        )
    if tx_type == "cash" and subtype_norm == "transfer":
        return (
            "sign",
            "cash/transfer is direction-ambiguous: direction taken from the amount's sign.",
        )
    return "subtype", f"The {tx_type}/{subtype_norm or '—'} subtype maps to this directly."


def build_rule_audit(
    session: Session,
    start_date: date | None = None,
    end_date: date | None = None,
) -> CashflowRuleAuditOut:
    """Every cashflow classification, collapsed to the rules that produced it.

    Covers ALL active accounts, not just the valued ones the return math uses —
    an account excluded for having no holdings still deserves to have its
    tagging checked, and hiding it would make the totals here disagree
    inexplicably with the transactions list. Rows that don't reach the return
    are marked `counts_toward_return=False` instead.
    """
    accts = active_account_ids(session)
    if not accts:
        return CashflowRuleAuditOut(
            start_date=start_date, end_date=end_date, groups=[], net_external_cashflow_in=Decimal(0)
        )
    valued = valued_account_ids(session)
    overrides = _load_transaction_overrides(session)
    account_names: dict[int, str] = {
        account_id: name
        for account_id, name in session.execute(select(Account.account_id, Account.name)).all()
    }

    stmt = select(InvestmentTransaction).where(InvestmentTransaction.account_id.in_(accts))
    if start_date is not None:
        stmt = stmt.where(InvestmentTransaction.date >= start_date)
    if end_date is not None:
        stmt = stmt.where(InvestmentTransaction.date <= end_date)
    txs = session.execute(stmt).scalars().all()

    groups: dict[tuple[str, ...], _Group] = {}
    net_total = Decimal(0)
    for tx in txs:
        override = overrides.get(tx.plaid_investment_transaction_id)
        classification = effective_classification(
            tx.type, tx.subtype, override, amount=Decimal(tx.amount), name=tx.name
        )
        if classification is None:
            # A trade or fee — never cashflow, nothing to audit.
            continue
        source, reason = _decision_source(tx.type, tx.subtype, override, tx.name)
        # Scope is part of the key. A rule firing in a valued account and in one
        # excluded from the return math produces two review items, not one:
        # only the first can move the number, and merging them would leave
        # `counts_toward_return` describing whichever row happened to arrive
        # first while silently misdescribing the rest.
        in_scope = tx.account_id in valued
        key = (source, classification, tx.type, str(tx.subtype), str(in_scope))

        group = groups.get(key)
        if group is None:
            group = _Group(
                source=source,
                classification=classification,
                tx_type=tx.type,
                subtype=tx.subtype,
                reason=reason,
                counts_toward_return=in_scope,
            )
            groups[key] = group

        signed = _signed_cashflow(
            tx.type, tx.subtype, Decimal(tx.amount or 0), override=override, name=tx.name
        )
        group.count += 1
        group.gross_amount += abs(Decimal(tx.amount or 0))
        group.tx_ids.append(tx.plaid_investment_transaction_id)
        group.accounts.add(account_names.get(tx.account_id, f"#{tx.account_id}"))
        if group.first_date is None or tx.date < group.first_date:
            group.first_date = tx.date
        if group.last_date is None or tx.date > group.last_date:
            group.last_date = tx.date
        pattern = _name_pattern(tx.name)
        if pattern:
            group.patterns.add(pattern)
            if len(group.samples) < _MAX_SAMPLES and pattern not in group.samples:
                group.samples.append(pattern)
        # An account outside the valued set contributes nothing to the return,
        # so its rows must not inflate either the group's dollar effect or the
        # grand total — otherwise the audit wouldn't reconcile with the chart.
        if in_scope:
            group.net_cashflow += signed
            net_total += signed

    ordered = sorted(groups.values(), key=lambda g: (-abs(g.net_cashflow), -g.count))
    return CashflowRuleAuditOut(
        start_date=start_date,
        end_date=end_date,
        net_external_cashflow_in=net_total,
        groups=[
            CashflowRuleGroupOut(
                decision_source=g.source,
                classification=g.classification,
                reason=g.reason,
                type=g.tx_type,
                subtype=g.subtype,
                distinct_patterns=len(g.patterns),
                count=g.count,
                net_cashflow=g.net_cashflow,
                gross_amount=g.gross_amount,
                first_date=g.first_date,
                last_date=g.last_date,
                accounts=sorted(g.accounts),
                sample_names=g.samples,
                transaction_ids=g.tx_ids,
                counts_toward_return=g.counts_toward_return,
            )
            for g in ordered
        ],
    )
