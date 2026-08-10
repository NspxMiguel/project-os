"""Setup, login, logout, password change.

The only router mounted without :func:`require_auth` on every route -- it has to
be, since it is how you get authenticated in the first place. Each endpoint here
therefore states its own rule explicitly rather than inheriting one.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from project_os import auth
from project_os.db import Database
from project_os.errors import ApiError
from project_os.main import get_config, get_db

log = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

#: Everything here lives under ``/auth`` except first-run setup, which also
#: answers at ``/api/setup``. Claiming the box is the one thing you do before
#: there is any authentication to be under, and the setup screen is the first
#: URL a fresh install ever hits -- so it gets a first-level path too.


class Credentials(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1)


class PasswordChange(BaseModel):
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=8)


@router.get("/auth/status")
async def status(request: Request, db: Database = Depends(get_db)) -> Dict[str, Any]:
    """Public: what the login screen needs before anyone has signed in."""
    user = auth.current_user(request)
    return {
        "setup_required": auth.setup_required(db),
        "auth_enabled": auth.auth_enabled(request),
        "authenticated": user is not None,
        "user": user,
    }


@router.post("/auth/setup", status_code=201)
@router.post("/setup", status_code=201, include_in_schema=False)
async def setup(
    payload: Credentials,
    request: Request,
    response: Response,
    db: Database = Depends(get_db),
    config: Any = Depends(get_config),
) -> Dict[str, Any]:
    """Create the first user and sign them in.

    Only ever works once. After a user exists this is a 409, which is what stops
    an open box from being claimed twice by whoever finds it second.
    """
    if not auth.setup_required(db):
        raise ApiError(409, "already_configured", "project-os already has a user.")
    if len(payload.password) < 8:
        raise ApiError(
            400, "weak_password", "Use at least 8 characters for the first password."
        )
    user = await auth.create_user_async(db, payload.username, payload.password)
    session = auth.create_session(
        db, user["id"], user_agent=request.headers.get("user-agent"), config=config
    )
    auth.set_session_cookie(
        response, session["token"], request=request, max_age=session["max_age"]
    )
    log.info("project-os claimed by user %r", user["username"])
    return {"user": user, "token": session["token"], "expires_at": session["expires_at"]}


@router.post("/auth/login")
async def login(
    payload: Credentials,
    request: Request,
    response: Response,
    db: Database = Depends(get_db),
    config: Any = Depends(get_config),
) -> Dict[str, Any]:
    if auth.setup_required(db):
        raise ApiError(428, "setup_required", "project-os has no user yet.")
    user = await auth.authenticate_async(db, payload.username, payload.password)
    if user is None:
        # One message for both "no such user" and "wrong password": telling them
        # apart is a free list of valid usernames.
        raise ApiError(401, "invalid_credentials", "Wrong username or password.")
    session = auth.create_session(
        db, user["id"], user_agent=request.headers.get("user-agent"), config=config
    )
    auth.set_session_cookie(
        response, session["token"], request=request, max_age=session["max_age"]
    )
    auth.purge_expired_sessions(db)
    return {"user": user, "token": session["token"], "expires_at": session["expires_at"]}


@router.post("/auth/logout")
async def logout(
    request: Request, response: Response, db: Database = Depends(get_db)
) -> Dict[str, Any]:
    """Always succeeds -- logging out of a session that is already gone is fine."""
    token = auth.token_from_request(request)
    if token:
        auth.delete_session(db, token)
    auth.clear_session_cookie(response)
    return {"ok": True}


@router.get("/auth/me")
async def me(user: Dict[str, Any] = Depends(auth.require_auth)) -> Dict[str, Any]:
    return user


@router.post("/auth/password")
async def change_password(
    payload: PasswordChange,
    db: Database = Depends(get_db),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """Change your own password.

    Every session is dropped, including the one making this call -- so the
    browser has to sign in again straight away. That is the point: someone
    changing a password usually suspects the old one leaked, and a caller who
    stayed signed in would be a session minted with the password they are
    trying to invalidate.
    """
    if user.get("anonymous"):
        raise ApiError(400, "no_account", "Authentication is disabled; there is no account.")
    current = await auth.authenticate_async(db, user["username"], payload.current_password)
    if current is None:
        raise ApiError(403, "wrong_password", "The current password is not right.")
    await auth.set_password_async(db, user["id"], payload.new_password)
    auth.delete_user_sessions(db, user["id"])
    log.info("password changed for %r; all sessions revoked", user["username"])
    return {"ok": True, "sessions_revoked": True}


@router.get("/auth/sessions")
async def sessions(
    request: Request,
    db: Database = Depends(get_db),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    if user.get("anonymous"):
        return {"sessions": []}
    mine = auth.token_from_request(request)
    rows = db.query(
        "SELECT token, created_at, expires_at, user_agent FROM sessions "
        "WHERE user_id = ? ORDER BY created_at DESC",
        (user["id"],),
    )
    out = []
    for row in rows:
        item = dict(row)
        # Compared before truncating, and with compare_digest so the answer does
        # not leak through timing. Without this flag the screen cannot tell you
        # which row is the browser you are reading it in -- which is the one
        # thing you need to know before revoking anything.
        item["current"] = bool(mine) and hmac.compare_digest(str(item["token"]), str(mine))
        # Never hand a session token back out: seeing the list must not be a way
        # to steal one of them.
        item["token"] = item["token"][:8] + "..."
        out.append(item)
    return {"sessions": out}


@router.delete("/auth/sessions")
async def revoke_sessions(
    db: Database = Depends(get_db), user: Dict[str, Any] = Depends(auth.require_auth)
) -> Dict[str, Any]:
    if not user.get("anonymous"):
        auth.delete_user_sessions(db, user["id"])
    return {"ok": True}


__all__ = ["router"]
