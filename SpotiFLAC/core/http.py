"""
HTTP client centralizzato con retry esponenziale e timeout.
Ogni provider riceve un'istanza configurata — zero `requests.get` raw in giro.
"""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import requests
from requests import Response, Session

from .errors import (
    AuthError, RateLimitedError, NetworkError,
    ParseError, TrackNotFoundError, SpotiflacError,
)

logger = logging.getLogger(__name__)


@dataclass
class RetryConfig:
    max_attempts:   int   = 3
    base_delay_s:   float = 1.0
    max_delay_s:    float = 30.0
    backoff_factor: float = 2.0


class HttpClient:
    """
    Wrapper attorno a requests.Session con:
    - timeout configurabile
    - retry esponenziale automatico
    - parsing sicuro della risposta JSON
    - mapping HTTP status → errori tipati
    """

    def __init__(
        self,
        provider:    str,
        timeout_s:   int            = 30,
        retry:       RetryConfig | None = None,
        headers:     dict[str, str] | None = None,
        session:     Session | None    = None,
    ) -> None:
        self._provider = provider
        self._timeout  = timeout_s
        self._retry    = retry or RetryConfig()
        self._session  = session or Session()
        if headers:
            self._session.headers.update(headers)

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def get(self, url: str, **kwargs: Any) -> Response:
        """GET con retry automatico."""
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Response:
        """POST con retry automatico."""
        return self._request("POST", url, **kwargs)

    def get_json(self, url: str, **kwargs: Any) -> dict:
        """GET + parse JSON sicuro."""
        return self._parse_json(self.get(url, **kwargs))

    def post_json(self, url: str, **kwargs: Any) -> dict:
        """POST + parse JSON sicuro."""
        return self._parse_json(self.post(url, **kwargs))

    def stream_to_file(
        self,
        url:        str,
        dest_path:  str,
        progress_cb: Any = None,   # Callable[[int, int], None] | None
        chunk_size: int = 256 * 1024,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        """
        Scarica in streaming verso un file .part, poi rinomina atomicamente.
        progress_cb(downloaded_bytes, total_bytes) viene chiamata ad ogni chunk.
        """
        import os
        temp = dest_path + ".part"
        req_kwargs: dict[str, Any] = {"stream": True, "timeout": (self._timeout, 300)}
        if extra_headers:
            req_kwargs["headers"] = extra_headers

        try:
            with self._session.get(url, **req_kwargs) as resp:
                self._raise_for_status(resp)
                total = int(resp.headers.get("Content-Length") or 0)
                downloaded = 0
                os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
                with open(temp, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=chunk_size):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if progress_cb:
                                progress_cb(downloaded, total)
            os.replace(temp, dest_path)
        except SpotiflacError:
            raise
        except Exception as exc:
            raise NetworkError(self._provider, f"Stream download failed: {exc}", exc)
        finally:
            if os.path.exists(temp):
                try:
                    os.remove(temp)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _request(self, method: str, url: str, **kwargs: Any) -> Response:
        kwargs.setdefault("timeout", self._timeout)
        last_err: SpotiflacError | None = None
        delay = self._retry.base_delay_s

        for attempt in range(1, self._retry.max_attempts + 1):
            try:
                resp = self._session.request(method, url, **kwargs)
                self._raise_for_status(resp)
                return resp

            except RateLimitedError as exc:
                last_err = exc
                wait = getattr(exc, "retry_after", delay)
                logger.warning("[%s] Rate limited — sleeping %ss (attempt %d/%d)",
                               self._provider, wait, attempt, self._retry.max_attempts)
                time.sleep(wait)

            except SpotiflacError as exc:
                if not exc.is_retryable() or attempt == self._retry.max_attempts:
                    raise
                last_err = exc
                logger.warning("[%s] Retryable error — attempt %d/%d: %s",
                               self._provider, attempt, self._retry.max_attempts, exc)
                time.sleep(min(delay, self._retry.max_delay_s))
                delay *= self._retry.backoff_factor

            except requests.Timeout as exc:
                last_err = NetworkError(self._provider, f"Timeout after {self._timeout}s", exc)
                if attempt == self._retry.max_attempts:
                    raise last_err
                time.sleep(min(delay, self._retry.max_delay_s))
                delay *= self._retry.backoff_factor

            except requests.ConnectionError as exc:
                last_err = NetworkError(self._provider, "Connection failed", exc)
                if attempt == self._retry.max_attempts:
                    raise last_err
                time.sleep(min(delay, self._retry.max_delay_s))
                delay *= self._retry.backoff_factor

        raise last_err  # type: ignore[misc]

    def _raise_for_status(self, resp: Response) -> None:
        sc = resp.status_code
        if sc == 200:
            return
        if sc == 401:
            raise AuthError(self._provider, "Unauthorized (401)")
        if sc == 403:
            raise AuthError(self._provider, "Forbidden (403)")
        if sc == 404:
            raise TrackNotFoundError(self._provider, resp.url)
        if sc == 429:
            retry_after = int(resp.headers.get("Retry-After", 5))
            raise RateLimitedError(self._provider, retry_after)
        if not resp.ok:
            raise NetworkError(self._provider, f"HTTP {sc} from {resp.url}")

    def _parse_json(self, resp: Response) -> dict:
        body = resp.text
        if not body.strip():
            raise ParseError(self._provider, "Empty response body")
        try:
            return resp.json()
        except ValueError as exc:
            preview = body[:200] + ("..." if len(body) > 200 else "")
            raise ParseError(self._provider, f"Invalid JSON: {preview}", exc)
