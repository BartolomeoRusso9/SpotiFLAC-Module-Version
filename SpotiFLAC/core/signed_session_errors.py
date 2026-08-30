"""SpotiFLAC/core/signed_session_errors.py — whose 401 was that?

A signed-session request goes through a gateway to a provider, and either
of them can answer 401 or 403. They mean opposite things:

  gateway 401   the session is no longer valid — re-bootstrap, and if that
                is not enough, ask the user to pass verification again
  provider 401  the *provider* refused this particular track: no
                subscription, region-locked, an expired token on their
                side. The session is fine.

The client used to treat both as the first case and throw the session away
on any 401, which means a single subscription-only track costs the user a
Turnstile challenge they did not need. That is the failure this module
exists to prevent — a provider response must not be able to masquerade as a
session failure merely by returning 401 upstream.

The gateway distinguishes them itself, in a JSON error envelope alongside
the status code. Ported from the mobile app's extension_signed_session.go,
whose contract is what the gateway actually emits.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

#: The session is gone and can be re-established without the user.
ACTION_BOOTSTRAP = "bootstrap_session"

#: The user has to pass a challenge before anything else will work.
ACTION_VERIFY = "verify"


@dataclass(frozen=True)
class SessionError:
    """The gateway's error envelope, normalised."""

    error: str = ""
    code: str = ""
    origin: str = ""
    action: str = ""
    retryable: bool = False
    retry_mode: str = ""
    retry_after_seconds: int = 0

    #: False when the response carried no envelope at all, which is what
    #: separates "the gateway told us whose fault this is" from "we have
    #: only a status code to go on".
    present: bool = False

    @property
    def from_gateway(self) -> bool:
        return self.origin == "gateway"

    @property
    def from_provider(self) -> bool:
        return self.origin == "provider"


def parse_session_error(body: Any) -> SessionError:
    """Reads the envelope out of a response body. Never raises.

    Accepts the parsed dict, raw bytes or a string, because callers have it
    in different forms depending on how far the response got.
    """
    data: Any = body
    if isinstance(body, (bytes, bytearray)):
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except Exception:
            return SessionError()
    elif isinstance(body, str):
        try:
            data = json.loads(body)
        except Exception:
            return SessionError()

    if not isinstance(data, dict):
        return SessionError()

    code = str(data.get("code") or "").strip().upper()
    origin = str(data.get("origin") or "").strip().lower()
    action = str(data.get("action") or "").strip().lower()
    try:
        retry_after = max(0, int(data.get("retry_after_seconds") or 0))
    except (TypeError, ValueError):
        retry_after = 0

    return SessionError(
        error=str(data.get("error") or "").strip(),
        code=code,
        origin=origin,
        action=action,
        retryable=bool(data.get("retryable")),
        retry_mode=str(data.get("retry_mode") or "").strip().lower(),
        retry_after_seconds=retry_after,
        # An envelope with none of these fields is not an envelope: some
        # gateways answer errors with an unrelated JSON body.
        present=bool(code or origin or action),
    )


def gateway_action(status_code: int, err: SessionError) -> str:
    """What the *session* should do about this response: "", "bootstrap_session"
    or "verify".

    Returns "" for anything the gateway did not explicitly attribute to
    itself, which is the whole point: a provider's 401 leaves the session
    alone.

    When the response carried no envelope the old status-based behaviour is
    kept. Being strict there would be the safer-looking choice and the wrong
    one: a gateway that has not adopted the contract would never be able to
    tell us the session died, and re-authentication would break entirely.
    Narrowing this is a decision to make once the envelope is known to be
    universal, not by guessing.
    """
    if not err.present:
        if status_code == 401:
            return ACTION_BOOTSTRAP
        if status_code == 428:
            return ACTION_VERIFY
        return ""

    if not err.from_gateway:
        return ""

    if status_code == 401 and err.code == "SESSION_INVALID":
        return ACTION_BOOTSTRAP
    if status_code == 428 and err.code == "VERIFY_REQUIRED":
        return ACTION_VERIFY
    return ""


def should_clear_session(status_code: int, body: Any) -> bool:
    """Whether this response means the stored session is worthless.

    The one call most of the client needs.
    """
    return gateway_action(status_code, parse_session_error(body)) != ""


def provider_retry_after(status_code: int, err: SessionError) -> int:
    """Seconds to wait before retrying the same operation, or 0.

    Only for a provider that declared itself temporarily unavailable and
    asked to be retried — never a reason to touch the session.
    """
    if (
        status_code == 503
        and err.from_provider
        and err.code == "PROVIDER_UNAVAILABLE"
        and err.retryable
        and err.retry_mode == "same_operation"
    ):
        return err.retry_after_seconds
    return 0
