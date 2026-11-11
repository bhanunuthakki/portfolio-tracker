"""Owner-confirmed sign corrections for individual Plaid short calls."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from portfolio_tracker.config import PROJECT_ROOT
from portfolio_tracker.plaid_client import PlaidHolding
from portfolio_tracker.services.option_contracts import option_from_occ

OVERRIDES_PATH = PROJECT_ROOT / "data" / "plaid_short_calls.json"


class ShortCallOverride(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    plaid_account_id: str = Field(min_length=1)
    occ_symbol: str
    confirmed_on: date
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        option = option_from_occ(self.occ_symbol)
        if (
            option is None
            or option.contract_type != "call"
            or self.confirmed_on > option.expiration_date
        ):
            raise ValueError("Expected a call symbol and a confirmation date on or before expiry")
        return self


def load_short_call_overrides(path: Path = OVERRIDES_PATH) -> tuple[ShortCallOverride, ...]:
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return ()
    try:
        return TypeAdapter(tuple[ShortCallOverride, ...]).validate_json(content)
    except ValidationError:
        raise ValueError("Invalid private Plaid short-call overrides") from None


def correct_short_call(
    holding: PlaidHolding, ticker: str | None, active_keys: set[tuple[str, str]]
) -> PlaidHolding:
    if (
        ticker is None
        or holding.quantity == 0
        or (holding.plaid_account_id, "".join(ticker.split())) not in active_keys
    ):
        return holding
    value = holding.institution_value
    basis = holding.cost_basis
    return holding.model_copy(
        update={
            "quantity": -abs(holding.quantity),
            "institution_value": -abs(value) if value is not None else None,
            "cost_basis": -abs(basis) if basis is not None else None,
        }
    )


def active_short_call_keys(
    overrides: tuple[ShortCallOverride, ...], snapshot_date: date
) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for override in overrides:
        option = option_from_occ(override.occ_symbol)
        if option is not None and override.confirmed_on <= snapshot_date <= option.expiration_date:
            keys.add((override.plaid_account_id, "".join(override.occ_symbol.split())))
    return keys
