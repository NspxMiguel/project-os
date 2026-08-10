"""Users, sessions and the ``require_auth`` dependency.

Password hashes are ``pbkdf2_sha256$200000$<salt_hex>$<hash_hex>``; sessions are
opaque 32-byte URL-safe tokens carried by the ``project_os_session`` cookie or an
``Authorization: Bearer`` header.

With no user in the database the box is in *setup mode*: everything guarded by
:func:`require_auth` answers ``428 {"error": "setup_required"}`` so the frontend
can send the user to the setup screen from any deep link.
"""

from __future__ import annotations

import asyncio
import binascii
import hashlib
import hmac
import logging
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import Request, Response

from project_os.errors import ApiError

log = logging.getLogger(__name__)

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 200000
SALT_BYTES = 16
TOKEN_BYTES = 32
SESSION_COOKIE = "project_os_session"
DEFAULT_SESSION_TTL_HOURS = 720
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}$")

ANONYMOUS_USER = {
    "id": 0,
    "username": "anonymous",
    "created_at": None,
    "anonymous": True,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ---------------------------------------------------------------------------
# passwords
# ---------------------------------------------------------------------------
def hash_password(password: str, salt: Optional[bytes] = None, iterations: int = ITERATIONS) -> str:
    if not isinstance(password, str) or password == "":
        raise ApiError(400, "invalid_password", "The password cannot be empty.")
    if salt is None:
        salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "%s$%d$%s$%s" % (
        ALGORITHM,
        iterations,
        binascii.hexlify(salt).decode("ascii"),
        binascii.hexlify(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: Optional[str]) -> bool:
    if not password or not encoded:
        return False
    parts = encoded.split("$")
    if len(parts) != 4:
        return False
    algorithm, raw_iterations, salt_hex, hash_hex = parts
    if algorithm != ALGORITHM:
        return False
    try:
        iterations = int(raw_iterations)
        salt = binascii.unhexlify(salt_hex)
        expected = binascii.unhexlify(hash_hex)
    except (ValueError, binascii.Error):
        return False
    if iterations <= 0:
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(candidate, expected)


async def hash_password_async(password: str) -> str:
    """200k PBKDF2 rounds take real time on a Pi -- keep them off the loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, hash_password, password)


async def verify_password_async(password: str, encoded: Optional[str]) -> bool:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, verify_password, password, encoded)


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------
def _public_user(row: Any) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    data = dict(row)
    data.pop("password_hash", None)
    return data


def user_count(db: Any) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM users", (), 0) or 0)


def setup_required(db: Any) -> bool:
    """True while no user exists (the box is unclaimed)."""
    try:
        return user_count(db) == 0
    except sqlite3.Error as exc:  # pragma: no cover - unmigrated database
        log.warning("cannot count users: %s", exc)
        return True


def get_user(db: Any, user_id: int) -> Optional[Dict[str, Any]]:
    return _public_user(db.one("SELECT * FROM users WHERE id = ?", (user_id,)))


def get_user_by_name(db: Any, username: str) -> Optional[Dict[str, Any]]:
    row = db.one("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,))
    return _public_user(row)


def list_users(db: Any) -> List[Dict[str, Any]]:
    rows = db.query("SELECT * FROM users ORDER BY id")
    return [_public_user(row) for row in rows]


def create_user(db: Any, username: str, password: str) -> Dict[str, Any]:
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise ApiError(
            400,
            "invalid_username",
            "Use 1-64 characters: letters, digits and . _ @ + - only.",
        )
    if not password:
        raise ApiError(400, "invalid_password", "The password cannot be empty.")
    if db.one("SELECT id FROM users WHERE username = ? COLLATE NOCASE", (username,)):
        raise ApiError(409, "user_exists", "That username is already taken.")
    now = _iso(_utcnow())
    cursor = db.execute(
        "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
        (username, hash_password(password), now),
    )
    return {"id": int(cursor.lastrowid), "username": username, "created_at": now}


def set_password(db: Any, user_id: int, password: str) -> None:
    if not password:
        raise ApiError(400, "invalid_password", "The password cannot be empty.")
    db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user_id)
    )


def authenticate(db: Any, username: str, password: str) -> Optional[Dict[str, Any]]:
    row = db.one(
        "SELECT * FROM users WHERE username = ? COLLATE NOCASE", ((username or "").strip(),)
    )
    if row is None:
        # Spend roughly the same time as a real verification.
        verify_password(password or "", hash_password("project_os-timing-equalizer"))
        return None
    if not verify_password(password or "", row["password_hash"]):
        return None
    return _public_user(row)


async def authenticate_async(db: Any, username: str, password: str) -> Optional[Dict[str, Any]]:
    """Login without stalling the box: PBKDF2 costs ~0.2 s on a Pi 3B."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, authenticate, db, username, password)


async def create_user_async(db: Any, username: str, password: str) -> Dict[str, Any]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, create_user, db, username, password)


async def set_password_async(db: Any, user_id: int, password: str) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, set_password, db, user_id, password)


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------
def _ttl_hours(config: Any) -> int:
    if config is None:
        return DEFAULT_SESSION_TTL_HOURS
    try:
        return int(config.get("security.session_ttl_hours", DEFAULT_SESSION_TTL_HOURS))
    except (TypeError, ValueError):
        return DEFAULT_SESSION_TTL_HOURS


def create_session(
    db: Any,
    user_id: int,
    ttl_hours: Optional[int] = None,
    user_agent: Optional[str] = None,
    config: Any = None,
) -> Dict[str, Any]:
    hours = int(ttl_hours if ttl_hours is not None else _ttl_hours(config))
    now = _utcnow()
    expires = now + timedelta(hours=max(hours, 1))
    token = secrets.token_urlsafe(TOKEN_BYTES)
    db.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at, user_agent) "
        "VALUES (?, ?, ?, ?, ?)",
        (token, int(user_id), _iso(now), _iso(expires), (user_agent or "")[:256]),
    )
    return {
        "token": token,
        "user_id": int(user_id),
        "created_at": _iso(now),
        "expires_at": _iso(expires),
        "max_age": int((expires - now).total_seconds()),
    }


def delete_session(db: Any, token: str) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token = ?", (token,))


def delete_user_sessions(db: Any, user_id: int) -> None:
    db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def purge_expired_sessions(db: Any) -> int:
    cursor = db.execute("DELETE FROM sessions WHERE expires_at < ?", (_iso(_utcnow()),))
    return int(cursor.rowcount or 0)


def get_session(db: Any, token: str) -> Optional[Dict[str, Any]]:
    """The session row, or ``None``. Expired rows are deleted on the way out."""
    if not token:
        return None
    row = db.one("SELECT * FROM sessions WHERE token = ?", (token,))
    if row is None:
        return None
    expires = _parse_iso(row["expires_at"])
    if expires is None or expires <= _utcnow():
        delete_session(db, token)
        return None
    return dict(row)


def session_user(db: Any, token: str) -> Optional[Dict[str, Any]]:
    session = get_session(db, token)
    if session is None:
        return None
    user = get_user(db, session["user_id"])
    if user is None:
        delete_session(db, token)
        return None
    user["session_expires_at"] = session["expires_at"]
    return user


# ---------------------------------------------------------------------------
# request plumbing
# ---------------------------------------------------------------------------
def token_from_request(request: Request) -> Optional[str]:
    header = request.headers.get("authorization") or ""
    if header[:7].lower() == "bearer ":
        token = header[7:].strip()
        if token:
            return token
    cookie = request.cookies.get(SESSION_COOKIE)
    return cookie or None


def _state(request: Request, name: str) -> Any:
    return getattr(request.app.state, name, None)


def _require_db(request: Request) -> Any:
    db = _state(request, "db")
    if db is None:
        raise ApiError(503, "not_ready", "The database is not open yet.")
    return db


def _config(request: Request) -> Any:
    config = _state(request, "config")
    if config is not None:
        return config
    from project_os.config import get_config  # local import: avoids a cycle at import time

    return get_config()


def auth_enabled(request: Request) -> bool:
    config = _config(request)
    try:
        return bool(config.get("security.auth_enabled", True))
    except Exception:  # pragma: no cover - a broken config must not lock the box open
        return True


def anonymous_user() -> Dict[str, Any]:
    return dict(ANONYMOUS_USER)


def current_user(request: Request) -> Optional[Dict[str, Any]]:
    """The signed-in user, or ``None``. Never raises."""
    cached = getattr(request.state, "user", None)
    if cached is not None:
        return cached
    db = _state(request, "db")
    if db is None:
        return None
    token = token_from_request(request)
    if not token:
        return None
    try:
        user = session_user(db, token)
    except sqlite3.Error as exc:  # pragma: no cover
        log.warning("session lookup failed: %s", exc)
        return None
    if user is not None:
        request.state.user = user
    return user


async def require_auth(request: Request) -> Dict[str, Any]:
    """FastAPI dependency: a real user, or the right error.

    * ``security.auth_enabled: false`` -> a synthetic anonymous user
    * no user in the database -> ``428 setup_required``
    * no/expired session -> ``401 unauthenticated``
    """
    if not auth_enabled(request):
        user = anonymous_user()
        request.state.user = user
        return user
    db = _require_db(request)
    if setup_required(db):
        raise ApiError(
            428, "setup_required", "project-os has no user yet. Finish setup first."
        )
    user = current_user(request)
    if user is None:
        raise ApiError(401, "unauthenticated", "Sign in to continue.")
    return user


async def optional_auth(request: Request) -> Optional[Dict[str, Any]]:
    """Like :func:`require_auth` but never raises -- for endpoints that adapt."""
    if not auth_enabled(request):
        return anonymous_user()
    return current_user(request)


# ---------------------------------------------------------------------------
# cookies
# ---------------------------------------------------------------------------
def is_secure_request(request: Optional[Request]) -> bool:
    if request is None:
        return False
    scheme = request.url.scheme
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return scheme == "https" or forwarded == "https"


def set_session_cookie(
    response: Response,
    token: str,
    request: Optional[Request] = None,
    max_age: Optional[int] = None,
    secure: Optional[bool] = None,
) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=is_secure_request(request) if secure is None else bool(secure),
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", samesite="lax")


__all__ = [
    "ALGORITHM",
    "ANONYMOUS_USER",
    "ITERATIONS",
    "SESSION_COOKIE",
    "anonymous_user",
    "auth_enabled",
    "authenticate",
    "authenticate_async",
    "clear_session_cookie",
    "create_session",
    "create_user",
    "create_user_async",
    "current_user",
    "delete_session",
    "delete_user_sessions",
    "get_session",
    "get_user",
    "get_user_by_name",
    "hash_password",
    "hash_password_async",
    "is_secure_request",
    "list_users",
    "optional_auth",
    "purge_expired_sessions",
    "require_auth",
    "session_user",
    "set_password",
    "set_password_async",
    "set_session_cookie",
    "setup_required",
    "token_from_request",
    "user_count",
    "verify_password",
    "verify_password_async",
]
