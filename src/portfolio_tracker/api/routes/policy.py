"""Read and govern the single-user policy benchmark allocation."""

from __future__ import annotations

from ipaddress import ip_address
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from portfolio_tracker.config import get_settings
from portfolio_tracker.db import get_session
from portfolio_tracker.schemas import PolicyOut, PolicyReplaceIn
from portfolio_tracker.services.policy_write import (
    PolicyIdempotencyConflictError,
    PolicyRecomputationError,
    PolicyRevisionConflictError,
    PolicyValidationError,
    read_policy,
    replace_policy,
)

router = APIRouter(prefix="/api/policy", tags=["policy"])
_WRITE_INTENT = "replace-policy"


def authorize_policy_write(
    request: Request,
    intent: Annotated[str | None, Header(alias="X-Portfolio-Write-Intent")] = None,
) -> None:
    """Authorize a state change for this deliberately localhost-only service.

    The peer must be loopback, browser origins must be configured, and the
    caller must opt into the destructive full-replacement semantics. No
    credential is put in a URL, browser bundle, exception, or log.
    """
    peer = request.client.host if request.client is not None else ""
    try:
        is_loopback = ip_address(peer).is_loopback
    except ValueError:
        is_loopback = False
    origin = request.headers.get("origin")
    origin_allowed = origin is None or origin in get_settings().cors_origins_list
    if not is_loopback or not origin_allowed or intent != _WRITE_INTENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "POLICY_WRITE_UNAUTHORIZED"},
        )


@router.get("", response_model=PolicyOut)
def get_policy(session: Annotated[Session, Depends(get_session)]) -> PolicyOut:
    return read_policy(session)


@router.put(
    "",
    response_model=PolicyOut,
    dependencies=[Depends(authorize_policy_write)],
)
def put_policy(
    body: PolicyReplaceIn,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
) -> PolicyOut:
    """Atomically replace the complete policy at an expected revision."""
    try:
        result, replayed = replace_policy(session, body)
    except PolicyValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "POLICY_VALIDATION_FAILED", "reason": exc.reason},
        ) from None
    except PolicyIdempotencyConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "POLICY_IDEMPOTENCY_CONFLICT"},
        ) from None
    except PolicyRevisionConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "POLICY_REVISION_CONFLICT",
                "expected_revision": exc.expected_revision,
                "current_revision": exc.current_revision,
            },
        ) from None
    except PolicyRecomputationError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "POLICY_RECOMPUTATION_INVALIDATION_FAILED",
                "retryable": True,
            },
        ) from None
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return result
