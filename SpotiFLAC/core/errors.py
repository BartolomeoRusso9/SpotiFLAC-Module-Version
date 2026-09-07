"""Typed error hierarchy for SpotiFLAC.
Inspired by the Go pattern: sentinel errors + errors.As/Is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto


class ErrorKind(Enum):
    AUTH_FAILED = auto()
    TRACK_NOT_FOUND = auto()
    RATE_LIMITED = auto()
    NETWORK_ERROR = auto()
    PARSE_ERROR = auto()
    UNAVAILABLE = auto()
    FILE_IO = auto()
    INVALID_URL = auto()
    METADATA_ERROR = auto()


@dataclass
class SpotiflacError(Exception):
    kind: ErrorKind
    message: str
    provider: str = ""
    cause: BaseException | None = field(default=None, repr=False)

    def __str__(self) -> str:
        prefix = f"[{self.provider}] " if self.provider else ""
        cause_str = f" (caused by: {self.cause})" if self.cause else ""
        return f"{prefix}{self.kind.name}: {self.message}{cause_str}"

    def is_retryable(self) -> bool:
        return self.kind in {ErrorKind.RATE_LIMITED, ErrorKind.NETWORK_ERROR}


class AuthError(SpotiflacError):
    def __init__(
        self,
        provider: str,
        msg: str,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(ErrorKind.AUTH_FAILED, msg, provider, cause)


class TrackNotFoundError(SpotiflacError):
    def __init__(self, provider: str, identifier: str) -> None:
        super().__init__(
            ErrorKind.TRACK_NOT_FOUND,
            f"Track not found for: {identifier}",
            provider,
        )


class RateLimitedError(SpotiflacError):
    def __init__(self, provider: str, retry_after: int = 5) -> None:
        super().__init__(
            ErrorKind.RATE_LIMITED,
            f"Rate limited — retry after {retry_after}s",
            provider,
        )
        self.retry_after = retry_after


class NetworkError(SpotiflacError):
    def __init__(
        self,
        provider: str,
        msg: str,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(ErrorKind.NETWORK_ERROR, msg, provider, cause)


class RedirectNotFollowedError(NetworkError):
    """A 3xx arrived at a caller that did not ask to follow redirects.

    A `NetworkError` subclass so every existing handler keeps working, but
    its own type so the retry logic can tell it apart. Retrying a redirect
    is the one retry guaranteed to be pointless: the server will answer with
    the same `Location` every time, and three round trips buy nothing.

    Worth its own class rather than a message check because a 3xx is often
    not a failure at all — it is how a lookup endpoint answers. Songstats'
    ISRC lookup is exactly that, and reading its 301 as a network fault is
    what kept an entire fallback path dead. When this is raised, the fix is
    almost always `follow_redirects=True` at the call site, so the message
    says where the server was pointing.
    """

    def __init__(
        self,
        provider: str,
        status: int,
        url: str,
        location: str = "",
        cause: BaseException | None = None,
    ) -> None:
        target = f" -> {location}" if location else ""
        super().__init__(
            provider,
            f"HTTP {status} redirect from {url}{target} "
            f"(the caller did not pass follow_redirects=True)",
            cause,
        )
        self.status = status
        self.location = location


class ParseError(SpotiflacError):
    def __init__(
        self,
        provider: str,
        msg: str,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(ErrorKind.PARSE_ERROR, msg, provider, cause)


class InvalidUrlError(SpotiflacError):
    def __init__(self, url: str) -> None:
        super().__init__(ErrorKind.INVALID_URL, f"Unsupported or invalid URL: {url}")
