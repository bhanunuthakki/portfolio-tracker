"""Deterministic, non-sensitive receipts for provider transaction deliveries.

The receipt proves what a successful adapter call established: the requested
window, the provider's reported row count, the number of rows parsed, and a
commitment to the normalized record set. It deliberately contains no account
identifier, transaction identifier, amount, description, or credential.

This is not by itself a broker-history completeness attestation. It proves
delivery completeness relative to the provider response, leaving brokerage
retention limits and account mapping to the later attestation layer.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import date
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProviderDeliveryError(RuntimeError):
    """Base class for safe-to-display provider delivery failures."""


class ProviderDeliveryIncompleteError(ProviderDeliveryError):
    """The adapter could not prove that it received every reported row."""


class ProviderPayloadError(ProviderDeliveryError):
    """A provider row or pagination envelope failed boundary validation."""


class UnsupportedProviderParserError(ProviderDeliveryError):
    """A source/parser pair is not explicitly accepted by this build."""


_SUPPORTED_PROVIDER_PARSERS: dict[str, frozenset[str]] = {
    "plaid_investment_transactions_api": frozenset({"plaid_investment_tx.v1"}),
    "snaptrade_account_activities_api": frozenset({"snaptrade_account_activity.v1"}),
}


def assert_supported_provider_parser(source_format: str, parser_version: str) -> None:
    """Reject unknown source formats and parser versions rather than certifying them."""
    accepted = _SUPPORTED_PROVIDER_PARSERS.get(source_format)
    if accepted is None or parser_version not in accepted:
        raise UnsupportedProviderParserError("provider source format/parser is not supported")


class ProviderDeliveryMetadata(BaseModel):
    """Aggregate evidence emitted only after complete, valid pagination."""

    model_config = ConfigDict(frozen=True)

    provider: Literal["plaid", "snaptrade"]
    source_format: str = Field(max_length=64)
    parser_version: str = Field(max_length=32)
    requested_start_date: date
    requested_end_date: date
    page_count: int = Field(ge=1)
    provider_reported_total: int = Field(ge=0)
    fetched_count: int = Field(ge=0)
    unique_record_count: int = Field(ge=0)
    record_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    is_complete: Literal[True] = True

    @model_validator(mode="after")
    def _validate_complete_receipt(self) -> Self:
        expected_format = {
            "plaid": "plaid_investment_transactions_api",
            "snaptrade": "snaptrade_account_activities_api",
        }[self.provider]
        if self.source_format != expected_format:
            raise ValueError("provider and source format do not match")
        accepted = _SUPPORTED_PROVIDER_PARSERS.get(self.source_format)
        if accepted is None or self.parser_version not in accepted:
            raise ValueError("provider source format/parser is not supported")
        if self.requested_start_date > self.requested_end_date:
            raise ValueError("provider delivery date range is invalid")
        if (
            self.fetched_count != self.provider_reported_total
            or self.unique_record_count != self.fetched_count
        ):
            raise ValueError("provider delivery counts do not prove completeness")
        return self


def build_provider_delivery_metadata(
    *,
    provider: Literal["plaid", "snaptrade"],
    source_format: str,
    parser_version: str,
    requested_start_date: date,
    requested_end_date: date,
    page_count: int,
    provider_reported_total: int,
    record_ids: Sequence[str],
    normalized_records: Sequence[dict[str, Any]],
) -> ProviderDeliveryMetadata:
    """Validate count parity and hash an order-independent normalized record set."""
    assert_supported_provider_parser(source_format, parser_version)
    fetched_count = len(normalized_records)
    unique_record_count = len(set(record_ids))
    if len(record_ids) != fetched_count:
        raise ProviderPayloadError("provider delivery record identifiers are incomplete")
    if any(not record_id for record_id in record_ids):
        raise ProviderPayloadError("provider delivery contains an empty record identifier")
    if fetched_count != provider_reported_total or unique_record_count != fetched_count:
        raise ProviderDeliveryIncompleteError(
            f"{provider} transaction delivery incomplete: "
            f"reported={provider_reported_total}, fetched={fetched_count}, "
            f"unique={unique_record_count}"
        )
    return ProviderDeliveryMetadata(
        provider=provider,
        source_format=source_format,
        parser_version=parser_version,
        requested_start_date=requested_start_date,
        requested_end_date=requested_end_date,
        page_count=page_count,
        provider_reported_total=provider_reported_total,
        fetched_count=fetched_count,
        unique_record_count=unique_record_count,
        record_set_sha256=canonical_normalized_record_set_sha256(normalized_records),
    )


def canonical_normalized_record_set_sha256(
    normalized_records: Sequence[dict[str, Any]],
) -> str:
    """Commit to normalized records without retaining or exposing their values."""
    canonical_rows = sorted(
        normalized_records,
        key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")),
    )
    encoded = json.dumps(
        canonical_rows,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
