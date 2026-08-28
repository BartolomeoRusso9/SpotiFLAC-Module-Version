"""Telling a music server that new files arrived.

The Docker/NAS case the project already serves — a headless instance
downloading into a folder something else indexes — has one missing step at
the end: the library server doesn't know anything changed until its own
scheduled scan comes round, which can be hours.

Every one of the three servers below exposes a "rescan now" call. What
differs is only the URL shape and how the credential travels, so this is a
small amount of per-server knowledge and one shared caller.

Credentials
-----------
Tokens come from `--library-token` or `$SPOTIFLAC_LIBRARY_TOKEN`, never from
the config a GUI/web request can supply: the same reasoning as the
post-download shell command (see app.py's POST_COMMAND_ENV). A rescan is
harmless, but the token is not, and a request must not get to choose which
host it is sent to.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from dataclasses import dataclass

from .http import AsyncHttpClient

logger = logging.getLogger(__name__)

LIBRARY_TOKEN_ENV = "SPOTIFLAC_LIBRARY_TOKEN"
LIBRARY_USER_ENV = "SPOTIFLAC_LIBRARY_USER"

SUPPORTED = ("plex", "jellyfin", "emby", "navidrome", "subsonic")


class LibraryNotifyError(RuntimeError):
    """The rescan could not be requested."""


@dataclass(frozen=True)
class LibraryTarget:
    kind: str
    url: str
    token: str
    username: str = ""

    @property
    def base(self) -> str:
        return self.url.rstrip("/")


def resolve_token(explicit: str | None) -> str:
    return (
        explicit if explicit is not None else os.environ.get(LIBRARY_TOKEN_ENV)
    ) or ""


def resolve_username(explicit: str | None) -> str:
    return (
        explicit if explicit is not None else os.environ.get(LIBRARY_USER_ENV)
    ) or ""


def build_target(
    kind: str,
    url: str,
    token: str | None = None,
    username: str | None = None,
) -> LibraryTarget:
    kind = (kind or "").strip().lower()
    if kind not in SUPPORTED:
        msg = f"Unknown library type {kind!r}. Expected one of: {', '.join(SUPPORTED)}"
        raise LibraryNotifyError(msg)
    if not url or not url.strip():
        msg = "A library URL is required (e.g. http://nas.local:8096)"
        raise LibraryNotifyError(msg)

    resolved_token = resolve_token(token)
    if not resolved_token:
        msg = (
            f"No credential for the {kind} rescan. Pass --library-token or set "
            f"${LIBRARY_TOKEN_ENV}."
        )
        raise LibraryNotifyError(msg)

    resolved_user = resolve_username(username)
    if kind in ("navidrome", "subsonic") and not resolved_user:
        msg = (
            "Navidrome/Subsonic needs a username as well as a password: pass "
            f"--library-user or set ${LIBRARY_USER_ENV}."
        )
        raise LibraryNotifyError(msg)

    target = LibraryTarget(kind, url.strip(), resolved_token, resolved_user)

    if target.kind in ("navidrome", "subsonic") and target.base.startswith("http://"):
        # Worth a warning specifically here. Plex and Jellyfin send an API
        # token — revoke it and it's over. Subsonic sends md5(password+salt)
        # derived from the account password itself, so anyone watching a
        # plaintext connection gets something they can grind offline. On a
        # LAN behind a router this is a small risk; over anything else it
        # isn't.
        logger.warning(
            "[library] %s is plain HTTP. Subsonic auth derives from your "
            "account password, so it can be captured and attacked offline — "
            "use https://, or an account you use for nothing else.",
            target.base,
        )

    return target


def _subsonic_auth(password: str) -> dict[str, str]:
    """Subsonic's salted-hash auth: `t = md5(password + salt)`, `s = salt`.

    On the MD5
    ----------
    Static analysis flags this, and it is right that MD5 is weak. It is also
    not a choice available to us: the Subsonic API *specifies* this exact
    construction, and the server computes the same digest to compare. Using
    PBKDF2 or bcrypt here would simply fail to authenticate.

    The alternatives the protocol offers are worse:

      - `p=<password>` — the plaintext password, in the query string, and
        therefore in the server's access log.
      - `p=enc:<hex>` — hex-encoded plaintext, which is the same thing.

    So this is the strongest of the three options a Subsonic server accepts,
    not a preference. What it *does* mean, and what the caller should know:
    the digest is offline-crackable for a weak password, so use a dedicated
    account for SpotiFLAC rather than one whose password you reuse, and
    reach the server over HTTPS (see request_rescan, which warns otherwise).

    If this alert is triaged again: it is accurate about MD5 and wrong about
    the remedy. Dismiss it as "won't fix" against this docstring, or drop
    Subsonic support — those are the two real options.
    """
    salt = secrets.token_hex(8)
    return {
        "t": hashlib.md5(  # noqa: S324 - Subsonic protocol, see docstring
            (password + salt).encode("utf-8")
        ).hexdigest(),
        "s": salt,
    }


def build_request(target: LibraryTarget) -> tuple[str, str, dict, dict]:
    """(method, url, params, headers) for `target`'s rescan endpoint."""
    if target.kind == "plex":
        # Plex refreshes per section; `all` covers every library on the server,
        # which is what someone dropping files into one folder wants.
        return (
            "GET",
            f"{target.base}/library/sections/all/refresh",
            {"X-Plex-Token": target.token},
            {},
        )

    if target.kind in ("jellyfin", "emby"):
        return (
            "POST",
            f"{target.base}/Library/Refresh",
            {},
            {"X-Emby-Token": target.token},
        )

    # Navidrome speaks the Subsonic API, which is also what every other
    # Subsonic-compatible server understands.
    params = {
        "u": target.username,
        "v": "1.16.1",
        "c": "SpotiFLAC",
        "f": "json",
        **_subsonic_auth(target.token),
    }
    return ("GET", f"{target.base}/rest/startScan", params, {})


async def request_rescan(target: LibraryTarget, timeout_s: int = 15) -> bool:
    """Asks `target` to rescan. Returns whether it accepted.

    Never raises: a library server being down must not turn a completed
    download into a failed run. The point of the call is a convenience, and
    the files are on disk either way.
    """
    method, url, params, headers = build_request(target)
    client = AsyncHttpClient(f"library:{target.kind}", timeout_s=timeout_s)
    try:
        if method == "POST":
            await client.post(url, params=params, headers=headers)
        else:
            await client.get(url, params=params, headers=headers)
    except Exception as exc:
        logger.warning(
            "[library] %s rescan request to %s failed: %s",
            target.kind,
            target.base,
            exc,
        )
        return False
    logger.info("[library] Asked %s at %s to rescan", target.kind, target.base)
    return True
