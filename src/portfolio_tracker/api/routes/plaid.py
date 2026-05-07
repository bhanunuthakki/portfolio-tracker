"""Plaid Link bootstrap + account management endpoints.

Three responsibilities:
  1. mint a Link token for the frontend to open Plaid Link
  2. exchange the public_token returned by Link for a permanent access_token
  3. expose the list of currently-linked Items (and let the user unlink)
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from portfolio_tracker import plaid_client
from portfolio_tracker.crypto import encrypt_token
from portfolio_tracker.db import get_session
from portfolio_tracker.models import INVESTMENT_ACCOUNT_TYPES, Account, Item
from portfolio_tracker.schemas import (
    AccountOut,
    ExchangePublicTokenIn,
    ExchangePublicTokenOut,
    ItemOut,
    LinkTokenOut,
)

router = APIRouter(prefix="/api/plaid", tags=["plaid"])


class LinkProfile(StrEnum):
    """Selects which Plaid `client_user_id` to use when minting a link_token.

    Plaid caches the phone number for "Returning User Experience" per
    client_user_id. Different profiles → distinct caches, so a fresh phone-
    number flow runs for each. Useful when linking accounts that belong to
    a different person (e.g., spouse) on the same machine.
    """

    PRIMARY = "primary"
    SPOUSE = "spouse"


# Stable mapping from profile → client_user_id. New profiles can be added
# here; the value is opaque to Plaid but must remain stable across sessions
# so the user gets the "Returning User" experience for that profile.
_CLIENT_USER_IDS: dict[LinkProfile, str] = {
    LinkProfile.PRIMARY: "local-user-primary",
    LinkProfile.SPOUSE: "local-user-spouse",
}


@router.post("/link-token", response_model=LinkTokenOut)
def create_link_token(
    profile: Annotated[LinkProfile, Query()] = LinkProfile.PRIMARY,
) -> LinkTokenOut:
    return LinkTokenOut(link_token=plaid_client.create_link_token(_CLIENT_USER_IDS[profile]))


@router.post("/exchange-token", response_model=ExchangePublicTokenOut)
def exchange_public_token(
    body: ExchangePublicTokenIn,
    session: Annotated[Session, Depends(get_session)],
) -> ExchangePublicTokenOut:
    access_token, plaid_item_id = plaid_client.exchange_public_token(body.public_token)

    existing = session.execute(
        select(Item).where(Item.plaid_item_id == plaid_item_id)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This institution login is already linked.",
        )

    item = Item(
        plaid_item_id=plaid_item_id,
        plaid_access_token_encrypted=encrypt_token(access_token),
        plaid_institution_id=body.institution_id,
        institution_name=body.institution_name,
    )
    session.add(item)
    session.flush()

    holdings = plaid_client.get_holdings(access_token)
    accounts_linked = 0
    for plaid_account in holdings.accounts:
        if plaid_account.type not in INVESTMENT_ACCOUNT_TYPES:
            continue
        account = Account(
            item_id=item.item_id,
            plaid_account_id=plaid_account.plaid_account_id,
            name=plaid_account.name,
            official_name=plaid_account.official_name,
            type=plaid_account.type,
            subtype=plaid_account.subtype,
            mask=plaid_account.mask,
            currency=plaid_account.currency,
        )
        session.add(account)
        accounts_linked += 1

    session.commit()
    return ExchangePublicTokenOut(item_id=item.item_id, accounts_linked=accounts_linked)


@router.get("/items", response_model=list[ItemOut])
def list_items(session: Annotated[Session, Depends(get_session)]) -> list[ItemOut]:
    rows = session.execute(select(Item).options(selectinload(Item.accounts))).scalars().all()
    return [_item_to_out(item) for item in rows]


def _item_to_out(item: Item) -> ItemOut:
    """Derive a friendly institution name when none was captured at link time.

    Older Items linked before we passed Plaid Link metadata to the backend
    have `institution_name=None`. Falling back to "Unknown Institution" in
    the UI is unhelpful when the account names plainly say "Robinhood
    Roth IRA" / "SoFi Self-directed". We extract the prefix shared across
    every account name in the Item — that's almost always the brokerage.
    """
    name = item.institution_name or _derive_institution_name(item.accounts)
    return ItemOut(
        item_id=item.item_id,
        institution_name=name,
        plaid_institution_id=item.plaid_institution_id,
        linked_at=item.linked_at,
        last_refreshed_at=item.last_refreshed_at,
        accounts=[AccountOut.model_validate(a) for a in item.accounts],
    )


def _derive_institution_name(accounts: list[Account]) -> str | None:
    """Common-prefix-of-first-word heuristic.

    "Robinhood Spending" + "Robinhood Roth IRA" → "Robinhood".
    "SoFi Self-directed" + "SoFi Self-directed Traditional IRA" → "SoFi".
    Returns None when there's no clear shared prefix.
    """
    names = [a.name for a in accounts if a.name]
    if not names:
        return None
    first_words = [n.split(maxsplit=1)[0] for n in names]
    if all(w == first_words[0] for w in first_words) and first_words[0]:
        return first_words[0]
    return None


@router.delete("/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def unlink_item(item_id: int, session: Annotated[Session, Depends(get_session)]) -> None:
    item = session.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    session.delete(item)
    session.commit()
