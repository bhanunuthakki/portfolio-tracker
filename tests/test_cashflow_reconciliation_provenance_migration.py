from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from portfolio_tracker.config import get_settings
from portfolio_tracker.models import (
    Account,
    CashFlowAccountMappingBasis,
    CashFlowDecisionAuthority,
    CashFlowEffectiveDateBasis,
    CashFlowEvidenceConfidence,
    CashFlowReconciliationDecision,
    CashFlowReconciliationRun,
    CashFlowReconciliationRunDecision,
    CashFlowReconciliationRunDecisionKind,
    CashFlowReconciliationRunStatus,
    CashFlowReconciliationRunTransactionMutation,
    CashFlowReconciliationTransactionMutationKind,
    CashFlowResolutionKind,
    CashFlowSourceAmountSignBasis,
    CashFlowSourceAttestation,
    CashFlowSourceEvent,
    CashFlowSourceLocatorKind,
    CashFlowSourceType,
    InvestmentTransaction,
    InvestmentTransactionType,
    Item,
    ItemSource,
)


def _config(database_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    get_settings.cache_clear()
    return Config(str(Path(__file__).parents[1] / "alembic.ini"))


def _production_engine(database_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    get_settings.cache_clear()
    from portfolio_tracker.db import build_engine

    return build_engine()


def _add_normalized_authority(session: Session) -> tuple[Account, InvestmentTransaction]:
    item = Item(
        source=ItemSource.PLAID.value,
        plaid_item_id="fixture-item",
        plaid_access_token_encrypted="fixture-ciphertext",
        institution_name="Fixture Institution",
    )
    account = Account(
        item=item,
        plaid_account_id="fixture-account",
        name="Fixture Investment Account",
        type="investment",
        currency="USD",
    )
    transaction = InvestmentTransaction(
        plaid_investment_transaction_id="fixture-provider-transaction",
        account_id=0,
        date=date(2025, 1, 15),
        name="Fixture contribution",
        quantity=Decimal("0"),
        amount=Decimal("1000.00"),
        type=InvestmentTransactionType.TRANSFER.value,
        currency="USD",
        origin="broker",
    )
    session.add_all([item, account])
    session.flush()
    transaction.account_id = account.account_id
    session.add(transaction)
    session.flush()
    return account, transaction


def _add_attested_event(
    session: Session,
    *,
    account_id: int,
    source_event_id: str,
    locator: str,
) -> CashFlowSourceEvent:
    captured_at = datetime(2025, 2, 1, 12, tzinfo=UTC)
    attestation = CashFlowSourceAttestation(
        attestation_key=f"attestation-{source_event_id[:8]}",
        account_id=account_id,
        coverage_start=date(2025, 1, 1),
        coverage_end=date(2025, 1, 31),
        source_type=CashFlowSourceType.BROKERAGE_STATEMENT.value,
        source_reference=f"fixture-statement-{source_event_id[:8]}.pdf",
        source_sha256="1" * 64,
        captured_at=captured_at,
        approved_at=captured_at,
        methodology_version="2",
        account_identity_sha256="2" * 64,
        account_mapping_basis=CashFlowAccountMappingBasis.STATEMENT_ACCOUNT_IDENTIFIER.value,
        account_mapping_confidence=CashFlowEvidenceConfidence.EXACT.value,
        source_format="brokerage_statement_pdf",
        parser_version="fixture-v1",
        source_timezone="America/New_York",
        source_row_count=20,
        cashflow_candidate_count=1,
        source_event_set_sha256="3" * 64,
        manifest_sha256="4" * 64,
    )
    session.add(attestation)
    session.flush()
    source_event = CashFlowSourceEvent(
        source_event_id=source_event_id,
        attestation_id=attestation.attestation_id,
        source_record_id=None,
        source_locator_kind=CashFlowSourceLocatorKind.PAGE_LINE.value,
        source_locator=locator,
        source_row_ordinal=None,
        source_page=2,
        source_line=7,
        source_row_sha256="5" * 64,
        activity_date=date(2025, 1, 15),
        process_date=date(2025, 1, 16),
        settlement_date=None,
        source_amount=Decimal("1000.00"),
        source_amount_sign_basis=CashFlowSourceAmountSignBasis.STATEMENT_PRINTED.value,
        currency="USD",
        source_code="CONTRIBUTION",
    )
    session.add(source_event)
    session.flush()
    return source_event


def _resolved_decision(
    *,
    decision_key: str,
    source_event_id: str,
    target_transaction_id: str,
    superseded_by_decision_key: str | None = None,
) -> CashFlowReconciliationDecision:
    approved_at = datetime(2025, 2, 2, 12, tzinfo=UTC)
    return CashFlowReconciliationDecision(
        decision_key=decision_key,
        source_event_id=source_event_id,
        target_transaction_id=target_transaction_id,
        resolution_kind=CashFlowResolutionKind.PROVIDER_EXACT.value,
        classification="external_in",
        signed_external_amount=Decimal("1000.00"),
        effective_date=date(2025, 1, 15),
        effective_date_basis=CashFlowEffectiveDateBasis.SOURCE_ACTIVITY.value,
        effective_timezone="America/New_York",
        decision_authority=CashFlowDecisionAuthority.OWNER_APPROVED.value,
        confidence=CashFlowEvidenceConfidence.EXACT.value,
        assumption_code=None,
        methodology_version="2",
        decision_payload_sha256="6" * 64,
        approved_at=approved_at,
        superseded_at=approved_at if superseded_by_decision_key is not None else None,
        superseded_by_decision_key=superseded_by_decision_key,
    )


def test_migration_preserves_legacy_attestations_and_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "provenance-round-trip.db"
    config = _config(database_path, monkeypatch)
    command.upgrade(config, "0025")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO items "
                "(item_id, source, plaid_item_id, plaid_access_token_encrypted, is_data_active) "
                "VALUES (1, 'plaid', 'legacy-item', 'fixture-ciphertext', 1)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO accounts "
                "(account_id, item_id, plaid_account_id, name, type, currency) "
                "VALUES (1, 1, 'legacy-account', 'Fixture Account', 'investment', 'USD')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO cashflow_source_attestations "
                "(attestation_id, attestation_key, account_id, coverage_start, coverage_end, "
                "source_type, source_reference, source_sha256, captured_at, approved_at, "
                "methodology_version) VALUES "
                "(1, 'legacy-attestation', 1, '2025-01-01', '2025-01-31', "
                "'brokerage_statement', 'fixture-statement.pdf', :source_sha256, "
                "'2025-02-01 12:00:00', '2025-02-01 12:00:00', '1')"
            ),
            {"source_sha256": "a" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO cashflow_source_gaps "
                "(gap_id, attestation_id, gap_start, gap_end, reason_code) VALUES "
                "(1, 1, '2025-01-20', '2025-01-21', 'statement_missing')"
            )
        )
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    inspector = inspect(engine)
    assert {
        "cashflow_source_events",
        "cashflow_reconciliation_decisions",
        "cashflow_reconciliation_runs",
        "cashflow_reconciliation_run_decisions",
        "cashflow_reconciliation_run_transaction_mutations",
    } <= set(inspector.get_table_names())
    attestation_columns = {
        column["name"] for column in inspector.get_columns("cashflow_source_attestations")
    }
    provenance_columns = {
        "account_identity_sha256",
        "account_mapping_basis",
        "account_mapping_confidence",
        "source_format",
        "parser_version",
        "source_timezone",
        "source_row_count",
        "cashflow_candidate_count",
        "source_event_set_sha256",
        "manifest_sha256",
    }
    assert provenance_columns <= attestation_columns
    decision_foreign_keys = inspector.get_foreign_keys("cashflow_reconciliation_decisions")
    assert any(
        foreign_key["constrained_columns"] == ["source_event_id", "superseded_by_decision_key"]
        and foreign_key["referred_columns"] == ["source_event_id", "decision_key"]
        for foreign_key in decision_foreign_keys
    )
    current_indexes = {
        index["name"]: index for index in inspector.get_indexes("cashflow_reconciliation_decisions")
    }
    assert current_indexes["uq_cashflow_reconciliation_decisions_current_event"]["unique"]
    with engine.connect() as connection:
        legacy = connection.execute(
            text(
                "SELECT account_identity_sha256, source_event_set_sha256, manifest_sha256 "
                "FROM cashflow_source_attestations WHERE attestation_id = 1"
            )
        ).one()
    assert tuple(legacy) == (None, None, None)
    engine.dispose()

    engine = _production_engine(database_path, monkeypatch)
    with Session(engine) as session:
        source_event = CashFlowSourceEvent(
            source_event_id="9" * 64,
            attestation_id=1,
            source_record_id=None,
            source_locator_kind=CashFlowSourceLocatorKind.ROW.value,
            source_locator="row:1",
            source_row_ordinal=1,
            source_page=None,
            source_line=None,
            source_row_sha256="8" * 64,
            activity_date=date(2025, 1, 15),
            process_date=None,
            settlement_date=None,
            source_amount=Decimal("1000.00"),
            source_amount_sign_basis=CashFlowSourceAmountSignBasis.STATEMENT_PRINTED.value,
            currency="USD",
            source_code="CONTRIBUTION",
        )
        unresolved = CashFlowReconciliationDecision(
            decision_key="7" * 64,
            source_event_id=source_event.source_event_id,
            target_transaction_id=None,
            resolution_kind=CashFlowResolutionKind.UNRESOLVED.value,
            classification=None,
            signed_external_amount=None,
            effective_date=None,
            effective_date_basis=None,
            effective_timezone=None,
            decision_authority=CashFlowDecisionAuthority.OWNER_APPROVED.value,
            confidence=CashFlowEvidenceConfidence.PROVISIONAL.value,
            assumption_code="fixture_pending_review",
            methodology_version="2",
            decision_payload_sha256="6" * 64,
            approved_at=None,
        )
        run = CashFlowReconciliationRun(
            run_id="5" * 64,
            plan_digest="4" * 64,
            manifest_set_sha256="3" * 64,
            software_revision="fixture-revision",
            backup_reference="fixture-backup-reference",
            preview_reference="fixture-preview-reference",
            affected_start=date(2025, 1, 1),
            affected_end=date(2025, 1, 31),
            affected_account_count=1,
            source_event_count=1,
            planned_mutation_count=1,
            applied_mutation_count=0,
            status=CashFlowReconciliationRunStatus.PREVIEWED.value,
            approved_at=None,
            applied_at=None,
        )
        membership = CashFlowReconciliationRunDecision(
            run_id=run.run_id,
            decision_key=unresolved.decision_key,
            membership_kind=CashFlowReconciliationRunDecisionKind.VERIFIED.value,
        )
        session.add(source_event)
        session.flush()
        session.add_all([unresolved, run])
        session.flush()
        session.add(membership)
        session.commit()
    engine.dispose()

    command.downgrade(config, "0025")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    inspector = inspect(engine)
    assert "cashflow_source_events" not in inspector.get_table_names()
    assert "cashflow_reconciliation_decisions" not in inspector.get_table_names()
    assert "cashflow_reconciliation_runs" not in inspector.get_table_names()
    assert "cashflow_reconciliation_run_decisions" not in inspector.get_table_names()
    assert "cashflow_reconciliation_run_transaction_mutations" not in inspector.get_table_names()
    downgraded_columns = {
        column["name"] for column in inspector.get_columns("cashflow_source_attestations")
    }
    assert provenance_columns.isdisjoint(downgraded_columns)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM cashflow_source_attestations")) == 1
        assert connection.scalar(text("SELECT count(*) FROM cashflow_source_gaps")) == 1
    engine.dispose()
    get_settings.cache_clear()


def test_lineage_current_decision_and_supersession_constraints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "provenance-constraints.db"
    config = _config(database_path, monkeypatch)
    command.upgrade(config, "head")
    engine = _production_engine(database_path, monkeypatch)

    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1

    with Session(engine) as session:
        account, transaction = _add_normalized_authority(session)
        first_event = _add_attested_event(
            session,
            account_id=account.account_id,
            source_event_id="a" * 64,
            locator="page:2#line:7",
        )
        current = _resolved_decision(
            decision_key="b" * 64,
            source_event_id=first_event.source_event_id,
            target_transaction_id=transaction.plaid_investment_transaction_id,
        )
        session.add(current)
        session.commit()

        historical = _resolved_decision(
            decision_key="c" * 64,
            source_event_id=first_event.source_event_id,
            target_transaction_id=transaction.plaid_investment_transaction_id,
            superseded_by_decision_key=current.decision_key,
        )
        run = CashFlowReconciliationRun(
            run_id="d" * 64,
            plan_digest="e" * 64,
            manifest_set_sha256="f" * 64,
            software_revision="fixture-revision",
            backup_reference="fixture-backup-reference",
            preview_reference="fixture-preview-reference",
            affected_start=date(2025, 1, 1),
            affected_end=date(2025, 1, 31),
            affected_account_count=1,
            source_event_count=1,
            planned_mutation_count=1,
            applied_mutation_count=1,
            status=CashFlowReconciliationRunStatus.APPLIED.value,
            approved_at=datetime(2025, 2, 2, 12, tzinfo=UTC),
            applied_at=datetime(2025, 2, 2, 13, tzinfo=UTC),
        )
        session.add_all([historical, run])
        session.commit()

        persisted = session.scalars(
            select(CashFlowReconciliationDecision).where(
                CashFlowReconciliationDecision.source_event_id == first_event.source_event_id
            )
        ).all()
        assert {decision.decision_key for decision in persisted} == {
            current.decision_key,
            historical.decision_key,
        }
        assert historical.superseded_by_decision_key == current.decision_key
        assert current.target_transaction_id == transaction.plaid_investment_transaction_id
        assert first_event.attestation_id is not None

        duplicate_current = _resolved_decision(
            decision_key="7" * 64,
            source_event_id=first_event.source_event_id,
            target_transaction_id=transaction.plaid_investment_transaction_id,
        )
        session.add(duplicate_current)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        semantic_event = _add_attested_event(
            session,
            account_id=account.account_id,
            source_event_id="2" * 64,
            locator="page:4#line:3",
        )
        invalid_internal = _resolved_decision(
            decision_key="2" * 64,
            source_event_id=semantic_event.source_event_id,
            target_transaction_id=transaction.plaid_investment_transaction_id,
        )
        invalid_internal.resolution_kind = CashFlowResolutionKind.INTERNAL.value
        session.add(invalid_internal)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        incomplete_run = CashFlowReconciliationRun(
            run_id="3" * 64,
            plan_digest="4" * 64,
            manifest_set_sha256="5" * 64,
            software_revision="fixture-revision",
            backup_reference="fixture-backup-reference",
            preview_reference="fixture-preview-reference",
            affected_start=date(2025, 1, 1),
            affected_end=date(2025, 1, 31),
            affected_account_count=1,
            source_event_count=1,
            planned_mutation_count=2,
            applied_mutation_count=1,
            status=CashFlowReconciliationRunStatus.APPLIED.value,
            approved_at=datetime(2025, 2, 2, 12, tzinfo=UTC),
            applied_at=datetime(2025, 2, 2, 13, tzinfo=UTC),
        )
        session.add(incomplete_run)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        run_decision = CashFlowReconciliationRunDecision(
            run_id=run.run_id,
            decision_key=historical.decision_key,
            membership_kind=CashFlowReconciliationRunDecisionKind.CREATED.value,
        )
        run_mutation = CashFlowReconciliationRunTransactionMutation(
            run_id=run.run_id,
            target_transaction_id=transaction.plaid_investment_transaction_id,
            mutation_kind=(
                CashFlowReconciliationTransactionMutationKind.OVERRIDE_UPDATE.value
            ),
            before_payload_sha256="a" * 64,
            after_payload_sha256="b" * 64,
        )
        session.add_all([run_decision, run_mutation])
        session.commit()

        assert session.get(
            CashFlowReconciliationRunDecision,
            (run.run_id, historical.decision_key),
        ) is not None
        assert session.get(
            CashFlowReconciliationRunTransactionMutation,
            (
                run.run_id,
                transaction.plaid_investment_transaction_id,
                CashFlowReconciliationTransactionMutationKind.OVERRIDE_UPDATE.value,
            ),
        ) is not None

        second_event = _add_attested_event(
            session,
            account_id=account.account_id,
            source_event_id="8" * 64,
            locator="page:3#line:4",
        )
        second_current = _resolved_decision(
            decision_key="9" * 64,
            source_event_id=second_event.source_event_id,
            target_transaction_id=transaction.plaid_investment_transaction_id,
        )
        session.add(second_current)
        session.commit()

        wrong_event_successor = _resolved_decision(
            decision_key="0" * 64,
            source_event_id=first_event.source_event_id,
            target_transaction_id=transaction.plaid_investment_transaction_id,
            superseded_by_decision_key=second_current.decision_key,
        )
        session.add(wrong_event_successor)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        one_sided_supersession = _resolved_decision(
            decision_key="1" * 64,
            source_event_id=first_event.source_event_id,
            target_transaction_id=transaction.plaid_investment_transaction_id,
        )
        one_sided_supersession.superseded_at = datetime(2025, 2, 3, 12, tzinfo=UTC)
        session.add(one_sided_supersession)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    engine.dispose()
    get_settings.cache_clear()
