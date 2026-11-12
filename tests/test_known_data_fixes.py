"""Regression tests for the known provider-data defects fixed in code.

Each test reproduces one observed live pattern with synthetic rows:

* duplicate ``transfer`` and ``fee`` rows next to an explicit ACATS row;
* a transfer described exactly as "contribution";
* provider-unresolved decisions with one provable economic reading;
* an owner-ruled zero-value flow-through account;
* two distinct accounts at one broker connected through two aggregators.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.models import (
    Account,
    CashFlowReconciliationDecision,
    CashFlowSourceAttestation,
    CashFlowSourceEvent,
    HoldingSnapshot,
    InvestmentTransaction,
    Item,
    Security,
)
from portfolio_tracker.plaid_client import PlaidInvestmentTransaction
from portfolio_tracker.provider_delivery import build_provider_delivery_metadata
from portfolio_tracker.services import external_flow_ledger, owner_rulings
from portfolio_tracker.services import performance as performance_service
from portfolio_tracker.services.cashflow_source_coverage import assess_cashflow_source_coverage
from portfolio_tracker.services.data_quality import (
    OVERLAPPING_BROKER_CONNECTIONS,
    OWNER_RULING_CONTRADICTED,
    _find_contradicted_owner_rulings,  # pyright: ignore[reportPrivateUsage]
    _find_overlapping_broker_connections,  # pyright: ignore[reportPrivateUsage]
)
from portfolio_tracker.services.external_flow_ledger import (
    ExternalFlowEntry,
    _rule_resolution_mismatches,  # pyright: ignore[reportPrivateUsage]
    build_external_flow_ledger,
    classify_transaction_cashflow,
    superseded_share_transfer_ids,
)
from portfolio_tracker.services.owner_rulings import (
    ZeroValueFlowThroughRuling,
    zero_value_account_ids,
    zero_value_ruling_contradictions,
)
from portfolio_tracker.services.provider_decision_rules import RuleResolution, resolve
from portfolio_tracker.services.provider_transaction_provenance import (
    ProviderAccountTransactionCapture,
    persist_provider_account_attestation,
)

START = date(2026, 5, 1)
END = date(2026, 5, 31)
ACATS_DAY = date(2026, 5, 13)
CAPTURED = datetime(2026, 6, 1, tzinfo=UTC)


def _account(
    session: Session,
    suffix: str,
    *,
    institution: str | None = None,
    source: str = "plaid",
    mask: str | None = None,
) -> Account:
    item = Item(
        source=source,
        plaid_item_id=f"item-{suffix}",
        institution_name=institution or f"Broker {suffix}",
        is_data_active=True,
    )
    session.add(item)
    session.flush()
    account = Account(
        item_id=item.item_id,
        plaid_account_id=f"account-{suffix}",
        name=f"Account {suffix}",
        type="investment",
        mask=mask,
    )
    session.add(account)
    session.flush()
    return account


def _security(session: Session, ticker: str) -> Security:
    security = Security(plaid_security_id=f"security-{ticker}", ticker=ticker, type="etf")
    session.add(security)
    session.flush()
    return security


def _snapshot(
    session: Session, account: Account, security: Security, day: date, value: str = "1000"
) -> None:
    session.add(
        HoldingSnapshot(
            snapshot_date=day,
            account_id=account.account_id,
            security_id=security.security_id,
            quantity=Decimal(10),
            institution_value=Decimal(value),
        )
    )


def _row(
    tx_id: str,
    account: Account,
    *,
    type_: str,
    subtype: str | None,
    on: date = ACATS_DAY,
    quantity: str = "0",
    amount: str = "0",
    security: Security | None = None,
    name: str | None = None,
) -> InvestmentTransaction:
    return InvestmentTransaction(
        plaid_investment_transaction_id=tx_id,
        account_id=account.account_id,
        security_id=security.security_id if security is not None else None,
        date=on,
        name=name,
        quantity=Decimal(quantity),
        amount=Decimal(amount),
        type=type_,
        subtype=subtype,
        currency="USD",
    )


# --- Issue 1: duplicate transfer and fee rows next to an ACATS row ------------


def test_superseded_rows_are_exact_duplicates_of_an_acats_row(session: Session):
    sender = _account(session, "sender")
    receiver = _account(session, "receiver")
    vti = _security(session, "VTI")
    rows = [
        _row(
            "acats-out",
            sender,
            type_="cash",
            subtype="external_asset_transfer_out",
            quantity="-10",
            security=vti,
        ),
        _row(
            "dup-transfer",
            sender,
            type_="transfer",
            subtype="transfer",
            quantity="-10",
            security=vti,
            name="Completed outgoing transfer",
        ),
        _row(
            "acats-in",
            receiver,
            type_="cash",
            subtype="external_asset_transfer_in",
            quantity="10",
            security=vti,
        ),
        _row(
            "dup-fee",
            receiver,
            type_="fee",
            subtype="fee",
            on=date(2026, 5, 16),
            quantity="10",
            amount="3000",
            security=vti,
            name="fee - TRANSFER IN",
        ),
        # Not duplicates: a different share count, a date outside five days,
        # and a transfer on another day.
        _row(
            "fee-other-qty",
            receiver,
            type_="fee",
            subtype="fee",
            quantity="3",
            amount="9",
            security=vti,
        ),
        _row(
            "fee-far",
            receiver,
            type_="fee",
            subtype="fee",
            on=date(2026, 5, 25),
            quantity="10",
            amount="9",
            security=vti,
        ),
        _row(
            "transfer-other-day",
            sender,
            type_="transfer",
            subtype="transfer",
            on=date(2026, 5, 14),
            quantity="-10",
            security=vti,
        ),
    ]

    assert superseded_share_transfer_ids(rows) == frozenset({"dup-transfer", "dup-fee"})
    assert superseded_share_transfer_ids([r for r in rows if r.type != "cash"]) == frozenset()


def test_performance_uses_the_ledger_supersession_rule():
    assert (
        performance_service.superseded_share_transfer_ids
        is external_flow_ledger.superseded_share_transfer_ids
    )


def test_duplicate_outgoing_transfer_no_longer_leaves_an_external_outflow(session: Session):
    sender = _account(session, "sender")
    receiver = _account(session, "receiver")
    vti = _security(session, "VTI")
    session.add_all(
        [
            _row(
                "acats-out",
                sender,
                type_="cash",
                subtype="external_asset_transfer_out",
                quantity="-10",
                security=vti,
            ),
            _row(
                "dup-transfer",
                sender,
                type_="transfer",
                subtype="transfer",
                quantity="-10",
                security=vti,
                name="Completed outgoing transfer",
            ),
            _row(
                "acats-in",
                receiver,
                type_="cash",
                subtype="external_asset_transfer_in",
                quantity="10",
                security=vti,
            ),
            _row(
                "dup-fee",
                receiver,
                type_="fee",
                subtype="fee",
                on=date(2026, 5, 15),
                quantity="10",
                amount="3000",
                security=vti,
                name="fee - TRANSFER IN",
            ),
        ]
    )
    session.commit()

    ledger = build_external_flow_ledger(
        session, START, END, account_ids=frozenset({sender.account_id, receiver.account_id})
    )

    assert ledger.issues == ()
    assert [e for e in ledger.entries if e.classification != "internal"] == []


# --- Issue 4a: a transfer described as "contribution" -------------------------


def test_transfer_named_contribution_is_an_external_inflow():
    decision = classify_transaction_cashflow(
        "transfer", "transfer", Decimal(500), name="Contribution"
    )
    assert decision is not None
    assert decision.classification == "external_in"
    assert decision.signed_external_amount == Decimal(500)
    assert decision.rule == "name.transfer_contribution"

    other = classify_transaction_cashflow(
        "transfer", "transfer", Decimal(500), name="Contribution reversal"
    )
    assert other is not None and other.rule == "subtype.transfer.plaid_sign"


# --- Issue 4: provider-unresolved decisions with one provable reading ---------


def _capture(
    account: Account,
    transactions: tuple[PlaidInvestmentTransaction, ...],
    securities: dict[str, int] | None = None,
) -> ProviderAccountTransactionCapture:
    delivery = build_provider_delivery_metadata(
        provider="plaid",
        source_format="plaid_investment_transactions_api",
        parser_version="plaid_investment_tx.v1",
        requested_start_date=START,
        requested_end_date=END,
        page_count=1,
        provider_reported_total=len(transactions),
        record_ids=[tx.plaid_investment_transaction_id for tx in transactions],
        normalized_records=[tx.model_dump(mode="json") for tx in transactions],
    )
    return ProviderAccountTransactionCapture(
        account_id=account.account_id,
        provider_account_id=account.plaid_account_id,
        coverage_start=START,
        coverage_end=END,
        delivery=delivery,
        transactions=transactions,
        security_ids_by_provider_id=securities or {},
        captured_at=CAPTURED,
        provider_history_gaps=(),
        broker_archive_coverage_basis="provider_explicit_full_range",
    )


def _deliver(
    session: Session,
    account: Account,
    tx_id: str,
    *,
    type_: str,
    subtype: str | None,
    amount: str = "0",
    quantity: str = "0",
    name: str = "Provider activity",
    security: Security | None = None,
) -> None:
    """Store one provider row and record its provenance as sync does."""
    source = PlaidInvestmentTransaction(
        plaid_investment_transaction_id=tx_id,
        plaid_account_id=account.plaid_account_id,
        plaid_security_id=security.plaid_security_id if security is not None else None,
        date=ACATS_DAY,
        name=name,
        quantity=Decimal(quantity),
        amount=Decimal(amount),
        type=type_,
        subtype=subtype,
        currency="USD",
    )
    session.add(
        _row(
            tx_id,
            account,
            type_=type_,
            subtype=subtype,
            quantity=quantity,
            amount=amount,
            security=security,
            name=name,
        )
    )
    session.flush()
    securities = (
        {security.plaid_security_id: security.security_id} if security is not None else None
    )
    persist_provider_account_attestation(session, _capture(account, (source,), securities))
    session.flush()


def _assumption(session: Session) -> list[str | None]:
    return [
        decision.assumption_code
        for decision in session.scalars(select(CashFlowReconciliationDecision))
        if decision.resolution_kind == "unresolved"
    ]


def test_contribution_rule_resolves_provider_gap_and_ledger_books_the_inflow(
    session: Session,
):
    account = _account(session, "feeder")
    _deliver(
        session,
        account,
        "contrib-1",
        type_="transfer",
        subtype="transfer",
        amount="500",
        name="Contribution",
    )
    assert _assumption(session) == ["provider_transfer_classification_unresolved"]
    session.commit()
    universe = frozenset({account.account_id})

    ledger = build_external_flow_ledger(session, START, END, account_ids=universe)

    assert ledger.issues == ()
    assert [(r.rule, r.classification) for r in ledger.rule_resolutions] == [
        ("contribution_inflow", "external_in")
    ]
    assert [(e.transaction_id, e.signed_external_amount) for e in ledger.entries] == [
        ("contrib-1", Decimal(500))
    ]
    coverage = assess_cashflow_source_coverage(session, START, END, account_ids=universe)
    assert coverage.accounts[0].uncovered_ranges == ()


def test_zero_economics_rule_closes_an_empty_provider_row(session: Session):
    account = _account(session, "empty")
    _deliver(session, account, "empty-1", type_="cash", subtype="deposit")
    assert _assumption(session) == ["provider_external_cash_amount_zero"]
    session.commit()

    ledger = build_external_flow_ledger(
        session, START, END, account_ids=frozenset({account.account_id})
    )

    assert ledger.issues == ()
    assert [r.rule for r in ledger.rule_resolutions] == ["zero_economics"]
    assert all(entry.signed_external_amount == 0 for entry in ledger.entries)


def test_paired_in_kind_rule_requires_the_opposite_leg_inside_the_universe(
    session: Session,
):
    sender = _account(session, "sender")
    receiver = _account(session, "receiver")
    vti = _security(session, "VTI")
    _deliver(
        session,
        sender,
        "acats-out",
        type_="cash",
        subtype="external_asset_transfer_out",
        quantity="-10",
        security=vti,
    )
    _deliver(
        session,
        receiver,
        "acats-in",
        type_="cash",
        subtype="external_asset_transfer_in",
        quantity="10",
        security=vti,
    )
    assert _assumption(session) == ["provider_in_kind_requires_reconciliation"] * 2
    session.commit()

    both = build_external_flow_ledger(
        session, START, END, account_ids=frozenset({sender.account_id, receiver.account_id})
    )
    assert both.issues == ()
    assert sorted(r.record_transaction_id for r in both.rule_resolutions) == [
        "acats-in",
        "acats-out",
    ]
    assert {r.rule for r in both.rule_resolutions} == {"paired_in_kind_transfer"}
    coverage = assess_cashflow_source_coverage(
        session, START, END, account_ids=frozenset({sender.account_id, receiver.account_id})
    )
    assert [account.uncovered_ranges for account in coverage.accounts] == [(), ()]

    # Without the receiving account, the pair is unproven: fail closed.
    alone = build_external_flow_ledger(
        session, START, END, account_ids=frozenset({sender.account_id})
    )
    assert alone.rule_resolutions == ()
    assert "provenance_current_decision_unresolved" in {i.code for i in alone.issues}


def test_rule_never_overrides_a_non_provider_unresolved_decision(session: Session):
    account = _account(session, "feeder")
    _deliver(
        session,
        account,
        "contrib-1",
        type_="transfer",
        subtype="transfer",
        amount="500",
        name="Contribution",
    )
    event = session.scalars(select(CashFlowSourceEvent)).one()
    attestation = session.scalars(select(CashFlowSourceAttestation)).one()
    decision = session.scalars(select(CashFlowReconciliationDecision)).one()
    universe = frozenset({account.account_id})
    assert resolve(session, event, attestation, decision, universe) is not None

    with session.no_autoflush:
        decision.decision_authority = "owner"
        assert resolve(session, event, attestation, decision, universe) is None
    session.rollback()


def _entry(transaction_id: str, classification: str) -> ExternalFlowEntry:
    return ExternalFlowEntry(
        flow_id=f"flow:{transaction_id}",
        date=ACATS_DAY,
        source_kind="investment_transaction",
        source_provider="plaid",
        signed_external_amount=Decimal(500),
        classification=classification,  # type: ignore[arg-type]
        classification_source="heuristic",
        classification_rule="test",
        currency="USD",
        account_ids=(1,),
        transaction_id=transaction_id,
    )


def test_ledger_fails_closed_when_it_disagrees_with_a_rule(session: Session):
    account = _account(session, "a")
    record = _row("rec-1", account, type_="transfer", subtype="transfer", amount="500")
    inflow = RuleResolution("e" * 64, "contribution_inflow", "external_in", "rec-1", ACATS_DAY)
    internal = RuleResolution("f" * 64, "zero_economics", "internal", "rec-1", ACATS_DAY)

    assert _rule_resolution_mismatches((inflow,), [_entry("rec-1", "external_in")], [record]) == []
    missing = _rule_resolution_mismatches((inflow,), [], [record])
    wrong = _rule_resolution_mismatches((inflow,), [_entry("rec-1", "external_out")], [record])
    leaked = _rule_resolution_mismatches((internal,), [_entry("rec-1", "external_in")], [record])

    for issues in (missing, wrong, leaked):
        assert [issue.code for issue in issues] == ["provenance_rule_resolution_mismatch"]


# --- Issues 2 and 4b: owner-ruled zero-value flow-through account -------------


def test_zero_value_ruling_verifies_identity_and_holdings(session: Session):
    feeder = _account(session, "feeder", institution="Fidelity")
    other = _account(session, "other", institution="Schwab")
    vti = _security(session, "VTI")
    session.commit()

    def ruling(account_id: int) -> ZeroValueFlowThroughRuling:
        return ZeroValueFlowThroughRuling(account_id, "Fidelity", date(2026, 11, 12))

    rulings = (ruling(feeder.account_id), ruling(other.account_id), ruling(9999))
    assert zero_value_ruling_contradictions(session, START, END, rulings) == {
        other.account_id: "institution_mismatch",
        9999: "account_missing",
    }
    assert zero_value_account_ids(session, START, END, rulings) == frozenset({feeder.account_id})

    _snapshot(session, feeder, vti, date(2026, 5, 20))
    session.commit()
    assert zero_value_ruling_contradictions(session, START, END, rulings[:1]) == {
        feeder.account_id: "holdings_reported"
    }
    assert zero_value_account_ids(session, START, END, rulings[:1]) == frozenset()
    # A window before the holdings still honors the ruling.
    assert zero_value_account_ids(session, START, date(2026, 5, 19), rulings[:1]) == frozenset(
        {feeder.account_id}
    )


def test_ruled_feeder_without_snapshots_does_not_block_daily_values(
    session: Session, monkeypatch: pytest.MonkeyPatch
):
    held = _account(session, "held")
    feeder = _account(session, "feeder", institution="Fidelity")
    vti = _security(session, "VTI")
    _snapshot(session, held, vti, START, "1000")
    _snapshot(session, held, vti, END, "1100")
    session.commit()

    blocked = performance_service._daily_portfolio_value_assessment(session, START, END)  # pyright: ignore[reportPrivateUsage]
    assert START not in blocked.values

    monkeypatch.setattr(
        owner_rulings,
        "ZERO_VALUE_FLOW_THROUGH",
        (ZeroValueFlowThroughRuling(feeder.account_id, "Fidelity", date(2026, 11, 12)),),
    )
    ruled = performance_service._daily_portfolio_value_assessment(session, START, END)  # pyright: ignore[reportPrivateUsage]

    assert ruled.values[START] == Decimal(1000)
    assert ruled.values[END] == Decimal(1100)
    assert feeder.account_id in ruled.valuation_account_ids


def test_data_quality_reports_a_contradicted_ruling_but_not_a_missing_account(
    session: Session, monkeypatch: pytest.MonkeyPatch
):
    feeder = _account(session, "feeder", institution="Fidelity")
    vti = _security(session, "VTI")
    _snapshot(session, feeder, vti, date(2026, 5, 20))
    session.commit()
    monkeypatch.setattr(
        owner_rulings,
        "ZERO_VALUE_FLOW_THROUGH",
        (
            ZeroValueFlowThroughRuling(feeder.account_id, "Fidelity", date(2026, 11, 12)),
            ZeroValueFlowThroughRuling(9999, "Fidelity", date(2026, 11, 12)),
        ),
    )

    findings = _find_contradicted_owner_rulings(session, END)

    assert [(f.category, f.context["account_id"], f.context["reason"]) for f in findings] == [
        (OWNER_RULING_CONTRADICTED, str(feeder.account_id), "holdings_reported")
    ]


# --- Issue 3: overlapping-broker warning -------------------------------------


def test_distinct_accounts_at_one_broker_are_not_double_counted(session: Session):
    plaid = _account(session, "p", institution="Robinhood", source="plaid", mask="1111")
    snap = _account(session, "s", institution="Robinhood", source="snaptrade", mask="2222")
    vti = _security(session, "VTI")
    qqq = _security(session, "QQQ")
    _snapshot(session, plaid, vti, END)
    _snapshot(session, snap, qqq, END)
    session.commit()

    assert _find_overlapping_broker_connections(session) == []


@pytest.mark.parametrize("basis", ["account_mask", "identical_positions"])
def test_same_account_through_two_aggregators_is_flagged(session: Session, basis: str):
    same_mask = basis == "account_mask"
    plaid = _account(session, "p", institution="Robinhood", source="plaid", mask="1111")
    snap = _account(
        session,
        "s",
        institution="Robinhood",
        source="snaptrade",
        mask="1111" if same_mask else None,
    )
    vti = _security(session, "VTI")
    _snapshot(session, plaid, vti, END)
    if not same_mask:
        _snapshot(session, snap, vti, END)
    session.commit()

    findings = _find_overlapping_broker_connections(session)

    assert [f.category for f in findings] == [OVERLAPPING_BROKER_CONNECTIONS]
    assert f"({basis})" in findings[0].context["matched_account_pairs"]
