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
"password_hash", "salt", "created_at", "role", "daily_track_quota",
"daily_byte_quota"}]}. Sessions are kept in memory only (a restart logs
everyone out — acceptable for what this is: a small self-hosted instance, not
a service with an uptime SLA).

Roles and quotas
----------------
Accounts used to be entirely flat: everyone could do the same things, and
/api/metrics hid instance-wide counters in multi-user mode precisely because
there was nobody to show them to (see webapp.py). There are now two roles —
`user` and `admin` — and an admin is simply an account that may see and
manage the instance rather than only its own corner of it.

Quotas are a limit on *tracks and bytes per rolling day*, counted from
`core/download_log.py` rather than tracked separately, so the number a user
is refused on is the same number `/api/v1/history` shows them. Zero means
unlimited, which is the default and preserves today's behaviour exactly for
anyone who never sets one.

Both fields are absent from existing files, so `_load()` supplies defaults
and nothing has to be migrated.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

from .atomic_io import write_private_json

logger = logging.getLogger(__name__)

USERS_FILE = Path.home() / ".spotiflac" / "web_users.json"

_PBKDF2_ITERATIONS = 600_000  # OWASP's 2023 recommendation for PBKDF2-SHA256
_SESSION_TTL_S = 7 * 24 * 3600  # 7 days

# Fixed, non-secret: only ever used to spend the same CPU on a login for an
# account that doesn't exist as on one that does. See verify_password().
_DUMMY_SALT = b"\x00" * 16

ROLES = ("user", "admin")

#: A rolling window rather than a calendar day: "resets at midnight" needs a
#: timezone to be meaningful, and a self-hosted instance and the person using
#: it are not always in the same one.
QUOTA_WINDOW_S = 24 * 3600


class WebUserError(ValueError):
    pass


class QuotaExceededError(RuntimeError):
    """An account is over its daily allowance.

    Carries the numbers as attributes as well as in the message, for the same
    reason job_queue.QueueFullError does: an HTTP response should be built
    from the fields, not from str(exc).
    """

    def __init__(
        self, message: str, used: int = 0, limit: int = 0, unit: str = "tracks"
    ) -> None:
        super().__init__(message)
        self.used = used
        self.limit = limit
        self.unit = unit


@dataclass(frozen=True)
class WebUser:
    username: str
    password_hash: str
    salt: str
    created_at: int
    role: str = "user"
    #: 0 = unlimited, for both.
    daily_track_quota: int = 0
    daily_byte_quota: int = 0

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def to_public_dict(self) -> dict:
        """Everything about an account except its credential material."""
        return {
            "username": self.username,
            "role": self.role,
            "created_at": self.created_at,
            "daily_track_quota": self.daily_track_quota,
            "daily_byte_quota": self.daily_byte_quota,
        }


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
                    username=u["username"],
                    password_hash=u["password_hash"],
                    salt=u["salt"],
                    created_at=u.get("created_at", 0),
                    # Absent in files written before roles existed. Defaulting
                    # to "user"/unlimited keeps an upgraded instance behaving
                    # exactly as it did.
                    role=u.get("role", "user"),
                    daily_track_quota=int(u.get("daily_track_quota", 0) or 0),
                    daily_byte_quota=int(u.get("daily_byte_quota", 0) or 0),
                )
                for u in data.get("users", [])
            ]
    except Exception as e:
        logger.warning("[WebUsers] Unable to read %s: %s", USERS_FILE, e)
    return []


def _save(users: list[WebUser]) -> None:
    payload = {
        "users": [
            {
                "username": u.username,
                "password_hash": u.password_hash,
                "salt": u.salt,
                "created_at": u.created_at,
                "role": u.role,
                "daily_track_quota": u.daily_track_quota,
                "daily_byte_quota": u.daily_byte_quota,
            }
            for u in users
        ]
    }
    write_private_json(USERS_FILE, payload)


def has_any_users() -> bool:
    """Whether multi-user mode has actually been set up — webapp.py uses
    this to decide whether to even offer a login screen.
    """
    return bool(_load())


def list_usernames() -> list[str]:
    return [u.username for u in _load()]


def create_user(
    username: str,
    password: str,
    *,
    role: str = "user",
    daily_track_quota: int = 0,
    daily_byte_quota: int = 0,
) -> None:
    username = (username or "").strip()
    if not username:
        raise WebUserError("Username cannot be empty")
    if not password:
        raise WebUserError("Password cannot be empty")
    if role not in ROLES:
        raise WebUserError(f"Unknown role '{role}'. Expected: {', '.join(ROLES)}")

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
            role=role,
            daily_track_quota=max(0, int(daily_track_quota)),
            daily_byte_quota=max(0, int(daily_byte_quota)),
        )
    )
    _save(users)


def get_user(username: str) -> WebUser | None:
    return next((u for u in _load() if u.username == username), None)


def list_users() -> list[dict]:
    """Every account, without credential material — see WebUser.to_public_dict."""
    return [u.to_public_dict() for u in _load()]


def is_admin(username: str | None) -> bool:
    if not username:
        return False
    user = get_user(username)
    return user is not None and user.is_admin


def has_admin() -> bool:
    return any(u.is_admin for u in _load())


def set_role(username: str, role: str) -> bool:
    if role not in ROLES:
        raise WebUserError(f"Unknown role '{role}'. Expected: {', '.join(ROLES)}")
    users = _load()
    for i, u in enumerate(users):
        if u.username != username:
            continue
        if u.is_admin and role != "admin" and sum(1 for x in users if x.is_admin) == 1:
            # Refusing to demote the last admin: the alternative is an
            # instance nobody can administer, recoverable only by editing
            # web_users.json by hand.
            raise WebUserError(
                f"'{username}' is the only admin; promote another account first."
            )
        users[i] = replace(u, role=role)
        _save(users)
        return True
    return False


def set_quota(
    username: str,
    *,
    daily_track_quota: int | None = None,
    daily_byte_quota: int | None = None,
) -> bool:
    """Sets either limit. 0 means unlimited; None leaves that limit alone."""
    users = _load()
    for i, u in enumerate(users):
        if u.username != username:
            continue
        users[i] = replace(
            u,
            daily_track_quota=(
                u.daily_track_quota
                if daily_track_quota is None
                else max(0, int(daily_track_quota))
            ),
            daily_byte_quota=(
                u.daily_byte_quota
                if daily_byte_quota is None
                else max(0, int(daily_byte_quota))
            ),
        )
        _save(users)
        return True
    return False


# ─────────────────────────────────────────────────────────────
#  Quotas
# ─────────────────────────────────────────────────────────────


def quota_usage(username: str) -> dict:
    """What this account has used in the last rolling day, and its limits.

    Counted from core/download_log.py rather than from a counter of its own:
    one source of truth means the number someone is refused on is the same
    number their history shows.
    """
    from . import download_log

    user = get_user(username)
    since = time.time() - QUOTA_WINDOW_S
    tracks = download_log.count_since(username, since)
    used_bytes = download_log.bytes_since(username, since)
    return {
        "username": username,
        "tracks_used": tracks,
        "tracks_limit": user.daily_track_quota if user else 0,
        "bytes_used": used_bytes,
        "bytes_limit": user.daily_byte_quota if user else 0,
        "window_hours": QUOTA_WINDOW_S / 3600,
    }


def check_quota(username: str) -> None:
    """Raises QuotaExceededError if `username` is over either limit.

    A no-op for an account with no quota set (the default), and for an
    unknown username — enforcement is not the place to decide whether
    somebody may log in.
    """
    user = get_user(username)
    if user is None:
        return
    if not user.daily_track_quota and not user.daily_byte_quota:
        return

    usage = quota_usage(username)

    if user.daily_track_quota and usage["tracks_used"] >= user.daily_track_quota:
        raise QuotaExceededError(
            f"Daily track quota reached ({usage['tracks_used']}/"
            f"{user.daily_track_quota}). It frees up as downloads age past "
            f"{int(QUOTA_WINDOW_S / 3600)}h.",
            used=usage["tracks_used"],
            limit=user.daily_track_quota,
            unit="tracks",
        )

    if user.daily_byte_quota and usage["bytes_used"] >= user.daily_byte_quota:
        raise QuotaExceededError(
            f"Daily size quota reached ({usage['bytes_used']}/"
            f"{user.daily_byte_quota} bytes).",
            used=usage["bytes_used"],
            limit=user.daily_byte_quota,
            unit="bytes",
        )


def delete_user(username: str) -> bool:
    users = _load()
    remaining = [u for u in users if u.username != username]
    if len(remaining) == len(users):
        return False
    _save(remaining)
    return True


def verify_password(username: str, password: str) -> bool:
    """Whether `password` is correct for `username`.

    The hash comparison is constant-time (secrets.compare_digest over two
    equal-length hex digests), but that only hides *which* password was
    wrong. Hiding *whether the account exists* takes the dummy hash below:
    returning early for an unknown username answered in microseconds, while
    a known one took 600k PBKDF2 iterations first — a difference any client
    can measure, turning the login form into a "does this user exist?"
    oracle for enumerating accounts before attacking them.
    """
    match = next((u for u in _load() if u.username == username), None)

    if match is None:
        # Same work, discarded. The salt is fixed and the result is unused;
        # it exists purely so both branches cost the same.
        _hash_password(password, _DUMMY_SALT)
        return False

    candidate = _hash_password(password, bytes.fromhex(match.salt))
    return secrets.compare_digest(candidate, match.password_hash)


def change_password(username: str, new_password: str) -> bool:
    if not new_password:
        raise WebUserError("Password cannot be empty")
    users = _load()
    for i, u in enumerate(users):
        if u.username == username:
            salt = secrets.token_bytes(16)
            # replace(), not a fresh WebUser: rebuilding the record field by
            # field silently reset everything the constructor was not told
            # about — which now includes the account's role and quotas, so
            # changing a password would have demoted an admin. Any field
            # added later is carried over by this for free.
            users[i] = replace(
                u,
                password_hash=_hash_password(new_password, salt),
                salt=salt.hex(),
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
