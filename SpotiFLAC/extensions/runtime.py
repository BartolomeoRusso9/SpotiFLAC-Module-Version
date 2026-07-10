"""
extensions/runtime.py — JSRuntime

Avvia il processo Node.js _bridge.js e fornisce un'API Python
per chiamare i metodi dell'estensione JS in modo sincrono.

Ogni istanza di JSRuntime rappresenta una sessione con una singola estensione.
Lo stato `storage` dell'estensione è persistente per tutta la vita del runtime.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from ..core.signed_session import SignedSessionClient, client_from_manifest, perform_signed_fetch

logger = logging.getLogger(__name__)

_BRIDGE_JS = Path(__file__).parent / "_bridge.js"


class ExtensionRuntimeError(RuntimeError):
    pass


class JSRuntime:
    """
    Gestisce un processo Node.js che esegue il _bridge.js con un'estensione caricata.

    Uso:
        rt = JSRuntime(ext_path, settings={"token": "..."})
        rt.start()          # avvia Node.js, aspetta "ready"
        result = rt.call("handleURL", "https://soundcloud.com/...")
        rt.stop()

    Come context manager:
        with JSRuntime(ext_path) as rt:
            result = rt.call("download", track_id, "mp3_128", "/tmp/out.mp3", None)
    """

    def __init__(
        self,
        ext_path: str | Path,
        settings: dict | None = None,
        node_executable: str = "node",
        startup_timeout: float = 20.0,
        signed_session_config: dict | None = None,
    ) -> None:
        self.ext_path        = Path(ext_path)
        self.settings        = settings or {}
        self.node_executable = node_executable
        self.startup_timeout = startup_timeout
        # manifest["signedSession"] block, if the extension declares one
        # (requiredRuntimeFeatures: ["signedSession@1", ...]). Lazily turned
        # into a SignedSessionClient the first time the JS side calls
        # session.signedFetch(...).
        self._signed_session_config = signed_session_config
        self._signed_session_client: SignedSessionClient | None = None
        self._signed_session_lock = threading.Lock()
        # A single dedicated event loop for the lifetime of this runtime:
        # SignedSessionClient holds a persistent httpx.AsyncClient, which
        # must not be reused across different event loops (as a fresh
        # asyncio.run() per call would do under concurrent requests).
        self._session_loop: asyncio.AbstractEventLoop | None = None
        self._session_loop_thread: threading.Thread | None = None

        self._proc:        subprocess.Popen | None = None
        self._seq          = 0
        self._pending:     dict[int, queue.Queue] = {}
        self._progress_cbs: dict[int, Callable[[float], None]] = {}
        self._lock         = threading.Lock()
        self._stdin_lock   = threading.Lock()
        self._reader:      threading.Thread | None = None
        self._ready_event  = threading.Event()

    # ─────────────────────── lifecycle ────────────────────────

    def start(self) -> None:
        if not shutil.which(self.node_executable):
            print("[SpotiFLAC] Node.js not found, attempting automatic installation...")
            self._auto_install_node()
            if shutil.which(self.node_executable):
                print("[SpotiFLAC] Node.js installed automatically.")
        if not shutil.which(self.node_executable):
            raise ExtensionRuntimeError(
                f"Node.js not found ('{self.node_executable}'). "
                "Install Node.js ≥ 16 to use JS extensions."
            )
        if not _BRIDGE_JS.exists():
            raise ExtensionRuntimeError(f"Bridge JS non trovato: {_BRIDGE_JS}")
        if not self.ext_path.exists():
            raise ExtensionRuntimeError(f"Estensione non trovata: {self.ext_path}")

        cmd = [
            self.node_executable,
            str(_BRIDGE_JS),
            str(self.ext_path),
            json.dumps(self.settings),
        ]

        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,                    # byte mode per robustezza
            bufsize=0,
        )

        # Thread che legge stdout di Node e smista le risposte
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

        # Thread che draina stderr (log estensione)
        threading.Thread(target=self._drain_stderr, daemon=True).start()

        # Aspetta il segnale "ready" dall'estensione
        if not self._ready_event.wait(timeout=self.startup_timeout):
            self.stop()
            raise ExtensionRuntimeError(
                f"L'estensione non ha risposto entro {self.startup_timeout}s. "
                "Verifica che il file JS sia valido."
            )
        logger.debug("[JSRuntime] extension ready: %s", self.ext_path.name)

    def _auto_install_node(self) -> None:
        if sys.platform.startswith("linux"):
            self._install_node_linux()
        elif sys.platform == "darwin":
            self._install_node_macos()
        elif sys.platform == "win32":
            self._install_node_windows()
        else:
            raise ExtensionRuntimeError(
                "Node.js non trovato e il sistema operativo non è supportato per "
                "l'installazione automatica. Installa Node.js ≥ 16 manualmente."
            )

        if not shutil.which(self.node_executable):
            raise ExtensionRuntimeError(
                "Installazione automatica di Node.js fallita. "
                "Installa Node.js ≥ 16 manualmente."
            )

        version = self._get_node_version()
        if version is None or version < 16:
            raise ExtensionRuntimeError(
                f"La versione di Node.js installata è insufficiente: {version}. "
                "Installa Node.js >= 16 manualmente."
            )

    def _run_install_command(self, cmd: list[str], description: str) -> None:
        if os.name != "nt":
            try:
                is_root = os.geteuid() == 0
            except AttributeError:
                is_root = False
            if not is_root:
                if shutil.which("sudo"):
                    cmd = ["sudo"] + cmd
                else:
                    raise ExtensionRuntimeError(
                        "L'installazione automatica di Node.js richiede privilegi di root. "
                        "Esegui il comando come root o installa Node.js manualmente."
                    )

        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            raise ExtensionRuntimeError(
                f"Installazione automatica di Node.js fallita ({description}): {result.stderr.strip()}"
            )

    def _install_node_linux(self) -> None:
        if shutil.which("apt-get"):
            self._run_install_command(["apt-get", "update"], "apt-get update")
            self._run_install_command(["apt-get", "install", "-y", "nodejs"], "apt-get install nodejs")
        elif shutil.which("dnf"):
            self._run_install_command(["dnf", "install", "-y", "nodejs"], "dnf install nodejs")
        elif shutil.which("yum"):
            self._run_install_command(["yum", "install", "-y", "nodejs"], "yum install nodejs")
        elif shutil.which("pacman"):
            self._run_install_command(["pacman", "-Sy", "--noconfirm", "nodejs"], "pacman install nodejs")
        else:
            raise ExtensionRuntimeError(
                "Nessun gestore pacchetti supportato trovato per installare Node.js automaticamente. "
                "Installa Node.js ≥ 16 manualmente."
            )

    def _install_node_macos(self) -> None:
        if shutil.which("brew"):
            self._run_install_command(["brew", "install", "node"], "brew install node")
        else:
            raise ExtensionRuntimeError(
                "Homebrew non è installato. Installa Homebrew o Node.js manualmente."
            )

    def _install_node_windows(self) -> None:
        if shutil.which("winget"):
            self._run_install_command(["winget", "install", "OpenJS.NodeJS", "/quiet"], "winget install Node.js")
        elif shutil.which("choco"):
            self._run_install_command(["choco", "install", "nodejs.install", "-y"], "choco install nodejs")
        else:
            raise ExtensionRuntimeError(
                "Nessun gestore pacchetti Windows supportato trovato per installare Node.js automaticamente. "
                "Installa Node.js ≥ 16 manualmente."
            )

    def _get_node_version(self) -> int | None:
        try:
            result = subprocess.run(
                [self.node_executable, "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
        except Exception:
            return None
        version = result.stdout.strip()
        if version.startswith("v"):
            version = version[1:]
        try:
            return int(version.split(".")[0])
        except (ValueError, IndexError):
            return None

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.stdin.close()
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
        self._proc = None

        if self._session_loop is not None:
            try:
                if self._signed_session_client is not None:
                    asyncio.run_coroutine_threadsafe(
                        self._signed_session_client.aclose(), self._session_loop
                    ).result(timeout=5)
            except Exception:
                pass
            self._session_loop.call_soon_threadsafe(self._session_loop.stop)
            if self._session_loop_thread is not None:
                self._session_loop_thread.join(timeout=5)
            self._session_loop = None
            self._session_loop_thread = None

    def __enter__(self) -> "JSRuntime":
        self.start()
        return self

    def __exit__(self, *_) -> None:
        self.stop()

    # ─────────────────────── chiamate ─────────────────────────

    def call(
        self,
        method: str,
        *args,
        progress_cb: Callable[[float], None] | None = None,
        timeout: float = 120.0,
    ) -> Any:
        """
        Chiama un metodo dell'estensione JS e ritorna il risultato.

        Per i metodi con onProgress (es. download), passa progress_cb;
        il placeholder '__progress__' verrà sostituito dalla funzione JS.
        """
        if not self._proc or self._proc.poll() is not None:
            raise ExtensionRuntimeError("JSRuntime non avviato o già terminato.")

        with self._lock:
            self._seq += 1
            seq = self._seq

        result_q: queue.Queue = queue.Queue()
        self._pending[seq] = result_q

        # Sostituisce None con '__progress__' se il metodo ha onProgress
        final_args = list(args)
        if progress_cb is not None and final_args and final_args[-1] is None:
            final_args[-1] = "__progress__"
            self._progress_cbs[seq] = progress_cb

        msg = json.dumps({"id": seq, "call": method, "args": final_args}) + "\n"
        try:
            self._write_stdin(msg)
        except OSError as e:
            self._pending.pop(seq, None)
            raise ExtensionRuntimeError(f"Errore scrittura stdin Node: {e}") from e

        try:
            resp = result_q.get(timeout=timeout)
        except queue.Empty:
            self._pending.pop(seq, None)
            self._progress_cbs.pop(seq, None)
            raise ExtensionRuntimeError(f"Timeout ({timeout}s) chiamando {method}")
        finally:
            self._progress_cbs.pop(seq, None)

        if "error" in resp:
            raise ExtensionRuntimeError(f"[JS] {resp['error']}")
        return resp.get("result")

    # ─────────────────────── internals ────────────────────────

    def _read_loop(self) -> None:
        """Legge stdout di Node.js riga per riga e smista le risposte."""
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

        # Svuota pending con errore
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
                    cb(float(msg.get("value", 0.0)))
                except Exception:
                    logger.debug("[JSRuntime] progress callback raised, ignored")
            return
        if msg.get("type") == "log":
            level = msg.get("level", "info")
            getattr(logger, level, logger.info)("[EXT] %s", msg.get("msg", ""))
            return
        if msg.get("type") == "session_request":
            # Handled off the reader thread so a slow/blocking signed HTTP
            # call (or a Turnstile solve) never stalls stdout draining.
            threading.Thread(
                target=self._handle_session_request, args=(msg,), daemon=True
            ).start()
            return
        seq = msg.get("id")
        if seq is None:
            return
        q = self._pending.pop(seq, None)
        if q:
            q.put(msg)

    def _write_stdin(self, text: str) -> None:
        with self._stdin_lock:
            self._proc.stdin.write(text.encode())
            self._proc.stdin.flush()

    def _get_session_loop(self) -> asyncio.AbstractEventLoop:
        if self._session_loop is None:
            ready = threading.Event()

            def _run():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._session_loop = loop
                ready.set()
                loop.run_forever()

            self._session_loop_thread = threading.Thread(target=_run, daemon=True)
            self._session_loop_thread.start()
            ready.wait(timeout=5)
        return self._session_loop

    def _get_signed_session_client(self) -> SignedSessionClient:
        with self._signed_session_lock:
            if self._signed_session_client is None:
                if not self._signed_session_config:
                    raise ExtensionRuntimeError(
                        "Extension calls session.signedFetch() but its manifest "
                        "has no 'signedSession' block."
                    )
                self._signed_session_client = client_from_manifest(self._signed_session_config)
            return self._signed_session_client

    def _handle_session_request(self, msg: dict) -> None:
        """
        Services a `session.signedFetch(method, path, body, headers)` call
        forwarded by _bridge.js's Node main thread. Runs the async
        SignedSessionClient flow in a private event loop and writes the
        JSON result back to Node's stdin as a `session_response` message.
        """
        req_id = msg.get("id")
        args = msg.get("args") or {}
        try:
            client = self._get_signed_session_client()
            loop = self._get_session_loop()
            future = asyncio.run_coroutine_threadsafe(
                perform_signed_fetch(
                    client,
                    method=args.get("method", "GET"),
                    path=args.get("path", "/"),
                    body=args.get("body"),
                    headers=args.get("headers") or {},
                ),
                loop,
            )
            result = future.result(timeout=90)
            reply = {"type": "session_response", "id": req_id, "result": result}
        except Exception as exc:
            logger.debug("[JSRuntime] session_request failed: %s", exc)
            reply = {"type": "session_response", "id": req_id, "error": str(exc)}

        try:
            self._write_stdin(json.dumps(reply) + "\n")
        except OSError:
            pass  # Node process likely already gone; nothing more to do.

    def _drain_stderr(self) -> None:
        try:
            for raw in self._proc.stderr:
                line = raw.rstrip(b"\n").decode("utf-8", errors="replace")
                if line:
                    logger.debug("[EXT stderr] %s", line)
        except Exception:
            pass