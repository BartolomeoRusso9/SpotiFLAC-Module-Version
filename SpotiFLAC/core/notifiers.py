"""core/notifiers.py — telling something outside the process what happened.

`core/library_notify.py` tells a *media server* to rescan. That is one
specific errand. What is missing is the general one: a run finished, and the
person who started it is not watching the terminal — a headless instance on a
NAS, a `--watch` loop, a nightly `--check-subscriptions` from cron.

Four shapes cover nearly every request for this:

  - `webhook`  — POST the run's JSON to a URL you control. The general case;
                 everything else here is a convenience wrapper for a service
                 that wants a particular body shape.
  - `discord`  — a Discord webhook URL.
  - `telegram` — a bot token and a chat id.
  - `ntfy`     — an ntfy.sh (or self-hosted) topic.

Credentials and destinations
----------------------------
The target is read from `--notify` / `$SPOTIFLAC_NOTIFY_URL`, never from the
config a GUI or web request can supply. Same reasoning as the post-download
shell command (see app.py's POST_COMMAND_ENV) and the library token: this
sends data *out* of the instance to a host of someone's choosing, and an HTTP
request must not get to choose which host that is. A `--web` user who could
set this could point every download's metadata at a server they own.

Nothing here is enabled by default and no destination is built in.

What is sent
------------
Track title, artist, album, provider, format, and success/failure. Not the
file path — it leaks the directory layout of the machine, and the receiving
end can do nothing with a path on another host anyway.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

NOTIFY_URL_ENV = "SPOTIFLAC_NOTIFY_URL"
NOTIFY_TOKEN_ENV = "SPOTIFLAC_NOTIFY_TOKEN"

KINDS = ("webhook", "discord", "telegram", "ntfy")

#: When to speak up.
EVENTS = ("success", "failure", "both", "summary")


class NotifierError(ValueError):
    """The notifier could not be configured."""


@dataclass(frozen=True)
class NotifyTarget:
    kind: str
    url: str
    token: str = ""
    #: Telegram's chat id; unused by the other kinds.
    chat_id: str = ""
    on: str = "both"

    def wants(self, success: bool) -> bool:
        if self.on == "both":
            return True
        if self.on == "success":
            return success
        if self.on == "failure":
            return not success
        # "summary" is handled by the caller, per run, not per track.
        return False


def resolve_url(explicit: str | None) -> str:
    return (explicit if explicit is not None else os.environ.get(NOTIFY_URL_ENV)) or ""


def resolve_token(explicit: str | None) -> str:
    return (
        explicit if explicit is not None else os.environ.get(NOTIFY_TOKEN_ENV)
    ) or ""


def build_target(
    kind: str,
    url: str | None = None,
    token: str | None = None,
    chat_id: str = "",
    on: str = "both",
) -> NotifyTarget:
    kind = (kind or "").strip().lower()
    if kind not in KINDS:
        msg = f"Unknown notifier {kind!r}. Expected one of: {', '.join(KINDS)}"
        raise NotifierError(msg)

    if on not in EVENTS:
        msg = f"Unknown notify event {on!r}. Expected one of: {', '.join(EVENTS)}"
        raise NotifierError(msg)

    resolved_url = resolve_url(url).strip()
    resolved_token = resolve_token(token).strip()

    if kind == "telegram":
        # Telegram is addressed by token + chat id rather than by a URL, so
        # the URL is built here and only the credential has to be supplied.
        if not resolved_token:
            msg = (
                "Telegram needs a bot token: pass --notify-token or set "
                f"${NOTIFY_TOKEN_ENV}."
            )
            raise NotifierError(msg)
        if not chat_id:
            msg = "Telegram needs a chat id: pass --notify-chat-id."
            raise NotifierError(msg)
        resolved_url = f"https://api.telegram.org/bot{resolved_token}/sendMessage"
    elif not resolved_url:
        msg = (
            f"The {kind} notifier needs a URL: pass --notify-url or set "
            f"${NOTIFY_URL_ENV}."
        )
        raise NotifierError(msg)

    target = NotifyTarget(
        kind=kind,
        url=resolved_url,
        token=resolved_token,
        chat_id=str(chat_id or ""),
        on=on,
    )
    _check_destination(target)
    return target


def _check_destination(target: NotifyTarget) -> None:
    """Refuses a destination that is obviously not a notification endpoint,
    and warns about one that will carry a credential in the clear.

    The scheme check is a refusal rather than a warning: `file://` and friends
    are not something a notifier URL is ever legitimately set to, and the
    failure mode of accepting one is silent.

    Whether the host is internal is *not* refused. The whole point of a
    self-hosted ntfy or a webhook into a home-lab service is that it lives on
    a private address, so blocking those would remove the feature from its
    main use case. It is worth one log line for the case where a URL was
    pasted wrong.
    """
    parsed = urlparse(target.url)
    if parsed.scheme not in ("http", "https"):
        msg = (
            f"A notifier URL must be http:// or https://, got {parsed.scheme!r}. "
            "This is where run results are sent, not a local path."
        )
        raise NotifierError(msg)

    if not parsed.hostname:
        msg = f"The notifier URL {target.url!r} has no host."
        raise NotifierError(msg)

    if parsed.scheme == "http" and not _is_local(parsed.hostname):
        # A Discord/Telegram/ntfy credential is embedded in the URL itself.
        logger.warning(
            "[notify] %s target is plain HTTP over a non-local host — the "
            "credential in the URL is readable by anything on the path.",
            target.kind,
        )


def _is_local(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            address = ipaddress.ip_address(socket.gethostbyname(host))
        except (OSError, ValueError):
            return False
    return address.is_loopback or address.is_private or address.is_link_local


# ─────────────────────────────────────────────────────────────
#  Message shaping
# ─────────────────────────────────────────────────────────────


def track_summary(result: Any, metadata: Any) -> str:
    title = getattr(metadata, "title", "") or "Unknown"
    artist = getattr(metadata, "artists", "") or "Unknown"
    success = bool(getattr(result, "success", False))
    if success:
        provider = getattr(result, "provider", "") or "?"
        fmt = (getattr(result, "format", "") or "").upper()
        detail = f"{provider}{f' · {fmt}' if fmt else ''}"
        return f"✅ {artist} — {title}  ({detail})"
    error = getattr(result, "error", "") or "unknown error"
    return f"❌ {artist} — {title}  ({error})"


def build_request(
    target: NotifyTarget, text: str, payload: dict
) -> tuple[str, dict, dict]:
    """(url, json_body, headers) for this target.

    `payload` is the structured version, sent as-is to a plain `webhook` so a
    receiving script gets data rather than a sentence; the other three take
    `text`, because they render a message to a human.
    """
    if target.kind == "discord":
        # Discord rejects an empty content and truncates at 2000 characters.
        return target.url, {"content": text[:1900] or "(no content)"}, {}

    if target.kind == "telegram":
        # No parse_mode: the message is plain text (see track_summary), and
        # asking Telegram to parse it as HTML makes a track title containing
        # '<' fail the whole send with "can't parse entities".
        return (
            target.url,
            {"chat_id": target.chat_id, "text": text[:4000]},
            {},
        )

    if target.kind == "ntfy":
        headers = {}
        if target.token:
            headers["Authorization"] = f"Bearer {target.token}"
        return target.url, {"message": text[:4000], "title": "SpotiFLAC"}, headers

    headers = {}
    if target.token:
        headers["Authorization"] = f"Bearer {target.token}"
    return target.url, payload, headers


async def send_async(target: NotifyTarget, text: str, payload: dict) -> bool:
    """Sends one notification. Returns whether it was accepted.

    Never raises: the download already happened, and a notifier that is down
    must not turn a completed run into a failed one — the same contract
    `library_notify.request_rescan()` has.
    """
    from .http import AsyncHttpClient

    url, body, headers = build_request(target, text, payload)
    client = AsyncHttpClient(f"notify:{target.kind}", timeout_s=15)
    try:
        await client.post(url, json=body, headers=headers)
    except Exception as exc:
        logger.warning("[notify] %s notification failed: %s", target.kind, exc)
        return False
    logger.info("[notify] Sent a %s notification", target.kind)
    return True


# ─────────────────────────────────────────────────────────────
#  Hook integration
# ─────────────────────────────────────────────────────────────


def notify_hook(target: NotifyTarget) -> Any:
    """A post-download hook that notifies per track.

    Registered the same way `RunReport` and the download log are — appended
    to `DownloadOptions.post_download_hooks` — so a notifier sees exactly the
    events a user-supplied `--post-hook` sees, and nothing in the downloader
    has to know notifiers exist.
    """

    async def _on_track(result: Any, metadata: Any) -> None:
        if not target.wants(bool(getattr(result, "success", False))):
            return
        payload = {
            "event": "track",
            "success": bool(getattr(result, "success", False)),
            "title": getattr(metadata, "title", ""),
            "artist": getattr(metadata, "artists", ""),
            "album": getattr(metadata, "album", ""),
            "isrc": getattr(metadata, "isrc", ""),
            "provider": getattr(result, "provider", ""),
            "format": getattr(result, "format", ""),
            "error": getattr(result, "error", None),
            # Deliberately no file_path — see the module docstring.
        }
        await send_async(target, track_summary(result, metadata), payload)

    _on_track.__qualname__ = "notifiers.notify_hook.on_track"
    return _on_track


async def notify_run_summary(target: NotifyTarget, report: Any) -> bool:
    """Sends one message for a whole run, from a `core/report.RunReport`.

    This is what `--notify-on summary` uses, and what anyone downloading a
    300-track discography actually wants: one message, not three hundred.
    """
    try:
        data = report.to_dict() if hasattr(report, "to_dict") else {}
    except Exception:
        data = {}

    # RunReport's own vocabulary: a per-track `status` of
    # "downloaded"/"skipped"/"failed" (not a boolean `success`), artists under
    # `artists`, and a pre-computed `summary` block.
    tracks = data.get("tracks") or []
    summary = data.get("summary") or {}
    downloaded = int(summary.get("downloaded", 0))
    skipped = int(summary.get("skipped", 0))
    failed = int(summary.get("failed", 0))

    headline = f"SpotiFLAC: {downloaded} downloaded, {failed} failed"
    if skipped:
        headline += f", {skipped} already present"
    lines = [headline + "."]

    # Only the failures are listed: a successful run needs no roll-call, and a
    # failed one is unactionable without the reason.
    for track in tracks:
        if track.get("status") != "failed":
            continue
        lines.append(
            f"  ❌ {track.get('artists') or '?'} — {track.get('title') or '?'}: "
            f"{track.get('error') or 'unknown error'}"
        )
    text = "\n".join(lines[:40])

    payload = {
        "event": "run",
        "downloaded": downloaded,
        "skipped": skipped,
        "failed": failed,
        "started_at": data.get("started_at"),
        "finished_at": data.get("finished_at"),
        # Rebuilt rather than spread from `data`: RunReport records a
        # `file_path` per track, and this module promises not to send one
        # (see the module docstring). `**data` would have leaked every one.
        "tracks": [
            {
                key: track.get(key)
                for key in (
                    "title",
                    "artists",
                    "album",
                    "isrc",
                    "status",
                    "provider",
                    "format",
                    "error",
                )
            }
            for track in tracks
        ],
    }
    return await send_async(target, text, payload)
