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

IMPORTANT — untested: this module was written without the ability to
install dependencies or run the server in this environment. Review and
exercise it (at minimum: import the app, hit each endpoint once, open the
WebSocket, do one real download) before relying on it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Request, WebSocket, WebSocketDisconnect
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
# What this does and doesn't do, in plain terms: a session cookie identifies
# *who is asking*, and gates /api/* + /ws behind having logged in as
# somebody. It does NOT give each account its own SpotiFLAC_API state —
# `api` above is one shared instance, so `current_tracks`, `download_dir`,
# and everything else on it is shared by every logged-in account, and the
# download-queue endpoints tag jobs with an owner for history/filtering
# without changing where the download itself writes to. Good enough for a
# household or small team who'd otherwise just share one login; not
# multi-tenant isolation for people who shouldn't see each other's search
# results or download folder.
SESSION_COOKIE = "spotiflac_session"


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


def _is_path_safe(candidate: Path, api) -> bool:
    """Check if candidate path is within approved roots (download_dir, home).

    Returns True if the resolved canonical path is a descendant of (or equal to)
    at least one approved root. Returns False otherwise (path traversal attempt).
    """
    try:
        resolved = candidate.resolve()
        # Approved roots: download_dir and user home
        approved_roots = [
            Path(api.download_dir).resolve(),
            Path.home().resolve(),
        ]
        for root in approved_roots:
            try:
                resolved.relative_to(root)
                return True
            except ValueError:
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
    "search_code",
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
    "add_trusted_key",
    "remove_trusted_key",
}


class ConnectionManager:
    """Tracks connected WebSocket clients and lets worker threads (where
    SpotiFLAC_API methods actually run) push events to them safely.
    """

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.discard(ws)

    async def _send_all(self, message: dict) -> None:
        dead = []
        for ws in list(self._connections):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._connections.discard(ws)

    def broadcast(self, fn_name: str, args: list) -> None:
        """Thread-safe: callable from any thread, including the worker
        threads download_tracks()/fetch_metadata()/etc. run in. Schedules
        the actual send onto the server's asyncio event loop.
        """
        if self._loop is None:
            return
        message = {"fn": fn_name, "args": args}
        try:
            asyncio.run_coroutine_threadsafe(self._send_all(message), self._loop)
        except Exception:
            logger.debug("WebSocket broadcast failed", exc_info=True)


def create_app(token: str | None = None, multiuser: bool = False) -> FastAPI:
    """`multiuser=True` layers per-account login on top of the same single
    SpotiFLAC_API instance every mode already shares — see the "Multi-user
    mode" note on SESSION_COOKIE below for exactly what that does and does
    not isolate between accounts before enabling it for anyone but
    yourself and people you'd hand raw shell access to anyway.
    """
    api = SpotiFLAC_API()
    manager = ConnectionManager()
    api._ws_broadcast = manager.broadcast

    sessions = None
    job_queue = None
    if multiuser:
        from .core.job_queue import JobQueue
        from .core.web_users import SessionStore

        sessions = SessionStore()

        def _run_queued_download(payload: dict) -> dict:
            api.download_tracks(payload["selected_indices"], payload["config"])
            return {"status": "dispatched"}

        job_queue = JobQueue(handler=_run_queued_download, workers=1)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        manager.bind_loop(asyncio.get_running_loop())
        # Mirrors what _on_loaded() does for the desktop window, minus the
        # window-specific bits (there is no self._window in web mode, so
        # every push below goes out over the WebSocket only).
        await run_in_threadpool(api.log, "Python backend connected (web mode).", "info")
        await run_in_threadpool(
            api.log,
            f"Default download folder: {api.download_dir}",
            "info",
        )
        await run_in_threadpool(api._check_ffmpeg_startup)
        await run_in_threadpool(api._check_node_startup)
        try:
            from .extensions.manager import ExtensionManager

            await run_in_threadpool(ExtensionManager)  # no auto-install by default
        except Exception as e:
            await run_in_threadpool(api.log, f"Extension init error: {e}", "warn")
        api._push("loadHistoryAndProfiles")
        api._push("__set_version_label", api.app_version)
        yield
        # No shutdown-side work (yet) — everything here is process-lifetime
        # state (threads, in-memory sessions/queue) that dies with the
        # process anyway.

    app = FastAPI(title="SpotiFLAC Web", lifespan=_lifespan)

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
        # Gates /api/* and /ws behind a logged-in session — see the
        # "Multi-user mode" note on SESSION_COOKIE above for what this does
        # and doesn't isolate between accounts. The frontend doesn't have a
        # login form yet: call POST /api/auth/login directly (curl, a
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
        async def auth_login(payload: dict = Body(...)) -> JSONResponse:
            from .core.web_users import verify_password

            username = str(payload.get("username", ""))
            password = str(payload.get("password", ""))
            valid = await run_in_threadpool(verify_password, username, password)
            if not valid:
                return JSONResponse(
                    {"error": "Invalid username or password"}, status_code=401
                )
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
            job = job_queue.submit(
                request.state.username,
                {
                    "selected_indices": payload.get("selected_indices", []),
                    "config": payload.get("config", {}),
                },
            )
            return JSONResponse({"job_id": job.id, "status": job.status.value})

        @app.get("/api/queue/mine")
        async def queue_mine(request: Request) -> JSONResponse:
            assert job_queue is not None  # always set together with multiuser=True
            jobs = job_queue.list_for(request.state.username)
            return JSONResponse({"jobs": [j.to_dict() for j in jobs]})

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
        method_name: str, payload: Any = Body(default=None)
    ) -> JSONResponse:
        if method_name not in ALLOWED_METHODS:
            return JSONResponse(
                {"error": f"Unknown or disallowed method: {method_name}"},
                status_code=404,
            )
        fn = getattr(api, method_name, None)
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
    @app.get("/api/browse-folder")
    async def browse_folder(path: str | None = None) -> JSONResponse:
        base = Path(path).expanduser() if path else Path.home()
        try:
            base = base.resolve()
            # Path traversal protection
            if not _is_path_safe(base, api):
                return JSONResponse(
                    {"error": "Access denied: path is outside approved directories"},
                    status_code=403,
                )
            if not base.is_dir():
                base = Path.home().resolve()
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
                {"error": "Unable to browse this folder"}, status_code=400
            )
        parent = str(base.parent) if base.parent != base else None
        return JSONResponse(
            {
                "path": str(base),
                "parent": parent,
                "directories": directories,
                "files": files,
            },
        )

    @app.get("/api/get-home-dir")
    async def get_home_dir() -> JSONResponse:
        """Returns the user's home directory path."""
        return JSONResponse({"home_dir": str(Path.home())})

    # ── WebSocket: push channel for log/progress/metadata/etc. ─────────────
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        if token:
            # The http middleware above never runs for WebSocket upgrades
            # (Starlette limitation), so the same check is repeated here.
            supplied = ws.query_params.get(WEB_TOKEN_QUERY_PARAM) or ws.cookies.get(
                WEB_TOKEN_COOKIE
            )
            if not _token_matches(supplied, token):
                await ws.close(code=1008)  # 1008 = Policy Violation
                return
        await manager.connect(ws)
        try:
            while True:
                # The frontend doesn't need to send anything; this just
                # keeps the connection open and detects disconnects.
                await ws.receive_text()
        except WebSocketDisconnect:
            manager.disconnect(ws)
        except Exception:
            manager.disconnect(ws)

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
