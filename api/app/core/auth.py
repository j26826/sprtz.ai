"""Caller identity, derived from IAP.

IAP terminates authentication at the edge and forwards a signed assertion. The
application must verify that assertion rather than trust any client-supplied
identity — without verification, anyone who reaches the container directly could
claim to be anyone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

_request = google_requests.Request()


@dataclass(frozen=True)
class CallerIdentity:
    uid: str
    email: str

    @property
    def is_anonymous(self) -> bool:
        return not self.uid


def _verify(assertion: str, audience: str) -> CallerIdentity:
    claims = id_token.verify_token(
        assertion,
        _request,
        audience=audience,
        certs_url="https://www.gstatic.com/iap/verify/public_key",
    )
    # With external identities the subject is prefixed; the Identity Platform uid
    # is the trailing segment, which is what Firestore's rules match on.
    subject = claims.get("sub", "")
    uid = subject.rsplit(":", 1)[-1] if subject else ""
    email = claims.get("email", "")
    if not uid:
        raise ValueError("IAP assertion carried no subject")
    return CallerIdentity(uid=uid, email=email)


async def current_user(
    x_goog_iap_jwt_assertion: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> CallerIdentity:
    """Resolve the caller, or reject the request."""
    if settings.is_local:
        # Local development only. This branch is unreachable in any deployed
        # environment because ENVIRONMENT is set by Terraform.
        return CallerIdentity(uid="local-dev-user", email="local@example.com")

    if not x_goog_iap_jwt_assertion:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing IAP assertion. This service must be reached through IAP.",
        )

    try:
        return _verify(x_goog_iap_jwt_assertion, settings.iap_audience)
    except Exception as exc:  # noqa: BLE001
        logger.warning("rejected IAP assertion: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid IAP assertion.",
        ) from exc
