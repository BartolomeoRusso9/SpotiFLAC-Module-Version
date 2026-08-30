"""SpotiFLAC/webapp.py — browser-based alternative to the pywebview desktop GUI.

Wraps the same SpotiFLAC_API used by app.py behind a small FastAPI server:
  - Every whitelisted Api method becomes a POST /api/<method> endpoint.
  - Push events that desktop mode sends via pywebview's evaluate_js are
    instead broadcast to connected browsers over a WebSocket at /ws.
  - A new /api/browse-folder endpoint replaces the native OS folder dialog
    (choose_folder), since a browser cannot open one that returns a real
    server-side path.

No business logic lives here — this module only adapts transport. All the
actual work (metadata, downloads, extensions, profiles, ...) is the same
SpotiFLAC_API code the desktop app already uses and that has already been
exercised in that form.

Covered by tests/test_webapp_*.py: auth (token and per-account), the
WebSocket gate, the ops endpoints, and the method allowlist. What no test
here covers is a real download through a real provider — that needs an
installed extension and a network.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import (
    Body,
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
)
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .app import SpotiFLAC_API

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"

# ── Optional shared-secret auth (off by default — see --web-token) ─────────
WEB_TOKEN_ENV = "SPOTIFLAC_WEB_TOKEN"
WEB_TOKEN_COOKIE = "spotiflac_web_token"
WEB_TOKEN_QUERY_PARAM = "token"

# ── Optional multi-user accounts (off by default — see --web-multiuser) ────
#
# A session cookie identifies who is asking, gates /api/* and /ws behind
# being logged in, and selects that account's own SpotiFLAC_API instance
# (see ApiRegistry). Each account therefore gets its own search results, its
# own download folder under the shared root, and its own event stream: one
# person's progress and file paths no longer scroll past in everybody's
# browser.
#
# What remains shared is what is genuinely machine-wide — installed
# extensions, the registry configuration, the Ed25519 trust store, the HTTP
# connection pool, and the ffmpeg/Node availability checks. This is
# household or small-team separation, not hostile-tenant isolation: accounts
# still run in one process, as one OS user, and anyone who can install an
# extension can affect everyone.
SESSION_COOKIE = "spotiflac_session"

#: Unauthenticated liveness probe — see the endpoint for why.
HEALTH_PATH = "/healthz"


def resolve_web_token(explicit: str | None) -> str | None:
    """CLI/API value wins; falls back to SPOTIFLAC_WEB_TOKEN; None means
    "no auth", preserving today's default (unauthenticated) behavior.
    """
    token = explicit if explicit is not None else os.environ.get(WEB_TOKEN_ENV)
    token = (token or "").strip()
    return token or None


def _token_matches(candidate: str | None, expected: str) -> bool:
    """Constant-time comparison so response timing can't leak the token."""
    if not candidate:
        return False
    return secrets.compare_digest(candidate, expected)


def _is_path_safe(candidate: Path, api, allow_home: bool = True) -> bool:
    """Check if candidate path is within approved roots.

    Returns True if the resolved canonical path is a descendant of (or equal to)
    at least one approved root. Returns False otherwise (path traversal attempt).

    `allow_home=False` drops the home directory from the approved roots,
    leaving only this caller's own download_dir. That is what multi-user mode
    needs: with home approved, any account could browse to the shared
    download root and read every other account's folder name — and anything
    else under $HOME besides. Single-user mode keeps home, because there the
    "other account" is the same person.
    """
    try:
        resolved = os.path.realpath(str(candidate))
        approved_roots = [os.path.realpath(str(api.download_dir))]
        if allow_home:
            approved_roots.append(os.path.realpath(str(Path.home())))
        for root in approved_roots:
            try:
                if os.path.commonpath([resolved, root]) == root:
                    return True
            except ValueError:
                # Different drives / mixed absolute-relative — not under root.
                continue
        return False
    except Exception:
        return False


# Methods safe to expose directly over HTTP. This is an explicit allowlist —
# window-chrome methods (minimize/maximize/resize/move/destroy, which only
# make sense for a native pywebview window) are intentionally excluded; the
# web frontend shim no-ops them instead. choose_folder is excluded too (see
# set_download_dir / browse-folder below).
ALLOWED_METHODS: set[str] = {
    "get_version",
    "get_latest_version",
    "get_artist_images",
    "get_ffmpeg_status",
    "get_node_status",
    "save_settings",
    "save_theme",
    "load_settings",
    "get_registries",
    "add_registry",
    "remove_registry",
    "get_registry_directories",
    "add_registry_directory",
    "remove_registry_directory",
    "discover_registries",
    "get_history",
    "get_profiles",
    "load_profile_data",
    "cache_image",
    "get_spotify_home_feed",
    "search_provider",
    "search_provider_async",
    "remove_history_item",
    "get_network_status",
    "save_profile_data",
    "delete_profile_data",
    "check_qobuz_api",
    "check_tidal_api",
    "set_download_dir",
    "open_config_folder",
    "open_url",
    "download_track_lyrics",
    "download_track_cover",
    "download_cover",
    "download_album_cover",
    "download_all_covers",
    "download_all_lyrics",
    "get_track_preview",
    "fetch_metadata",
    "download_tracks",
    "run_health_check",
    "scan_local",
    "apply_local_tags",
    "get_dedup_status",
    "scan_for_duplicates",
    "get_trusted_keys",
    # Subscriptions (see core/subscriptions.py). Read and write, but every
    # write here only edits a list of URLs to follow — the same category of
    # operation as add_registry, and unlike add_trusted_key it grants no new
    # ability to whoever can reach the port.
    "get_subscriptions",
    "add_subscription",
    "remove_subscription",
    "set_subscription_enabled",
    "reset_subscription",
    "check_subscriptions",
    # Extension health (read-only, plus a counter reset).
    "get_extension_health",
    "reset_extension_health",
    # The dashboard (core/stats.py). Read-only, and in multi-user mode it is
    # the calling account's own history: each account gets its own Api
    # instance, and `owner` is set on it above.
    "get_stats",
    # CSV input (core/csv_source.py). Both take the file's *contents*, never
    # a path — the browser reads the file locally, so nothing here can be
    # pointed at a path on the host.
    "preview_csv",
    "fetch_csv",
}

# Deliberately absent from ALLOWED_METHODS, even though they exist on the Api
# object (keep this list next to the allowlist so a future "why isn't X
# exposed?" has an answer here rather than in a commit message):
#
#   add_trusted_key / remove_trusted_key
#       These write ~/.spotiflac/trusted_keys.json — the Ed25519 root of trust
#       that decides which registry entries count as "signed" (see
#       extensions/trust.py). Reachable over HTTP, they let whoever can reach
#       the port install their own key and then sign their own extensions,
#       which is the one thing the signing scheme exists to prevent. Reading
#       the list (get_trusted_keys) is fine; writing it is CLI-only, via
#       tools/registry_signing_cli.py.
#
#   search_code
#       A development helper: greps a caller-supplied path and returns the
#       matching *lines*, which over HTTP is an arbitrary file-content read of
#       anything the process can open. The frontend never called it.
#
#   choose_folder and the window-chrome methods
#       See the note above the allowlist — those are native-window only.


class LoginRateLimiter:
    """Exponential backoff per client address after failed logins.

    Two things make an unthrottled /api/auth/login worse than it looks. It's
    in _EXEMPT_PATHS, so it is the one endpoint reachable without a session —
    and every attempt costs 600k PBKDF2 iterations (see core/web_users.py),
    run in Starlette's bounded threadpool. So the same endpoint that lets a
    password be guessed at full speed also lets a few dozen concurrent
    requests saturate the pool and stall every other request in the process.

    Deliberately in-memory and per-process, like SessionStore: this is a
    small self-hosted server, not a fleet behind a shared cache.
    """

    _BASE_DELAY_S = 1.0
    _MAX_DELAY_S = 60.0
    _FREE_ATTEMPTS = 3  # fat-finger allowance before any delay kicks in
    _FORGET_AFTER_S = 900.0

    def __init__(self) -> None:
        self._failures: dict[str, tuple[int, float]] = {}  # key -> (count, last_try)
        self._lock = threading.Lock()

    def retry_after(self, key: str) -> int | None:
        """Seconds the caller must wait, or None if it may try now."""
        now = time.monotonic()
        with self._lock:
            entry = self._failures.get(key)
            if entry is None:
                return None
            count, last = entry
            if now - last > self._FORGET_AFTER_S:
                del self._failures[key]
                return None
            if count <= self._FREE_ATTEMPTS:
                return None
            delay = min(
                self._BASE_DELAY_S * (2 ** (count - self._FREE_ATTEMPTS - 1)),
                self._MAX_DELAY_S,
            )
            remaining = (last + delay) - now
            return max(1, int(remaining + 0.999)) if remaining > 0 else None

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            count, last = self._failures.get(key, (0, now))
            if now - last > self._FORGET_AFTER_S:
                count = 0
            self._failures[key] = (count + 1, now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


class ConnectionManager:
    """Tracks connected WebSocket clients and lets worker threads (where
    SpotiFLAC_API methods actually run) push events to them safely.

    Each connection remembers who opened it. In single-user mode that is
    None and every event goes to everyone, exactly as before; in multi-user
    mode it is the logged-in account, and an event addressed to one owner
    reaches only their sockets. Without that, "isolated" accounts would
    still watch each other's logs, progress and file paths scroll past.
    """

    def __init__(self) -> None:
        self._connections: dict[WebSocket, str | None] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, ws: WebSocket, owner: str | None = None) -> None:
        await ws.accept()
        self._connections[ws] = owner

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.pop(ws, None)

    def count(self, owner: str | None = None) -> int:
        if owner is None:
            return len(self._connections)
        return sum(1 for value in self._connections.values() if value == owner)

    async def _send_all(self, message: dict, owner: str | None) -> None:
        dead = []
        for ws, ws_owner in list(self._connections.items()):
            if owner is not None and ws_owner != owner:
                continue
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._connections.pop(ws, None)

    def broadcast(self, fn_name: str, args: list, owner: str | None = None) -> None:
        """Thread-safe: callable from any thread, including the worker
        threads download_tracks()/fetch_metadata()/etc. run in. Schedules
        the actual send onto the server's asyncio event loop.

        `owner=None` means everyone — the single-user default. A per-user Api
        instance passes its own username (see ApiRegistry).
        """
        if self._loop is None:
            return
        message = {"fn": fn_name, "args": args}
        try:
            asyncio.run_coroutine_threadsafe(self._send_all(message, owner), self._loop)
        except Exception:
            logger.debug("WebSocket broadcast failed", exc_info=True)


class ApiRegistry:
    """One SpotiFLAC_API per account, created on first use.

    Before this, `--web-multiuser` shared a single Api instance across every
    session: accounts had separate logins but one `current_tracks`, one
    `download_dir`, and one event stream. Logging in as someone else changed
    who a job was attributed to and nothing else, which is a thin enough
    notion of "multi-user" that webapp.py's own docstring warned people off
    using it for anyone they wouldn't hand a shell to.

    Each account now gets its own instance, its own search results and its
    own download folder underneath the shared root. What is still shared is
    everything that is genuinely machine-wide: installed extensions, the
    registry configuration, the trust store, the HTTP connection pool.
    """

    def __init__(self, manager: ConnectionManager, base_download_dir: str) -> None:
        self._manager = manager
        self._base = base_download_dir
        self._apis: dict[str, SpotiFLAC_API] = {}
        self._lock = threading.Lock()

    def get(self, username: str | None) -> SpotiFLAC_API:
        key = username or ""
        with self._lock:
            existing = self._apis.get(key)
            if existing is not None:
                return existing
            api = self._build(username)
            self._apis[key] = api
            return api

    def _build(self, username: str | None) -> SpotiFLAC_API:
        api = SpotiFLAC_API()
        api._ws_broadcast = lambda fn, args: self._manager.broadcast(
            fn, args, owner=username
        )
        # Everything this instance downloads is written to the log under this
        # name, and the dashboard it serves reads back the same name — one
        # account's numbers, not the machine's.
        api.owner = username or ""
        if username:
            # A per-account subfolder of the same root, not an unrelated
            # path: an operator who bind-mounted one downloads volume still
            # gets one downloads volume, just with a folder each.
            api.download_dir = os.path.join(self._base, _safe_username(username))
            with contextlib.suppress(OSError):
                os.makedirs(api.download_dir, exist_ok=True)
        return api

    def known(self) -> list[str]:
        with self._lock:
            return [k for k in self._apis if k]


def _safe_username(username: str) -> str:
    """A username reduced to something safe to use as a directory name.

    Accounts are created locally by the operator, so this is not the last
    line of defence — but a username is still user-supplied text on its way
    into a filesystem path, and `..` should not be spellable there.

    The suffix is not decoration. Sanitising alone is lossy: "a b", "a_b"
    and "a/b" all reduce to "a_b", so three separate accounts would have
    silently shared one download folder — the exact thing per-account
    directories exist to prevent. A short digest of the *original* name
    keeps distinct accounts distinct while the readable part stays readable.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", username).strip("._") or "user"
    digest = hashlib.sha256(username.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned[:55]}-{digest}"


def create_app(token: str | None = None, multiuser: bool = False) -> FastAPI:
    """`multiuser=True` gives every account its own SpotiFLAC_API instance,
    download folder and event stream — see the note on SESSION_COOKIE above
    for what is isolated and what is still shared machine-wide.
    """
    manager = ConnectionManager()
    # The shared instance. In single-user mode it is the only one, and its
    # events go to every connected browser (owner=None), exactly as before.
    api = SpotiFLAC_API()
    api._ws_broadcast = manager.broadcast

    registry = ApiRegistry(manager, api.download_dir)

    def api_for(request: Request) -> SpotiFLAC_API:
        """The Api instance a request should act on.

        Single-user mode has exactly one; multi-user mode has one per
        account, so two people searching at the same time no longer
        overwrite each other's `current_tracks`.
        """
        if not multiuser:
            return api
        return registry.get(getattr(request.state, "username", None))

    # Exposed on app.state so tests (and anything embedding this app) can
    # observe which instance a request actually reached, rather than having
    # to infer it from a response that would look identical either way.
    app_state_api = api
    sessions = None
    job_queue = None
    login_limiter = LoginRateLimiter()
    if multiuser:
        from .core.job_queue import JobQueue, QueueFullError
        from .core.web_users import SessionStore, check_quota

        sessions = SessionStore()

        def _quota_check(owner: str) -> None:
            """Refuses a submission that would exceed the account's quota.

            Injected into JobQueue rather than implemented there: the queue
            deliberately knows nothing about accounts (see its docstring), and
            "how much may this person download" is a question about accounts.
            """
            check_quota(owner)

        def _run_queued_download(payload: dict) -> dict:
            # The owner rides in the payload so the worker downloads into
            # *their* folder and their browser gets the progress events —
            # the queue thread has no request to read it from.
            owner_api = registry.get(payload.get("owner"))

            # Two shapes reach this handler. The frontend submits tracks it
            # has already fetched, by index into the account's current_tracks
            # (`selected_indices`); /api/v1/downloads submits a bare URL,
            # because a REST client has no notion of a fetch that happened
            # earlier in someone's browser session. Resolving a URL is the
            # same fetch_metadata() the GUI runs, so both end up on one path.
            if "selected_indices" in payload:
                owner_api.download_tracks(
                    payload["selected_indices"], payload.get("config", {})
                )
            else:
                owner_api.fetch_metadata(payload["url"])
            return {"status": "dispatched"}

        job_queue = JobQueue(
            handler=_run_queued_download,
            workers=1,
            # Survives a restart — see core/job_queue.py's docstring. This is
            # the deployment the queue exists for (headless, on a NAS), and
            # the one where a lost backlog is least likely to be noticed.
            persist=True,
            quota_check=_quota_check,
        )

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        manager.bind_loop(asyncio.get_running_loop())
        # Mirrors what _on_loaded() does for the desktop window, minus the
        # window-specific bits (there is no self._window in web mode, so
        # every push below goes out over the WebSocket only).
        await run_in_threadpool(
            api.log, "Python backend connected (web mode).", "debug"
        )
        await run_in_threadpool(
            api.log,
            f"Default download folder: {api.download_dir}",
            "debug",
        )
        await run_in_threadpool(api._check_ffmpeg_startup)
        await run_in_threadpool(api._check_node_startup)
        try:
            from .extensions.manager import ExtensionManager

            # Explicit, because the comment that used to sit here said "no
            # auto-install by default" while the constructor's default is
            # True — so it did bootstrap, and had done all along. Behaviour
            # unchanged; only the claim about it was wrong. In practice this
            # is a no-op under `spotiflac --web`: launcher.amain() has already
            # run the bootstrap, and ExtensionManager dedupes it per-process
            # (_startup_registry_checks). It matters when webapp is started
            # directly, e.g. `python -m SpotiFLAC.webapp`.
            await run_in_threadpool(ExtensionManager, auto_install_downloads=True)
        except Exception as e:
            await run_in_threadpool(api.log, f"Extension init error: {e}", "warn")
        api._push("loadHistoryAndProfiles")
        api._push("__set_version_label", api.app_version)
        yield
        # No shutdown-side work (yet) — everything here is process-lifetime
        # state (threads, in-memory sessions/queue) that dies with the
        # process anyway.

    app = FastAPI(title="SpotiFLAC Web", lifespan=_lifespan)
    app.state.shared_api = app_state_api
    app.state.api_registry = registry
    app.state.job_queue = job_queue

    @app.middleware("http")
    async def _no_cache_frontend(request, call_next):
        response = await call_next(request)
        if request.url.path.endswith((".js", ".css", ".html")):
            response.headers["Cache-Control"] = (
                "no-store, no-cache, must-revalidate, max-age=0"
            )
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    if token:
        # Opt-in shared-secret gate (see --web-token / SPOTIFLAC_WEB_TOKEN):
        # every request — page, static asset, or /api/* call — must present
        # the token, either as a `?token=` query param (the only option a
        # first plain browser navigation or a WebSocket upgrade can supply)
        # or the cookie this middleware sets once that query param checks
        # out. Everything is denied outright when no token is configured
        # (the default), so this changes nothing unless explicitly enabled.
        @app.middleware("http")
        async def _require_web_token(request: Request, call_next):
            # /healthz is exempt on purpose: an orchestrator's health probe
            # has no token, and a check that 401s reports "unhealthy" for a
            # reason that has nothing to do with health. It discloses only
            # that the process is answering.
            if request.url.path == HEALTH_PATH:
                return await call_next(request)
            supplied = request.query_params.get(
                WEB_TOKEN_QUERY_PARAM
            ) or request.cookies.get(WEB_TOKEN_COOKIE)
            if not _token_matches(supplied, token):
                return PlainTextResponse(
                    "Missing or invalid ?token= — this SpotiFLAC web instance "
                    "requires the token configured via --web-token / "
                    f"{WEB_TOKEN_ENV}.",
                    status_code=401,
                )
            response = await call_next(request)
            if request.query_params.get(WEB_TOKEN_QUERY_PARAM):
                # Valid token supplied via URL: persist it as a cookie so the
                # browser stops needing ?token=... on every single request
                # (static assets, the WebSocket upgrade, /api/* fetches).
                response.set_cookie(
                    WEB_TOKEN_COOKIE,
                    token,
                    httponly=True,
                    samesite="lax",
                )
            return response

    if multiuser:
        # Gates /api/* behind a logged-in session — see the "Multi-user mode"
        # note on SESSION_COOKIE above for what this does and doesn't isolate
        # between accounts. /ws is gated separately, inside ws_endpoint: HTTP
        # middleware never runs for WebSocket upgrades. The frontend doesn't
        # have a login form yet: call POST /api/auth/login directly (curl, a
        # future UI, ...) to obtain the session cookie.
        _EXEMPT_PATHS = {"/api/auth/login", "/api/auth/status"}

        @app.middleware("http")
        async def _require_session(request: Request, call_next):
            path = request.url.path
            if not path.startswith("/api/") or path in _EXEMPT_PATHS:
                return await call_next(request)
            username = sessions.username_for(request.cookies.get(SESSION_COOKIE))
            if username is None:
                return JSONResponse({"error": "Not logged in"}, status_code=401)
            request.state.username = username
            return await call_next(request)

        @app.post("/api/auth/login")
        async def auth_login(
            request: Request, payload: dict = Body(...)
        ) -> JSONResponse:
            from .core.web_users import verify_password

            client_ip = request.client.host if request.client else "unknown"
            retry_after = login_limiter.retry_after(client_ip)
            if retry_after is not None:
                return JSONResponse(
                    {"error": "Too many failed logins. Try again shortly."},
                    status_code=429,
                    headers={"Retry-After": str(retry_after)},
                )

            username = str(payload.get("username", ""))
            password = str(payload.get("password", ""))
            valid = await run_in_threadpool(verify_password, username, password)
            if not valid:
                login_limiter.record_failure(client_ip)
                return JSONResponse(
                    {"error": "Invalid username or password"}, status_code=401
                )
            login_limiter.reset(client_ip)
            session_token = sessions.create(username)
            response = JSONResponse({"status": "ok", "username": username})
            response.set_cookie(
                SESSION_COOKIE, session_token, httponly=True, samesite="lax"
            )
            return response

        @app.post("/api/auth/logout")
        async def auth_logout(request: Request) -> JSONResponse:
            sessions.revoke(request.cookies.get(SESSION_COOKIE))
            response = JSONResponse({"status": "ok"})
            response.delete_cookie(SESSION_COOKIE)
            return response

        @app.post("/api/queue/submit-download")
        async def queue_submit_download(
            request: Request, payload: dict = Body(...)
        ) -> JSONResponse:
            assert job_queue is not None  # always set together with multiuser=True
            try:
                job = job_queue.submit(
                    request.state.username,
                    {
                        "owner": request.state.username,
                        "selected_indices": payload.get("selected_indices", []),
                        "config": payload.get("config", {}),
                    },
                )
            except QueueFullError as exc:
                # Built from the exception's own fields, not str(exc): an
                # exception message is written for a log reader, and putting
                # one in a response body is how internals end up in front of
                # whoever is calling. The caller still learns everything
                # actionable — how many they have queued, and the limit.
                logger.info(
                    "Queue submission refused for %s (%d/%d)",
                    request.state.username,
                    exc.pending,
                    exc.limit,
                )
                return JSONResponse(
                    {
                        "error": "Too many downloads already queued.",
                        "pending": exc.pending,
                        "limit": exc.limit,
                    },
                    status_code=429,
                )
            return JSONResponse({"job_id": job.id, "status": job.status.value})

        @app.get("/api/queue/mine")
        async def queue_mine(request: Request) -> JSONResponse:
            assert job_queue is not None  # always set together with multiuser=True
            jobs = job_queue.list_for(request.state.username)
            return JSONResponse({"jobs": [j.to_dict() for j in jobs]})

        @app.get("/api/quota/mine")
        async def quota_mine(request: Request) -> JSONResponse:
            """This account's own usage against its own limits.

            Not admin-gated: it is about the caller, and being refused a
            download without being able to see why is the kind of opacity
            that generates support requests.
            """
            from .core.web_users import quota_usage

            usage = await run_in_threadpool(quota_usage, request.state.username)
            return JSONResponse(usage)

        # ── Administration ────────────────────────────────────────────────
        #
        # Everything below needs the `admin` role. Accounts were flat until
        # now, which is why /api/metrics hides instance-wide counters in
        # multi-user mode — there was nobody to show them to. There is now,
        # so an admin sees them and an ordinary account still does not.
        async def _require_admin(request: Request) -> str:
            from .core.web_users import is_admin

            username = request.state.username
            if not await run_in_threadpool(is_admin, username):
                # 404, not 403: whether an admin API exists on this instance
                # is not something an ordinary account needs to learn.
                raise HTTPException(status_code=404, detail="Not found")
            return username

        @app.get("/api/admin/users")
        async def admin_list_users(request: Request) -> JSONResponse:
            from .core.web_users import list_users, quota_usage

            await _require_admin(request)
            users = await run_in_threadpool(list_users)
            for user in users:
                user["usage"] = await run_in_threadpool(quota_usage, user["username"])
            return JSONResponse({"users": users})

        @app.post("/api/admin/quota")
        async def admin_set_quota(
            request: Request, payload: dict = Body(...)
        ) -> JSONResponse:
            from .core.web_users import set_quota

            await _require_admin(request)
            username = str(payload.get("username", ""))
            tracks = payload.get("daily_track_quota")
            size = payload.get("daily_byte_quota")
            updated = await run_in_threadpool(
                lambda: set_quota(
                    username,
                    daily_track_quota=None if tracks is None else int(tracks),
                    daily_byte_quota=None if size is None else int(size),
                )
            )
            if not updated:
                return JSONResponse({"error": "No such user"}, status_code=404)
            return JSONResponse({"ok": True})

        @app.post("/api/admin/role")
        async def admin_set_role(
            request: Request, payload: dict = Body(...)
        ) -> JSONResponse:
            from .core.web_users import WebUserError, set_role

            await _require_admin(request)
            try:
                updated = await run_in_threadpool(
                    set_role,
                    str(payload.get("username", "")),
                    str(payload.get("role", "")),
                )
            except WebUserError as exc:
                # This one *is* safe to surface: every WebUserError raised by
                # set_role is a message written for the operator ("unknown
                # role", "that is the only admin"), with no internals in it.
                return JSONResponse({"error": str(exc)}, status_code=400)
            if not updated:
                return JSONResponse({"error": "No such user"}, status_code=404)
            return JSONResponse({"ok": True})

        @app.get("/api/admin/queue")
        async def admin_queue(request: Request) -> JSONResponse:
            await _require_admin(request)
            assert job_queue is not None
            return JSONResponse({"jobs": [j.to_dict() for j in job_queue.list_all()]})

    # ── Operations: liveness and metrics ──────────────────────────────────
    #
    # /healthz is deliberately outside /api/, and therefore outside the
    # session gate: a container orchestrator has no cookie, and a health
    # check that needs credentials is one that reports "unhealthy" for the
    # wrong reason. It returns no data about the instance beyond "the process
    # is answering", so there is nothing there to protect.
    #
    # /metrics does expose real information (which providers are failing, how
    # much has been downloaded), so it sits under /api/ and inherits whatever
    # auth is configured.
    @app.get(HEALTH_PATH)
    async def healthz() -> JSONResponse:
        """Liveness. docker-compose.example.yml used to poll `/` for this,
        which downloads and renders the whole frontend to answer a yes/no
        question — and would go on succeeding if every backend component
        behind it were broken.
        """
        # Status and nothing else. Everything this used to add — version,
        # whether auth is on, how many clients are connected — is a free
        # fingerprint of the instance for anyone who can reach the port, and
        # this is the one endpoint deliberately outside the auth gate. The
        # same fields are in /api/metrics, behind whatever auth is set.
        return JSONResponse({"status": "ok"})

    @app.get("/api/metrics")
    async def metrics(request: Request) -> JSONResponse:
        """Counters worth watching on a long-running instance.

        provider_stats has been recording per-API successes and failures all
        along, purely to order providers by reliability; nothing ever showed
        it to anyone.
        """
        from .core import provider_stats
        from .core.progress import DownloadManager

        payload: dict[str, Any] = {
            "version": api.app_version,
            "multiuser": multiuser,
            "auth": bool(token) or multiuser,
        }

        # Everything below is instance-wide: provider stats, download totals
        # and queue depth aggregate every account's activity, and the client
        # count says how many other people are connected. In single-user mode
        # that is the operator looking at their own instance. In multi-user
        # mode it is one account being told how much the others are doing, so
        # it is shown only to an admin (see core/web_users.py, which grew
        # roles for exactly this). The counters keep being recorded either
        # way; only this projection is narrowed.
        visible = not multiuser
        if multiuser:
            from .core.web_users import is_admin

            visible = await run_in_threadpool(
                is_admin, getattr(request.state, "username", None)
            )

        if visible:
            payload["providers"] = await run_in_threadpool(provider_stats.snapshot)
            payload["websocket_clients"] = manager.count()

            with contextlib.suppress(Exception):
                payload["downloads"] = await DownloadManager().get_stats()

            if job_queue is not None:
                jobs = job_queue.list_all()
                counts: dict[str, int] = {}
                for job in jobs:
                    counts[job.status.value] = counts.get(job.status.value, 0) + 1
                payload["queue"] = {"total": len(jobs), "by_status": counts}

        return JSONResponse(payload)

    @app.get("/api/auth/status")
    async def auth_status(request: Request) -> JSONResponse:
        """Always registered, regardless of `multiuser` — lets the frontend
        decide whether to show a login screen at all without needing to
        know the server's configuration ahead of time. `logged_in` is
        always True when multiuser is off (nothing to log into).
        """
        if not multiuser:
            return JSONResponse({"multiuser": False, "logged_in": True})
        username = sessions.username_for(request.cookies.get(SESSION_COOKIE))
        return JSONResponse({"multiuser": True, "logged_in": username is not None})

    # ── Dynamic dispatcher for every whitelisted Api method ────────────────
    @app.post("/api/{method_name}")
    async def call_method(
        request: Request, method_name: str, payload: Any = Body(default=None)
    ) -> JSONResponse:
        if method_name not in ALLOWED_METHODS:
            return JSONResponse(
                {"error": f"Unknown or disallowed method: {method_name}"},
                status_code=404,
            )
        # Per-account in multi-user mode, so two people searching at once no
        # longer overwrite each other's current_tracks.
        target = api_for(request)
        fn = getattr(target, method_name, None)
        if fn is None:
            return JSONResponse(
                {"error": f"No such method: {method_name}"}, status_code=404
            )

        args: list = []
        kwargs: dict = {}
        if isinstance(payload, list):
            args = payload
        elif isinstance(payload, dict):
            kwargs = payload
        # else: no body / None -> call with no arguments

        try:
            # Every Api method is synchronous (some spawn their own internal
            # threads for long-running work and return immediately; a few,
            # like get_latest_version(), call asyncio.run() internally,
            # which would raise if called directly inside this async route —
            # running them in a worker thread avoids that).
            result = await run_in_threadpool(fn, *args, **kwargs)
        except TypeError:
            # Log the real exception (may include argument values/internal
            # details) server-side only; the client gets a generic message
            # so internals (paths, types, library versions, ...) aren't
            # exposed to whatever is calling this HTTP API.
            logger.exception("Bad arguments calling %s", method_name)
            return JSONResponse(
                {"error": f"Bad arguments for {method_name}"}, status_code=400
            )
        except Exception:
            logger.exception("Error calling %s", method_name)
            return JSONResponse(
                {"error": f"Internal error while calling {method_name}"},
                status_code=500,
            )

        return JSONResponse({"result": result})

    # ── Server-side folder browser (replaces the native folder dialog) ─────
    #
    # Both handlers below answer with one account's private view of the
    # filesystem: the folder it downloads into, and what is inside it.
    # Neither response carries a validator, which leaves a browser or an
    # intermediary free to reuse it heuristically — and on a shared machine
    # in multi-user mode, the next reuse can be for a different account. In
    # single-user mode there is nobody to reuse it for, so nothing is added.
    _private_headers = {"Cache-Control": "no-store"} if multiuser else None

    @app.get("/api/browse-folder")
    async def browse_folder(request: Request, path: str | None = None) -> JSONResponse:
        target_api = api_for(request)
        # In multi-user mode the only approved root is the caller's own
        # download folder, and it is also where browsing starts — landing
        # someone in $HOME would show them the other accounts by name.
        root = str(Path.home()) if not multiuser else target_api.download_dir
        try:
            # Resolve the requested path to a canonical, absolute form and
            # confirm it sits under an approved root *before* it is ever used
            # to touch the filesystem. Everything downstream operates on the
            # sanitized `safe_root` string, never on the raw `path` input.
            requested = os.path.realpath(os.path.expanduser(path) if path else root)
            if not _is_path_safe(Path(requested), target_api, allow_home=not multiuser):
                return JSONResponse(
                    {"error": "Access denied: path is outside approved directories"},
                    status_code=403,
                    headers=_private_headers,
                )
            base = Path(requested)
            if not base.is_dir():
                base = Path(os.path.realpath(root))
            directories = sorted(
                (
                    p.name
                    for p in base.iterdir()
                    if p.is_dir() and not p.name.startswith(".")
                ),
                key=str.lower,
            )
            files = sorted(
                (
                    p.name
                    for p in base.iterdir()
                    if p.is_file() and not p.name.startswith(".")
                ),
                key=str.lower,
            )
        except Exception:
            logger.exception("Error browsing folder %r", path)
            return JSONResponse(
                {"error": "Unable to browse this folder"},
                status_code=400,
                headers=_private_headers,
            )
        parent = str(base.parent) if base.parent != base else None
        return JSONResponse(
            {
                "path": str(base),
                "parent": parent,
                "directories": directories,
                "files": files,
            },
            headers=_private_headers,
        )

    @app.get("/api/get-home-dir")
    async def get_home_dir(request: Request) -> JSONResponse:
        """Where the folder browser should start.

        Named for what the frontend calls it. In multi-user mode it is the
        account's own download folder, not the OS home — the browser is only
        allowed inside that root there, so handing back $HOME would just
        start people somewhere they cannot open.
        """
        if multiuser:
            return JSONResponse(
                {"home_dir": api_for(request).download_dir},
                headers=_private_headers,
            )
        return JSONResponse({"home_dir": str(Path.home())})

    # ── WebSocket: push channel for log/progress/metadata/etc. ─────────────
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        # Neither middleware above runs for WebSocket upgrades — @app.middleware
        # ("http") only sees scope["type"] == "http" — so *both* gates have to be
        # repeated here. This channel carries every push event the app emits
        # (logs, progress, metadata, on-disk paths), so leaving either one out
        # means an unauthenticated client can watch everything the instance does.
        if token:
            supplied = ws.query_params.get(WEB_TOKEN_QUERY_PARAM) or ws.cookies.get(
                WEB_TOKEN_COOKIE
            )
            if not _token_matches(supplied, token):
                await ws.close(code=1008)  # 1008 = Policy Violation
                return
        ws_owner: str | None = None
        if multiuser:
            assert sessions is not None  # always set together with multiuser=True
            ws_owner = sessions.username_for(ws.cookies.get(SESSION_COOKIE))
            if ws_owner is None:
                await ws.close(code=1008)
                return
            # Make sure the account's Api exists now, so events it emits have
            # somewhere to be addressed even before its first API call.
            registry.get(ws_owner)
        await manager.connect(ws, owner=ws_owner)
        try:
            while True:
                # The frontend doesn't need to send anything; this just
                # keeps the connection open and detects disconnects.
                await ws.receive_text()
        except WebSocketDisconnect:
            manager.disconnect(ws)
        except Exception:
            manager.disconnect(ws)

    # ── The versioned REST API (/api/v1) ──────────────────────────────────
    #
    # Additive: the RPC bridge above is untouched and the frontend still uses
    # it. This is the surface for everything that is *not* our frontend —
    # bots, scripts, other services — which needs a declared schema rather
    # than a list of GUI method names. See webapi/__init__.py.
    #
    # Mounted after the middleware that gates /api/*, so it inherits the same
    # token and session auth rather than reimplementing either.
    from .webapi import ApiDeps, build_v1_router

    app.include_router(
        build_v1_router(
            ApiDeps(
                api_for=api_for,
                multiuser=multiuser,
                token_required=bool(token),
                job_queue=job_queue,
                username_for=lambda request: getattr(request.state, "username", None),
            )
        )
    )

    # ── Frontend: same static files the desktop build uses, with a small
    #    script injected so window.pywebview.api exists in a plain browser.
    @app.get("/")
    async def index() -> HTMLResponse:
        html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
        inject = (
            "<script>window.__SPOTIFLAC_WEB_MODE__ = true;</script>\n"
            '<script src="/web-shim.js?v=20260817"></script>\n'
        )
        html = html.replace(
            '<script src="toast-system.js?v=20260817"></script>',
            inject + '<script src="toast-system.js?v=20260817"></script>',
        )
        return HTMLResponse(html)

    @app.get("/web-shim.js")
    async def web_shim() -> FileResponse:
        return FileResponse(
            FRONTEND_DIR / "web-shim.js", media_type="application/javascript"
        )

    # Everything else (app.js, styles.css, assets/...) served as-is.
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR)), name="frontend")

    return app


def _warn_if_exposed(host: str, token: str | None) -> None:
    if host in ("127.0.0.1", "localhost"):
        return
    if token:
        logger.warning(
            "Binding SpotiFLAC's web GUI to %s exposes it beyond this machine. "
            "A --web-token is set, so requests need the token — but it travels "
            "as a plain-text query param/cookie with no HTTPS here, so treat "
            "this as basic access control, not a substitute for a trusted "
            "network or a real TLS-terminating reverse proxy.",
            host,
        )
    else:
        logger.warning(
            "Binding SpotiFLAC's web GUI to %s exposes it beyond this machine, "
            "with no authentication — anyone who can reach it can trigger "
            "downloads through your instance. Use 127.0.0.1 unless you have "
            "a specific, deliberate reason not to, and set --web-token (or put "
            "it behind your own authentication) if you do.",
            host,
        )


async def run_async(
    host: str = "127.0.0.1",
    port: int = 8000,
    token: str | None = None,
    multiuser: bool = False,
) -> None:
    """Use this from code that is already running inside an asyncio event
    loop (e.g. launcher.py's amain(), itself started via asyncio.run()).
    Calling uvicorn.run() there would try to start a second event loop with
    its own asyncio.run() and raise 'asyncio.run() cannot be called from a
    running event loop' — this awaits the server directly instead.

    `token`: see resolve_web_token() / --web-token. None (default) keeps the
    instance unauthenticated, exactly like before this option existed.
    `multiuser`: see --web-multiuser / create_app()'s docstring.
    """
    import uvicorn

    _warn_if_exposed(host, token)
    config = uvicorn.Config(
        create_app(token=token, multiuser=multiuser),
        host=host,
        port=port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    await server.serve()


def run(
    host: str = "127.0.0.1",
    port: int = 8000,
    token: str | None = None,
    multiuser: bool = False,
) -> None:
    """Use this only from plain (non-async) code, with no event loop already
    running — e.g. a standalone `python -m SpotiFLAC.webapp` invocation.
    From inside launcher.py's amain() (async), use run_async() and await it
    instead, or this will raise the same nested-loop error it exists to avoid.
    """
    import uvicorn

    _warn_if_exposed(host, token)
    uvicorn.run(
        create_app(token=token, multiuser=multiuser),
        host=host,
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--web-token",
        dest="token",
        default=None,
        help=f"Shared secret required on every request; falls back to "
        f"${WEB_TOKEN_ENV}. Unset (default) means no authentication.",
    )
    parser.add_argument(
        "--web-multiuser",
        action="store_true",
        help="Require per-account login (see core/web_users.py to create "
        "accounts first) instead of (or alongside) --web-token.",
    )
    args = parser.parse_args()
    args.token = resolve_web_token(args.token)
    run(host=args.host, port=args.port, token=args.token, multiuser=args.web_multiuser)
