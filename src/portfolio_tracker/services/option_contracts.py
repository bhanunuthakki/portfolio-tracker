"""Source-backed option descriptors; unknown deliverables never imply 100 shares."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

QuantityUnit: TypeAlias = Literal["shares", "underlying_units", "unknown"]
_OCC_PATTERN = re.compile(r"([A-Z][A-Z0-9.]{0,5})\s*(\d{6})([CP])(\d{8})")


class OptionContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    underlying_ticker: str = Field(min_length=1)
    contract_type: Literal["call", "put"]
    expiration_date: date
    strike_price: Decimal = Field(ge=0)
    multiplier: Decimal | None = Field(default=None, gt=0)
    metadata_source: Literal["plaid.option_contract", "snaptrade.option_symbol", "occ_symbol"]
    multiplier_source: str | None = None


def option_from_occ(ticker: str | None) -> OptionContract | None:
    """Decode only a complete OCC symbol, without inferring contract deliverables."""
    if ticker is None:
        return None
    match = _OCC_PATTERN.fullmatch(ticker.strip())
    if match is None:
        return None
    root, expiry, side, strike = match.groups()
    # Adjusted roots do not identify a trustworthy underlying ticker.
    if any(char.isdigit() for char in root):
        return None
    try:
        expiration = date(2000 + int(expiry[:2]), int(expiry[2:4]), int(expiry[4:6]))
    except ValueError:
        return None
    return OptionContract(
        underlying_ticker=root,
        contract_type="call" if side == "C" else "put",
        expiration_date=expiration,
        strike_price=Decimal(strike) / Decimal(1000),
        metadata_source="occ_symbol",
    )


def occ_has_adjusted_root(
    ticker: str | None, *, sourced_mini_underlying: str | None = None
) -> bool:
    match = _OCC_PATTERN.fullmatch(ticker.strip()) if ticker else None
    if match is None:
        return False
    root = match.group(1)
    # Mini options use the underlying root plus 7. Accept that root only
    # with matching provider evidence; the symbol alone proves no multiplier.
    if sourced_mini_underlying is not None and root == f"{sourced_mini_underlying}7":
        return False
    return any(char.isdigit() for char in root)


def stored_option(payload: str | None, ticker: str | None) -> OptionContract | None:
    return OptionContract.model_validate_json(payload) if payload else option_from_occ(ticker)


def contracts(
    quantity: Decimal, unit: QuantityUnit, option: OptionContract | None
) -> Decimal | None:
    if unit != "underlying_units" or option is None or option.multiplier is None:
        return None
    return quantity / option.multiplier
