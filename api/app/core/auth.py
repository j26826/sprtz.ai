"""Caller identity.

Authentication is enforced here, in the application, rather than by IAP.

IAP could not be made to work in this project: its authorization step ran with
an empty principal (`authenticationInfo: {}` in the audit log) on both the Cloud
Run built-in integration and a load-balancer backend service, so no IAM binding
could ever match — `allAuthenticatedUsers` was refused too. The project's legacy
OAuth brand has zero clients and the API that could create one was shut down in
March 2026.

So the SPA signs in with Identity Platform and sends that ID token, and this
module verifies it. That is a better fit for the data model anyway: the uid in
a Firebase token is the same uid Firestore's rules compare against
(`ownerUid == request.auth.uid`), whereas an IAP assertion carries a Google
subject that does not match it — jobs created under an IAP identity would have
been invisible to the browser's own listeners.

An IAP assertion is still honoured when present, so putting IAP back in front
later needs no code change.
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


def _verify_firebase(token: str, project_id: str) -> CallerIdentity:
    """Verify an Identity Platform ID token.

    The uid is taken from the token subject, which is exactly what Firestore
    security rules see as `request.auth.uid` — so ownership written here matches
    what the browser can read back.
    """
    claims = id_token.verify_firebase_token(token, _request, audience=project_id)
    if not claims:
        raise ValueError("token did not verify")
    uid = claims.get("user_id") or claims.get("sub") or ""
    if not uid:
        raise ValueError("token carried no subject")
    return CallerIdentity(uid=uid, email=claims.get("email", ""))


def _verify_iap(assertion: str, audience: str) -> CallerIdentity:
    """Verify an IAP assertion, for deployments that do front this with IAP."""
    claims = id_token.verify_token(
        assertion,
        _request,
        audience=audience,
        certs_url="https://www.gstatic.com/iap/verify/public_key",
    )
    subject = claims.get("sub", "")
    uid = subject.rsplit(":", 1)[-1] if subject else ""
    if not uid:
        raise ValueError("IAP assertion carried no subject")
    return CallerIdentity(uid=uid, email=claims.get("email", ""))


async def current_user(
    authorization: str | None = Header(default=None),
    x_goog_iap_jwt_assertion: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> CallerIdentity:
    """Resolve the caller, or reject the request."""
    if settings.is_local:
        # Local development only; ENVIRONMENT is set by Terraform everywhere else.
        return CallerIdentity(uid="local-dev-user", email="local@example.com")

    # Prefer the Identity Platform token: its uid is the one Firestore matches.
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        try:
            return _verify_firebase(token, settings.project_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("rejected Identity Platform token: %s", exc)

    if x_goog_iap_jwt_assertion and settings.iap_audience:
        try:
            return _verify_iap(x_goog_iap_jwt_assertion, settings.iap_audience)
        except Exception as exc:  # noqa: BLE001
            logger.warning("rejected IAP assertion: %s", exc)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Sign in required.",
        headers={"WWW-Authenticate": "Bearer"},
    )
