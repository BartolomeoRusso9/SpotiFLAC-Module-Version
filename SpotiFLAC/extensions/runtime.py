"""extensions/runtime.py — JSRuntime.

Starts the Node.js _bridge.js process and provides a Python API
to call JavaScript extension methods synchronously.

Each JSRuntime instance represents a session with a single extension.
The extension's `storage` state persists for the entire runtime lifetime.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import shutil
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from typing_extensions import Self

if TYPE_CHECKING:
    from collections.abc import Awaitable

logger = logging.getLogger(__name__)

_BRIDGE_JS = Path(__file__).parent / "_bridge.js"


class ExtensionRuntimeError(RuntimeError):
    pass


class JSRuntime:
    """Manages a Node.js process that runs _bridge.js with a loaded extension.

    Usage:
        rt = JSRuntime(ext_path, settings={"token": "..."})
        rt.start()          # starts Node.js, waits for "ready"
        result = rt.call("handleURL", "https://soundcloud.com/...")
        rt.stop()

    As context manager:
        with JSRuntime(ext_path) as rt:
            result = rt.call("download", track_id, "mp3_128", "/tmp/out.mp3", None)

    Extensions with "requiredRuntimeFeatures": ["signedSession@1", ...] in the
    manifest call `session.signedFetch(method, path, body, headers)` from JS.
    This bridge does NOT implement that logic in Node (it would require
    duplicating all the HMAC signature/Turnstile handling already written in Python):
    it instead forwards the request to `session_handler`, an async Python function
    provided by the caller (typically JSExtensionProvider, which links it to
    SignedSessionClient/perform_signed_fetch).
    """

    def __init__(
        self,
        ext_path: str | Path,
        settings: dict | None = None,
        node_executable: str = "node",
        startup_timeout: float = 20.0,
        session_handler: Callable[[str, str, Any, dict], Awaitable[dict]] | None = None,
    ) -> None:
        self.ext_path = Path(ext_path)
        self.settings = settings or {}
        self.node_executable = node_executable
        self.startup_timeout = startup_timeout
        self.session_handler = session_handler

        self._proc: subprocess.Popen | None = None
        self._seq = 0
        self._pending: dict[int, queue.Queue] = {}
        self._progress_cbs: dict[int, Callable[[float], None]] = {}
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._ready_event = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    # ─────────────────────── lifecycle ────────────────────────

    def start(self) -> None:
        if not shutil.which(self.node_executable):
            msg = (
                f"Node.js not found ('{self.node_executable}'). "
                "Install Node.js ≥ 16 to use JS extensions."
            )
            raise ExtensionRuntimeError(
                msg,
            )
        if not _BRIDGE_JS.exists():
            msg = f"Bridge JS not found: {_BRIDGE_JS}"
            raise ExtensionRuntimeError(msg)
        if not self.ext_path.exists():
            msg = f"Extension not found: {self.ext_path}"
            raise ExtensionRuntimeError(msg)

        cmd = [
            self.node_executable,
            str(_BRIDGE_JS),
            str(self.ext_path),
            json.dumps(self.settings),
        ]

        # --- FIX OPENSSL 3.0 (NODE 17+) ---
        # Checks Node version and enables legacy algorithms (Blowfish) if needed
        env = os.environ.copy()
        try:
            v_out = subprocess.check_output([self.node_executable, "-v"], text=True)
            v_major = int(v_out.strip().lstrip("v").split(".")[0])
            if v_major >= 17:
                env["NODE_OPTIONS"] = (
                    env.get("NODE_OPTIONS", "") + " --openssl-legacy-provider"
                ).strip()
        except Exception:
            pass
        # ----------------------------------

        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=0,
            env=env,  # <-- Injects modified environment variables
        )

        # Thread that reads Node stdout and routes responses
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

        # Thread that drains stderr (extension logs)
        threading.Thread(target=self._drain_stderr, daemon=True).start()

        # Waits for "ready" signal from the extension
        if not self._ready_event.wait(timeout=self.startup_timeout):
            self.stop()
            msg = (
                f"Extension did not respond within {self.startup_timeout}s. "
                "Verify that the JS file is valid."
            )
            raise ExtensionRuntimeError(
                msg,
            )
        logger.debug("[JSRuntime] extension ready: %s", self.ext_path.name)

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.stdin.close()
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
        self._proc = None

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *_) -> None:
        self.stop()

    # ─────────────────────── calls ─────────────────────────

    def call(
        self,
        method: str,
        *args,
        progress_cb: Callable[[float], None] | None = None,
        timeout: float = 120.0,
    ) -> Any:
        """Calls a JavaScript extension method and returns the result.

        For methods with onProgress (e.g. download), pass progress_cb;
        the placeholder '__progress__' will be replaced by the JS function.
        """
        if not self._proc or self._proc.poll() is not None:
            msg = "JSRuntime not started or already terminated."
            raise ExtensionRuntimeError(msg)

        with self._lock:
            self._seq += 1
            seq = self._seq

        result_q: queue.Queue = queue.Queue()
        self._pending[seq] = result_q

        # Replaces None with '__progress__' if the method has onProgress
        final_args = list(args)
        if progress_cb is not None and final_args and final_args[-1] is None:
            final_args[-1] = "__progress__"
            self._progress_cbs[seq] = progress_cb

        msg = json.dumps({"id": seq, "call": method, "args": final_args}) + "\n"
        try:
            self._proc.stdin.write(msg.encode())
            self._proc.stdin.flush()
        except OSError as e:
            self._pending.pop(seq, None)
            msg_0 = f"Error writing to Node stdin: {e}"
            raise ExtensionRuntimeError(msg_0) from e

        try:
            resp = result_q.get(timeout=timeout)
        except queue.Empty:
            self._pending.pop(seq, None)
            self._progress_cbs.pop(seq, None)
            msg_0 = f"Timeout ({timeout}s) calling {method}"
            raise ExtensionRuntimeError(msg_0)
        finally:
            self._progress_cbs.pop(seq, None)

        if "error" in resp:
            msg_0 = f"[JS] {resp['error']}"
            raise ExtensionRuntimeError(msg_0)
        return resp.get("result")

    # ─────────────────────── internals ────────────────────────

    def _read_loop(self) -> None:
        """Reads Node.js stdout line by line and routes responses."""
        buf = b""
        while self._proc and self._proc.poll() is None:
            try:
                chunk = self._proc.stdout.read(1)
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
            if b"\n" not in buf:
                continue
            lines = buf.split(b"\n")
            buf = lines[-1]
            for raw in lines[:-1]:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                self._dispatch(msg)

        # Empties pending with error
        err = {"error": "Node.js process terminated unexpectedly"}
        for q in list(self._pending.values()):
            q.put(err)
        self._pending.clear()

    def _dispatch(self, msg: dict) -> None:
        if msg.get("type") == "ready":
            self._ready_event.set()
            return
        if msg.get("type") == "progress":
            call_id = msg.get("callId")
            cb = self._progress_cbs.get(call_id)
            if cb is not None:
                try:
                    # NEW: if the callback accepts bytes_received/bytes_total
                    # (signature "(fraction)" vs "(current, total)"), try first
                    # the extended form so JSExtensionProvider._progress_adapter
                    # can receive real bytes instead of just fraction 0..1
                    # when available (file.download now always provides them).
                    bytes_received = msg.get("bytesReceived")
                    bytes_total = msg.get("bytesTotal")
                    if bytes_received is not None and bytes_total:
                        cb(
                            float(msg.get("value", 0.0)),
                            int(bytes_received),
                            int(bytes_total),
                        )
                    else:
                        cb(float(msg.get("value", 0.0)))
                except TypeError:
                    # The registered callback accepts only (fraction,)
                    try:
                        cb(float(msg.get("value", 0.0)))
                    except Exception:
                        logger.debug("[JSRuntime] progress callback raised, ignored")
                except Exception:
                    logger.debug("[JSRuntime] progress callback raised, ignored")
            return
        if msg.get("type") == "log":
            level = msg.get("level", "info")
            getattr(logger, level, logger.info)("[EXT] %s", msg.get("msg", ""))
            return
        if msg.get("type") == "session_signed_fetch":
            self._handle_session_signed_fetch(msg)
            return
        seq = msg.get("id")
        if seq is None:
            return
        q = self._pending.pop(seq, None)
        if q:
            q.put(msg)

    def _handle_session_signed_fetch(self, msg: dict) -> None:
        """Handles a `session.signedFetch(...)` request made from the JS
        extension. Called from _read_loop (a separate thread).

        IMPORTANT: runs session_handler in a NEW AND ISOLATED asyncio event loop
        (asyncio.run), instead of trying to schedule it on the caller's loop
        (e.g. via run_coroutine_threadsafe). The reason: JSRuntime.call()
        is synchronous and blocks the calling thread/loop with a queue.get() —
        if that loop were shared and blocked there, a coroutine
        scheduled on it (run_coroutine_threadsafe) could NEVER
        run until call() returns, which itself waits for exactly
        this response: deadlock. An isolated loop here avoids the problem
        by construction, completely independent of how
        JSExtensionProvider is used (sync or async) in the rest of the program.
        """
        request_id = msg.get("requestId")
        method = msg.get("method", "GET")
        path = msg.get("path", "")
        body = msg.get("body")
        headers = msg.get("headers") or {}

        def _respond(result: dict) -> None:
            try:
                line = (
                    json.dumps(
                        {
                            "type": "session_signed_fetch_response",
                            "requestId": request_id,
                            "result": result,
                        },
                    )
                    + "\n"
                )
                self._proc.stdin.write(line.encode())
                self._proc.stdin.flush()
            except Exception as e:
                logger.debug(
                    "[JSRuntime] unable to respond to session.signedFetch: %s",
                    e,
                )

        if self.session_handler is None:
            _respond({"error": "session.signedFetch: no session_handler configured"})
            return

        async def _run() -> dict:
            try:
                return await self.session_handler(method, path, body, headers)
            except Exception as e:
                return {"error": str(e)}

        try:
            result = asyncio.run(_run())
        except Exception as e:
            result = {"error": str(e)}
        _respond(result)

    def _drain_stderr(self) -> None:
        try:
            for raw in self._proc.stderr:
                line = raw.rstrip(b"\n").decode("utf-8", errors="replace")
                if line:
                    logger.debug("[EXT stderr] %s", line)
        except Exception:
            pass
