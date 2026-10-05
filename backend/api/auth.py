"""
Transfera v2 — Authentication / Authorization helpers
Shared dependency for protecting destructive endpoints with a local secret token.
"""

from __future__ import annotations

from fastapi import Header, HTTPException, Query

from backend.config import LOCAL_SECRET_TOKEN


async def require_local_token(
    x_local_token: str | None = Header(None, alias="X-Local-Token"),
) -> None:
    import hmac as _hmac

    candidate = x_local_token or ""
    if not candidate or not _hmac.compare_digest(candidate, LOCAL_SECRET_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid or missing local token")


async def require_local_token_or_query(
    x_local_token: str | None = Header(None, alias="X-Local-Token"),
    token: str | None = Query(None, description="Local token for <img>-loaded endpoints"),
) -> None:
    """Auth for ``<img src>``-loaded GET endpoints (browsers can't set headers).

    Accepts the local secret via ``?token=`` query param — same pattern as
    the WebSocket ``verify_ws_token``. Scoped to thumbnail endpoints only;
    listings and destructive endpoints stay header-only so tokens don't end
    up in access logs.
    """
    import hmac as _hmac

    candidate = x_local_token or token or ""
    if not candidate or not _hmac.compare_digest(candidate, LOCAL_SECRET_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid or missing local token")


async def verify_ws_token(token: str | None) -> bool:
    """Constant-time check for WebSocket ?token= auth."""
    import hmac as _hmac

    if not token:
        return False
    try:
        return _hmac.compare_digest(token, LOCAL_SECRET_TOKEN)
    except Exception:
        return False
