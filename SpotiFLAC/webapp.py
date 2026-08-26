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
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .app import SpotiFLAC_API

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"


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
    "save_settings",
    "load_settings",
    "get_registries",
    "add_registry",
    "remove_registry",
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


def create_app() -> FastAPI:
    api = SpotiFLAC_API()
    manager = ConnectionManager()
    api._ws_broadcast = manager.broadcast

    app = FastAPI(title="SpotiFLAC Web")

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

    @app.on_event("startup")
    async def _on_startup() -> None:
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
        try:
            from .extensions.manager import ExtensionManager

            await run_in_threadpool(ExtensionManager)  # no auto-install by default
        except Exception as e:
            await run_in_threadpool(api.log, f"Extension init error: {e}", "warn")
        api._push("loadHistoryAndProfiles")
        api._push("__set_version_label", api.app_version)

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
            return JSONResponse({"error": "Unable to browse this folder"}, status_code=400)
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


def _warn_if_exposed(host: str) -> None:
    if host not in ("127.0.0.1", "localhost"):
        logger.warning(
            "Binding SpotiFLAC's web GUI to %s exposes it beyond this machine, "
            "with no authentication — anyone who can reach it can trigger "
            "downloads through your instance. Use 127.0.0.1 unless you have "
            "a specific, deliberate reason not to, and put it behind your "
            "own authentication if you do.",
            host,
        )


async def run_async(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Use this from code that is already running inside an asyncio event
    loop (e.g. launcher.py's amain(), itself started via asyncio.run()).
    Calling uvicorn.run() there would try to start a second event loop with
    its own asyncio.run() and raise 'asyncio.run() cannot be called from a
    running event loop' — this awaits the server directly instead.
    """
    import uvicorn

    _warn_if_exposed(host)
    config = uvicorn.Config(create_app(), host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


def run(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Use this only from plain (non-async) code, with no event loop already
    running — e.g. a standalone `python -m SpotiFLAC.webapp` invocation.
    From inside launcher.py's amain() (async), use run_async() and await it
    instead, or this will raise the same nested-loop error it exists to avoid.
    """
    import uvicorn

    _warn_if_exposed(host)
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    run(host=args.host, port=args.port)
