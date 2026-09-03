"""Canonical external-cashflow projection for whole-portfolio returns.

The persisted authorities remain the normalized transaction, owner override,
historical price, and holding-snapshot tables.  This module is a derived,
read-only ledger over those authorities; it deliberately adds no second store.

Both Modified Dietz performance and the explanatory ``/api/v1/cash-flows``
resource consume this projection so classification, synthesized share-transfer
valuations, account scope, and window totals cannot drift between them.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.models import (
    Account,
    InvestmentTransaction,
    InvestmentTransactionType,
    Item,
    Price,
    Security,
    TransactionOverride,
)
from portfolio_tracker.services.active_items import valued_account_ids
from portfolio_tracker.services.splits import load_split_factors

FlowClassification = Literal["external_in", "external_out", "internal"]
ClassificationSource = Literal["override", "heuristic", "derived_share_transfer_net"]
FlowSourceKind = Literal["transaction", "share_transfer_valuation"]
ValuationPriceSource = Literal["historical_close"]

_INTERNAL_TRANSFER_SUBTYPES: frozenset[str] = frozenset(
    {"assignment", "exercise", "merger", "spin off", "split", "stock distribution"}
)
_INFLOW_CASH_SUBTYPES: frozenset[str] = frozenset(
    {"deposit", "contribution", "rollover", "wire", "ach"}
)
_OUTFLOW_CASH_SUBTYPES: frozenset[str] = frozenset({"withdrawal"})
_AMBIGUOUS_CASH_SUBTYPES: frozenset[str] = frozenset({"transfer"})
_SHARE_TRANSFER_SUBTYPES: frozenset[str] = frozenset(
    {"external_asset_transfer_in", "external_asset_transfer_out"}
)


@dataclass(frozen=True)
class ClassificationDecision:
    classification: FlowClassification
    signed_external_amount: Decimal
    source: Literal["override", "heuristic"]
    rule: str


@dataclass(frozen=True)
class ExternalFlowEntry:
    """One explainable event in the derived whole-portfolio flow ledger."""

    flow_id: str
    date: date
    source_kind: FlowSourceKind
    source_provider: str
    signed_external_amount: Decimal
    classification: FlowClassification
    classification_source: ClassificationSource
    classification_rule: str
    currency: str
    transaction_id: str | None = None
    component_transaction_ids: tuple[str, ...] = ()
    account_id: int | None = None
    account_ids: tuple[int, ...] = ()
    account_name: str | None = None
    security_id: int | None = None
    security_ids: tuple[int, ...] = ()
    ticker: str | None = None
    name: str | None = None
    type: str = "cash"
    subtype: str | None = None
    amount: Decimal = Decimal(0)
    valuation_price: Decimal | None = None
    valuation_price_date: date | None = None
    valuation_price_source: ValuationPriceSource | None = None


@dataclass(frozen=True)
class ExternalFlowIssue:
    code: Literal[
        "share_transfer_missing_security",
        "share_transfer_missing_ticker",
        "share_transfer_price_unavailable",
    ]
    date: date
    security_key: str
    component_transaction_ids: tuple[str, ...]


class IncompleteExternalFlowLedgerError(RuntimeError):
    def __init__(self, issues: tuple[ExternalFlowIssue, ...]) -> None:
        self.issues = issues
        codes = ", ".join(sorted({issue.code for issue in issues}))
        super().__init__(f"external-flow ledger is incomplete: {codes}")


@dataclass(frozen=True)
class ExternalFlowLedger:
    start_date: date
    end_date: date
    account_ids: frozenset[int]
    entries: tuple[ExternalFlowEntry, ...]
    issues: tuple[ExternalFlowIssue, ...] = ()

    def require_complete(self) -> None:
        if self.issues:
            raise IncompleteExternalFlowLedgerError(self.issues)

    @property
    def net_external_cashflow_in(self) -> Decimal:
        self.require_complete()
        return sum(
            (
                entry.signed_external_amount
                for entry in self.entries
                if entry.classification != "internal"
            ),
            Decimal(0),
        )

    @property
    def daily_external_cashflows(self) -> dict[date, Decimal]:
        self.require_complete()
        totals: dict[date, Decimal] = defaultdict(lambda: Decimal(0))
        for entry in self.entries:
            if entry.classification != "internal" and entry.signed_external_amount != 0:
                totals[entry.date] += entry.signed_external_amount
        return dict(totals)


def _name_classification(name: str | None) -> tuple[FlowClassification, str] | None:
    if not name:
        return None
    normalized = name.lower()
    if "reinvestment" in normalized or "drip" in normalized:
        return "internal", "name.reinvestment"
    # Owner-approved rule: promotional consideration arrived from outside the
    # book and must not be credited as investment performance. Keep this ahead
    # of portfolio income because brokers may call a bonus an interest payment.
    if "bonus" in normalized or "boost payment" in normalized or "reimbursement" in normalized:
        return "external_in", "name.owner_approved_bonus"
    if "promo" in normalized or "reward" in normalized or "incentive" in normalized:
        return "external_in", "name.owner_approved_bonus"
    if (
        "dividend" in normalized
        or "interest payment" in normalized
        or "credit interest" in normalized
    ):
        return "internal", "name.portfolio_income"
    if "outgoing" in normalized or "withdrawal" in normalized:
        return "external_out", "name.outflow"
    if "incoming" in normalized or "deposit" in normalized:
        return "external_in", "name.inflow"
    return None


def classify_by_name(name: str | None) -> str | None:
    """Return only the classification selected by a transaction-name rule."""
    decision = _name_classification(name)
    return decision[0] if decision is not None else None


def _is_cashflow_eligible_type(tx_type: str) -> bool:
    return tx_type in (
        InvestmentTransactionType.CASH.value,
        InvestmentTransactionType.TRANSFER.value,
    )


def classify_transaction_cashflow(
    tx_type: str,
    tx_subtype: str | None,
    amount: Decimal,
    *,
    override: str | None = None,
    name: str | None = None,
) -> ClassificationDecision | None:
    """Classify one normalized transaction with explicit decision lineage."""
    if override is not None:
        signed = {
            "internal": Decimal(0),
            "external_in": abs(amount),
            "external_out": -abs(amount),
        }.get(override)
        if signed is None:
            return None
        return ClassificationDecision(
            classification=cast(FlowClassification, override),
            signed_external_amount=signed,
            source="override",
            rule=f"transaction_override.{override}",
        )

    if not _is_cashflow_eligible_type(tx_type):
        return None

    name_decision = _name_classification(name)
    if name_decision is not None:
        classification, rule = name_decision
        signed = Decimal(0)
        if classification == "external_in":
            signed = abs(amount)
        elif classification == "external_out":
            signed = -abs(amount)
        return ClassificationDecision(classification, signed, "heuristic", rule)

    subtype = (tx_subtype or "").lower().strip()
    if tx_type == InvestmentTransactionType.TRANSFER.value:
        if subtype in _INTERNAL_TRANSFER_SUBTYPES:
            return ClassificationDecision(
                "internal", Decimal(0), "heuristic", f"subtype.transfer.{subtype}"
            )
        return ClassificationDecision(
            "external_in" if -amount > 0 else "external_out",
            -amount,
            "heuristic",
            "subtype.transfer.plaid_sign",
        )

    if tx_type == InvestmentTransactionType.CASH.value:
        if subtype in _INFLOW_CASH_SUBTYPES:
            return ClassificationDecision(
                "external_in", abs(amount), "heuristic", f"subtype.cash.{subtype}"
            )
        if subtype in _OUTFLOW_CASH_SUBTYPES:
            return ClassificationDecision(
                "external_out", -abs(amount), "heuristic", f"subtype.cash.{subtype}"
            )
        if subtype in _AMBIGUOUS_CASH_SUBTYPES:
            return ClassificationDecision(
                "external_in" if -amount > 0 else "external_out",
                -amount,
                "heuristic",
                "subtype.cash.transfer.plaid_sign",
            )
        if subtype in _SHARE_TRANSFER_SUBTYPES:
            # The raw rows carry quantities but normally no dollars. Their net
            # dollar effect is represented once by the synthesized ledger row.
            return None
    return None


def signed_cashflow(
    tx_type: str,
    tx_subtype: str | None,
    amount: Decimal,
    override: str | None = None,
    name: str | None = None,
) -> Decimal:
    decision = classify_transaction_cashflow(
        tx_type, tx_subtype, amount, override=override, name=name
    )
    return decision.signed_external_amount if decision is not None else Decimal(0)


def is_external_cashflow(
    tx_type: str,
    tx_subtype: str | None,
    override: str | None = None,
    name: str | None = None,
) -> bool:
    decision = classify_transaction_cashflow(
        tx_type, tx_subtype, Decimal(1), override=override, name=name
    )
    return decision is not None and decision.classification != "internal"


def effective_classification(
    tx_type: str,
    tx_subtype: str | None,
    override: str | None,
    amount: Decimal | None = None,
    name: str | None = None,
) -> str | None:
    decision = classify_transaction_cashflow(
        tx_type,
        tx_subtype,
        amount if amount is not None else Decimal(1),
        override=override,
        name=name,
    )
    return decision.classification if decision is not None else None


def load_transaction_overrides(session: Session) -> dict[str, str]:
    rows = session.execute(
        select(
            TransactionOverride.plaid_investment_transaction_id,
            TransactionOverride.classification,
        )
    ).all()
    return {tx_id: classification for tx_id, classification in rows}


@dataclass(frozen=True)
class _ShareTransferComponent:
    transaction_id: str
    date: date
    account_id: int
    security_id: int
    ticker: str | None
    quantity: Decimal
    subtype: str
    provider: str
    override: str | None
    name: str | None
    type: str


def _is_share_transfer_candidate(transaction: InvestmentTransaction) -> bool:
    subtype = (transaction.subtype or "").lower().strip()
    if (
        transaction.type == InvestmentTransactionType.CASH.value
        and subtype in _SHARE_TRANSFER_SUBTYPES
    ):
        return transaction.quantity != 0
    return (
        transaction.type == InvestmentTransactionType.TRANSFER.value
        and Decimal(transaction.amount or 0) == 0
        and transaction.quantity != 0
    )


def build_external_flow_ledger(
    session: Session,
    start_date: date,
    end_date: date,
    *,
    account_ids: frozenset[int] | None = None,
) -> ExternalFlowLedger:
    """Build the canonical read-only flow projection for one closed window."""
    accounts = account_ids if account_ids is not None else valued_account_ids(session)
    if not accounts:
        return ExternalFlowLedger(start_date, end_date, frozenset(), ())

    rows = session.execute(
        select(InvestmentTransaction, Account, Item, Security)
        .join(Account, Account.account_id == InvestmentTransaction.account_id)
        .join(Item, Item.item_id == Account.item_id)
        .join(Security, Security.security_id == InvestmentTransaction.security_id, isouter=True)
        # Portfolio values are end-of-day observations. A flow on the opening
        # date is already inside V_start, so the canonical return window is
        # (start_date, end_date], not [start_date, end_date].
        .where(InvestmentTransaction.date > start_date)
        .where(InvestmentTransaction.date <= end_date)
        .where(InvestmentTransaction.account_id.in_(accounts))
    ).all()
    overrides = load_transaction_overrides(session)
    entries: list[ExternalFlowEntry] = []
    transfer_components: list[_ShareTransferComponent] = []
    issues: list[ExternalFlowIssue] = []

    for transaction, account, item, security in rows:
        transaction_id = transaction.plaid_investment_transaction_id
        subtype = (transaction.subtype or "").lower().strip()
        override = overrides.get(transaction_id)
        if _is_share_transfer_candidate(transaction):
            component_decision = classify_transaction_cashflow(
                transaction.type,
                transaction.subtype,
                Decimal(0),
                override=override,
                name=transaction.name,
            )
            if component_decision is not None and component_decision.classification == "internal":
                continue
            if transaction.security_id is None:
                issues.append(
                    ExternalFlowIssue(
                        code="share_transfer_missing_security",
                        date=transaction.date,
                        security_key="missing",
                        component_transaction_ids=(transaction_id,),
                    )
                )
                continue
            if security is None or security.ticker is None or not security.ticker.strip():
                issues.append(
                    ExternalFlowIssue(
                        code="share_transfer_missing_ticker",
                        date=transaction.date,
                        security_key=f"security:{transaction.security_id}",
                        component_transaction_ids=(transaction_id,),
                    )
                )
                continue
            assert transaction.security_id is not None
            transfer_components.append(
                _ShareTransferComponent(
                    transaction_id=transaction_id,
                    date=transaction.date,
                    account_id=transaction.account_id,
                    security_id=transaction.security_id,
                    ticker=security.ticker if security is not None else None,
                    quantity=Decimal(transaction.quantity or 0),
                    subtype=subtype,
                    provider=item.source,
                    override=override,
                    name=transaction.name,
                    type=transaction.type,
                )
            )
            continue

        amount = Decimal(transaction.amount or 0)
        decision = classify_transaction_cashflow(
            transaction.type,
            transaction.subtype,
            amount,
            override=override,
            name=transaction.name,
        )
        if decision is None:
            continue
        entries.append(
            ExternalFlowEntry(
                flow_id=f"transaction:{transaction_id}",
                date=transaction.date,
                source_kind="transaction",
                source_provider=item.source,
                signed_external_amount=decision.signed_external_amount,
                classification=decision.classification,
                classification_source=decision.source,
                classification_rule=decision.rule,
                currency=transaction.currency,
                transaction_id=transaction_id,
                account_id=account.account_id,
                account_ids=(account.account_id,),
                account_name=account.name,
                security_id=security.security_id if security is not None else None,
                ticker=security.ticker if security is not None else None,
                name=transaction.name,
                type=transaction.type,
                subtype=transaction.subtype,
                amount=amount,
            )
        )

    synthesized, synthesized_issues = _synthesized_share_transfer_entries(
        session, transfer_components, end_date
    )
    entries.extend(synthesized)
    issues.extend(synthesized_issues)
    entries.sort(key=lambda entry: (entry.date, entry.flow_id), reverse=True)
    return ExternalFlowLedger(
        start_date, end_date, frozenset(accounts), tuple(entries), tuple(issues)
    )


def _synthesized_share_transfer_entries(
    session: Session,
    components: list[_ShareTransferComponent],
    end_date: date,
) -> tuple[list[ExternalFlowEntry], list[ExternalFlowIssue]]:
    grouped: dict[tuple[date, str], list[_ShareTransferComponent]] = defaultdict(list)
    for component in components:
        security_key = (
            component.ticker.strip().upper()
            if component.ticker and component.ticker.strip()
            else f"security:{component.security_id}"
        )
        grouped[(component.date, security_key)].append(component)
    if not grouped:
        return [], []

    security_ids = frozenset(
        component.security_id for group in grouped.values() for component in group
    )
    earliest = min(flow_date for flow_date, _ in grouped) - timedelta(days=14)
    price_rows = session.execute(
        select(Price.security_id, Price.date, Price.close)
        .where(Price.security_id.in_(security_ids))
        .where(Price.position_price_trade_eligibility_clause())
        .where(Price.close > 0)
        .where(Price.date >= earliest)
        .where(Price.date <= end_date)
    ).all()
    prices: dict[int, dict[date, Decimal]] = defaultdict(dict)
    for security_id, price_date, close in price_rows:
        prices[security_id][price_date] = Decimal(close)

    split_factors = load_split_factors(session, security_ids)
    out: list[ExternalFlowEntry] = []
    issues: list[ExternalFlowIssue] = []
    for (flow_date, security_key), group in grouped.items():
        net_quantity = Decimal(0)
        owner_override_applied = False
        for component in group:
            if component.override == "internal":
                owner_override_applied = True
                continue
            amount_decision = classify_transaction_cashflow(
                component.type,
                component.subtype,
                Decimal(0),
                override=component.override,
                name=component.name,
            )
            quantity = abs(component.quantity) * split_factors.factor_after(
                component.security_id, flow_date
            )
            if component.override == "external_in" or (
                amount_decision is not None
                and amount_decision.classification == "external_in"
                and amount_decision.rule.startswith("name.")
            ):
                owner_override_applied = owner_override_applied or component.override is not None
                net_quantity += quantity
            elif component.override == "external_out" or (
                amount_decision is not None
                and amount_decision.classification == "external_out"
                and amount_decision.rule.startswith("name.")
            ):
                owner_override_applied = owner_override_applied or component.override is not None
                net_quantity -= quantity
            elif amount_decision is not None and amount_decision.classification == "internal":
                continue
            elif component.subtype == "external_asset_transfer_in":
                net_quantity += quantity
            elif component.subtype == "external_asset_transfer_out":
                net_quantity -= quantity
            else:
                net_quantity += component.quantity * split_factors.factor_after(
                    component.security_id, flow_date
                )
        if net_quantity == 0:
            continue

        price_candidates: list[tuple[date, int, Decimal]] = []
        for security_id in {component.security_id for component in group}:
            available_prices = prices.get(security_id, {})
            usable_dates = [
                price_date
                for price_date in available_prices
                if flow_date - timedelta(days=14) <= price_date <= flow_date
            ]
            if usable_dates:
                price_date = max(usable_dates)
                price_candidates.append((price_date, security_id, available_prices[price_date]))
        if not price_candidates:
            component_ids = tuple(sorted(component.transaction_id for component in group))
            issues.append(
                ExternalFlowIssue(
                    code="share_transfer_price_unavailable",
                    date=flow_date,
                    security_key=security_key,
                    component_transaction_ids=component_ids,
                )
            )
            continue
        valuation_date, valuation_security_id, valuation_price = max(price_candidates)
        valuation_source: ValuationPriceSource = "historical_close"

        signed_amount = net_quantity * valuation_price
        classification: FlowClassification = "external_in" if signed_amount > 0 else "external_out"
        transaction_ids = tuple(sorted(component.transaction_id for component in group))
        account_ids = tuple(sorted({component.account_id for component in group}))
        providers = sorted({component.provider for component in group})
        ticker = next((component.ticker for component in group if component.ticker), None)
        grouped_security_ids = tuple(sorted({component.security_id for component in group}))
        out.append(
            ExternalFlowEntry(
                flow_id=f"share-transfer:{flow_date.isoformat()}:{security_key}",
                date=flow_date,
                source_kind="share_transfer_valuation",
                source_provider=providers[0] if len(providers) == 1 else "multiple",
                signed_external_amount=signed_amount,
                classification=classification,
                classification_source=(
                    "override" if owner_override_applied else "derived_share_transfer_net"
                ),
                classification_rule="share_transfer.net_quantity_valued_at_available_price",
                currency="USD",
                component_transaction_ids=transaction_ids,
                account_ids=account_ids,
                security_id=valuation_security_id,
                security_ids=grouped_security_ids,
                ticker=ticker,
                name=f"Unmatched share transfer{f' ({ticker})' if ticker else ''}",
                type="derived",
                subtype="unmatched_share_transfer",
                valuation_price=valuation_price,
                valuation_price_date=valuation_date,
                valuation_price_source=valuation_source,
            )
        )
    return out, issues
