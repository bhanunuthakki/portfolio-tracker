from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from portfolio_tracker.models import (
    Account,
    AccountValuationObservation,
    AccountValuationSourceKind,
    Item,
)
from portfolio_tracker.services.account_valuations import (
    NewAccountValuationObservation,
    account_valuation_boundary_evidence,
    latest_complete_account_valuation_on_or_before,
    latest_complete_account_valuations_on_or_before,
    record_account_valuation_observation,
)


def _account(session) -> Account:
    item = Item(source="plaid", institution_name="Broker")
    session.add(item)
    session.flush()
    account = Account(
        item_id=item.item_id,
        plaid_account_id="provider-account-1",
        name="Taxable",
        type="investment",
        currency="USD",
    )
    session.add(account)
    session.flush()
    return account


def _observation(
    account_id: int,
    *,
    as_of_date: date = date(2026, 9, 2),
    total_value: Decimal = Decimal("1234.56"),
    fetched_at: datetime = datetime(2026, 9, 3, 12, tzinfo=UTC),
    is_complete: bool = True,
    is_empty: bool = False,
    source_payload_sha256: str = "a" * 64,
) -> NewAccountValuationObservation:
    return NewAccountValuationObservation(
        account_id=account_id,
        as_of_date=as_of_date,
        as_of_at=datetime.combine(as_of_date, time(20), tzinfo=UTC),
        total_value=total_value,
        cash_value=Decimal("34.56"),
        currency="USD",
        source_kind=AccountValuationSourceKind.PROVIDER_API,
        source_provider="plaid",
        source_reference="investments/holdings:get.account.current",
        source_record_id="provider-account-1:2026-09-02",
        source_payload_sha256=source_payload_sha256,
        fetched_at=fetched_at,
        is_complete=is_complete,
        is_empty=is_empty,
    )


def test_record_is_idempotent_for_the_same_exact_capture(session):
    account = _account(session)
    capture = _observation(account.account_id)
    first = record_account_valuation_observation(session, capture)
    replay = record_account_valuation_observation(
        session,
        capture,
    )

    assert first.created is True
    assert replay.created is False
    assert replay.observation.valuation_observation_id == (
        first.observation.valuation_observation_id
    )
    assert replay.observation.fetched_at == first.observation.fetched_at
    assert session.scalar(select(func.count()).select_from(AccountValuationObservation)) == 1


def test_replay_rejects_a_stored_payload_tampered_behind_its_key(session):
    account = _account(session)
    capture = _observation(account.account_id)
    stored = record_account_valuation_observation(session, capture).observation
    stored.total_value = Decimal("9999")
    session.flush()

    with pytest.raises(ValueError, match="does not match observation_key"):
        record_account_valuation_observation(session, capture)


def test_later_capture_is_distinct_even_when_provider_values_repeat(session):
    account = _account(session)
    first = record_account_valuation_observation(session, _observation(account.account_id))
    later = record_account_valuation_observation(
        session,
        _observation(
            account.account_id,
            fetched_at=datetime(2026, 9, 3, 13, tzinfo=UTC),
        ),
    )

    assert first.created is True
    assert later.created is True
    assert later.observation.observation_key != first.observation.observation_key
    assert session.scalar(select(func.count()).select_from(AccountValuationObservation)) == 2


def test_latest_capture_survives_value_reversion(session):
    account = _account(session)
    for value, fetched_at, digest in (
        (Decimal("100"), datetime(2026, 9, 3, 9, tzinfo=UTC), "a" * 64),
        (Decimal("110"), datetime(2026, 9, 3, 10, tzinfo=UTC), "b" * 64),
        (Decimal("100"), datetime(2026, 9, 3, 11, tzinfo=UTC), "a" * 64),
    ):
        record_account_valuation_observation(
            session,
            _observation(
                account.account_id,
                total_value=value,
                fetched_at=fetched_at,
                source_payload_sha256=digest,
            ),
        )

    selected = latest_complete_account_valuation_on_or_before(
        session,
        account_id=account.account_id,
        boundary_date=date(2026, 9, 2),
        earliest_date=date(2026, 9, 2),
    )

    assert selected is not None
    assert selected.total_value == Decimal("100")
    # SQLite drops the offset while preserving the normalized UTC wall time.
    assert selected.fetched_at == datetime(2026, 9, 3, 11)


def test_correction_is_append_only_and_latest_capture_wins(session):
    account = _account(session)
    first = record_account_valuation_observation(session, _observation(account.account_id))
    corrected = record_account_valuation_observation(
        session,
        _observation(
            account.account_id,
            total_value=Decimal("1250.00"),
            fetched_at=datetime(2026, 9, 3, 14, tzinfo=UTC),
            source_payload_sha256="b" * 64,
        ),
    )

    selected = latest_complete_account_valuation_on_or_before(
        session,
        account_id=account.account_id,
        boundary_date=date(2026, 9, 2),
        source_kinds=(AccountValuationSourceKind.PROVIDER_API,),
    )

    assert corrected.created is True
    assert corrected.observation.observation_key != first.observation.observation_key
    assert selected is not None
    assert selected.valuation_observation_id == corrected.observation.valuation_observation_id
    assert session.scalar(select(func.count()).select_from(AccountValuationObservation)) == 2


def test_latest_complete_query_fails_closed_on_partial_and_staleness(session):
    account = _account(session)
    partial = record_account_valuation_observation(
        session,
        _observation(
            account.account_id,
            as_of_date=date(2026, 9, 2),
            is_complete=False,
        ),
    ).observation
    complete = record_account_valuation_observation(
        session,
        _observation(
            account.account_id,
            as_of_date=date(2026, 9, 1),
            fetched_at=datetime(2026, 9, 2, 12, tzinfo=UTC),
        ),
    ).observation

    selected = latest_complete_account_valuation_on_or_before(
        session,
        account_id=account.account_id,
        boundary_date=date(2026, 9, 2),
        earliest_date=date(2026, 9, 1),
    )
    stale = latest_complete_account_valuation_on_or_before(
        session,
        account_id=account.account_id,
        boundary_date=date(2026, 9, 2),
        earliest_date=date(2026, 9, 2),
    )

    assert partial.is_complete is False
    assert selected is not None
    assert selected.valuation_observation_id == complete.valuation_observation_id
    assert stale is None


def test_bulk_query_leaves_missing_accounts_explicit(session):
    covered = _account(session)
    missing = Account(
        item_id=covered.item_id,
        plaid_account_id="provider-account-2",
        name="IRA",
        type="investment",
        currency="USD",
    )
    session.add(missing)
    session.flush()
    observation = record_account_valuation_observation(
        session, _observation(covered.account_id)
    ).observation

    selected = latest_complete_account_valuations_on_or_before(
        session,
        account_ids=(covered.account_id, missing.account_id),
        boundary_date=date(2026, 9, 2),
        earliest_date=date(2026, 9, 2),
    )

    assert selected == {covered.account_id: observation}
    assert missing.account_id not in selected


def test_empty_account_requires_a_complete_zero_balance(session):
    account = _account(session)
    with pytest.raises(ValueError, match="empty account observation"):
        record_account_valuation_observation(
            session,
            _observation(
                account.account_id,
                total_value=Decimal("1.00"),
                is_empty=True,
            ),
        )

    empty_input = _observation(
        account.account_id,
        total_value=Decimal("0"),
        is_empty=True,
    )
    empty_input = replace(empty_input, cash_value=Decimal("0"))
    stored = record_account_valuation_observation(session, empty_input).observation
    assert stored.is_complete is True
    assert stored.is_empty is True


def test_values_must_fit_losslessly_in_persisted_numeric(session):
    account = _account(session)
    with pytest.raises(ValueError, match="more than six decimal places"):
        record_account_valuation_observation(
            session,
            _observation(account.account_id, total_value=Decimal("1.0000001")),
        )


def test_boundary_evidence_carries_exact_source_locator(session):
    account = _account(session)
    observation = record_account_valuation_observation(
        session, _observation(account.account_id)
    ).observation

    evidence = account_valuation_boundary_evidence(observation)

    assert evidence.observation_key == observation.observation_key
    assert evidence.account_id == account.account_id
    assert evidence.as_of_date == date(2026, 9, 2)
    assert evidence.source_kind == "provider_api"
    assert evidence.source_provider == "plaid"
    assert evidence.source_reference == "investments/holdings:get.account.current"
    assert evidence.source_record_id == "provider-account-1:2026-09-02"
    assert evidence.source_payload_sha256 == "a" * 64
    assert evidence.normalization_version == "1"
    assert evidence.is_complete is True
