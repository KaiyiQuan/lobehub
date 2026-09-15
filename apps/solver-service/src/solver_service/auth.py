"""Bearer-token auth dependency (constant-time comparison)."""

import hmac

from typing import Optional

from fastapi import Header, HTTPException


def make_auth_dependency(settings):
    """Build the auth dependency bound to the given settings.

    Auth is enforced whenever an API key is configured. Without a key the
    service only runs at all when the explicit dev flag was set, and then this
    dependency is a no-op.
    """

    def require_auth(authorization: Optional[str] = Header(default=None)):
        if not settings.auth_enabled:
            return  # dev mode (SOLVER_DEV_ALLOW_NO_AUTH=1), no key configured
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        token = authorization[len("Bearer ") :]
        if not hmac.compare_digest(token.encode("utf-8"), settings.api_key.encode("utf-8")):
            raise HTTPException(status_code=401, detail="invalid bearer token")

    return require_auth
