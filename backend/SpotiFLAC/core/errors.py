"""
Gerarchia di errori tipati per SpotiFLAC.
Ispirato al pattern Go: sentinel errors + errors.As/Is.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum, auto


class ErrorKind(Enum):
    AUTH_FAILED      = auto()
    TRACK_NOT_FOUND  = auto()
    RATE_LIMITED     = auto()
    NETWORK_ERROR    = auto()
    PARSE_ERROR      = auto()
    UNAVAILABLE      = auto()
    FILE_IO          = auto()
    INVALID_URL      = auto()
    METADATA_ERROR   = auto()


@dataclass
class SpotiflacError(Exception):
    kind:     ErrorKind
    message:  str
    provider: str = ""
    cause:    BaseException | None = field(default=None, repr=False)

    def __str__(self) -> str:
        prefix = f"[{self.provider}] " if self.provider else ""
        cause_str = f" (caused by: {self.cause})" if self.cause else ""
        return f"{prefix}{self.kind.name}: {self.message}{cause_str}"

    def is_retryable(self) -> bool:
        return self.kind in {ErrorKind.RATE_LIMITED, ErrorKind.NETWORK_ERROR}


class AuthError(SpotiflacError):
    def __init__(self, provider: str, msg: str, cause: BaseException | None = None):
        super().__init__(ErrorKind.AUTH_FAILED, msg, provider, cause)


class TrackNotFoundError(SpotiflacError):
    def __init__(self, provider: str, identifier: str):
        super().__init__(
            ErrorKind.TRACK_NOT_FOUND,
            f"Track not found for: {identifier}",
            provider,
        )


class RateLimitedError(SpotiflacError):
    def __init__(self, provider: str, retry_after: int = 5):
        super().__init__(
            ErrorKind.RATE_LIMITED,
            f"Rate limited — retry after {retry_after}s",
            provider,
        )
        self.retry_after = retry_after


class NetworkError(SpotiflacError):
    def __init__(self, provider: str, msg: str, cause: BaseException | None = None):
        super().__init__(ErrorKind.NETWORK_ERROR, msg, provider, cause)


class ParseError(SpotiflacError):
    def __init__(self, provider: str, msg: str, cause: BaseException | None = None):
        super().__init__(ErrorKind.PARSE_ERROR, msg, provider, cause)


class InvalidUrlError(SpotiflacError):
    def __init__(self, url: str):
        super().__init__(ErrorKind.INVALID_URL, f"Unsupported or invalid URL: {url}")
