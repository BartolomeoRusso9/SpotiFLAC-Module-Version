"""core/web_users.py — optional multi-user accounts for `--web` mode.

Off by default: `--web` with no users configured behaves exactly as it
always has — one shared, unauthenticated (or --web-token-gated) instance.
Multi-user is opt-in (`--web-multiuser`, see webapp.py) and additive: it
layers per-user login on top of the existing single-instance app rather
than replacing it, so a solo/home user who never touches this sees no
difference at all.

Passwords are hashed with PBKDF2-HMAC-SHA256 (stdlib `hashlib`, no new
dependency) and a random per-user salt — never stored or compared in
plaintext. Session tokens are opaque random strings (`secrets.token_urlsafe`)
held server-side with an expiry; nothing here is a JWT or otherwise
self-verifying, by design, so a token can always be revoked by deleting it
from the store.

Storage: ~/.spotiflac/web_users.json — {"users": [{"username",
"password_hash", "salt", "created_at"}]}. Sessions are kept in memory only
(a restart logs everyone out — acceptable for what this is: a small
self-hosted instance, not a service with an uptime SLA).
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

USERS_FILE = Path.home() / ".spotiflac" / "web_users.json"

_PBKDF2_ITERATIONS = 600_000  # OWASP's 2023 recommendation for PBKDF2-SHA256
_SESSION_TTL_S = 7 * 24 * 3600  # 7 days


class WebUserError(ValueError):
    pass


@dataclass(frozen=True)
class WebUser:
    username: str
    password_hash: str
    salt: str
    created_at: int


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    ).hex()


def _load() -> list[WebUser]:
    try:
        if USERS_FILE.exists():
            data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
            return [
                WebUser(
                    u["username"], u["password_hash"], u["salt"], u.get("created_at", 0)
                )
                for u in data.get("users", [])
            ]
    except Exception as e:
        logger.warning("[WebUsers] Unable to read %s: %s", USERS_FILE, e)
    return []


def _save(users: list[WebUser]) -> None:
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "users": [
            {
                "username": u.username,
                "password_hash": u.password_hash,
                "salt": u.salt,
                "created_at": u.created_at,
            }
            for u in users
        ]
    }
    USERS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def has_any_users() -> bool:
    """Whether multi-user mode has actually been set up — webapp.py uses
    this to decide whether to even offer a login screen.
    """
    return bool(_load())


def list_usernames() -> list[str]:
    return [u.username for u in _load()]


def create_user(username: str, password: str) -> None:
    username = (username or "").strip()
    if not username:
        raise WebUserError("Username cannot be empty")
    if not password:
        raise WebUserError("Password cannot be empty")

    users = _load()
    if any(u.username == username for u in users):
        raise WebUserError(f"User '{username}' already exists")

    salt = secrets.token_bytes(16)
    users.append(
        WebUser(
            username=username,
            password_hash=_hash_password(password, salt),
            salt=salt.hex(),
            created_at=int(time.time()),
        )
    )
    _save(users)


def delete_user(username: str) -> bool:
    users = _load()
    remaining = [u for u in users if u.username != username]
    if len(remaining) == len(users):
        return False
    _save(remaining)
    return True


def verify_password(username: str, password: str) -> bool:
    """Constant-time-safe by construction: hashlib.pbkdf2_hmac + comparing
    two hex digests of equal, fixed length via secrets.compare_digest.
    """
    for u in _load():
        if u.username != username:
            continue
        candidate = _hash_password(password, bytes.fromhex(u.salt))
        return secrets.compare_digest(candidate, u.password_hash)
    return False


def change_password(username: str, new_password: str) -> bool:
    if not new_password:
        raise WebUserError("Password cannot be empty")
    users = _load()
    for i, u in enumerate(users):
        if u.username == username:
            salt = secrets.token_bytes(16)
            users[i] = WebUser(
                username=username,
                password_hash=_hash_password(new_password, salt),
                salt=salt.hex(),
                created_at=u.created_at,
            )
            _save(users)
            return True
    return False


# ─────────────────────────────────────────────────────────────
#  Sessions (in-memory only — see module docstring)
# ─────────────────────────────────────────────────────────────


class SessionStore:
    """Thread-safe in-memory session-token store. One instance per running
    `--web` process (created by webapp.py's create_app()), not a module
    singleton, so tests (and multiple servers in one process) don't share
    state.
    """

    def __init__(self, ttl_s: int = _SESSION_TTL_S) -> None:
        self._ttl_s = ttl_s
        self._sessions: dict[str, tuple[str, float]] = {}  # token -> (username, expiry)
        self._lock = threading.Lock()

    def create(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = (username, time.time() + self._ttl_s)
        return token

    def username_for(self, token: str | None) -> str | None:
        if not token:
            return None
        with self._lock:
            entry = self._sessions.get(token)
            if entry is None:
                return None
            username, expiry = entry
            if time.time() > expiry:
                del self._sessions[token]
                return None
            return username

    def revoke(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)
