"""
Transfera v2 — Authentication / Authorization helpers
Shared dependency for protecting destructive endpoints with a local secret token.
"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException

from backend.config import LOCAL_SECRET_TOKEN


async def require_local_token(
    x_local_token: str | None = Header(None, alias="X-Local-Token"),
) -> None:
    if x_local_token != LOCAL_SECRET_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing local token")
