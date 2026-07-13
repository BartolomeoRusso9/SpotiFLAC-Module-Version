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
import threading
from pathlib import Path
from typing import Any, Awaitable, Callable

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

    Estensioni con "requiredRuntimeFeatures": ["signedSession@1", ...] nel
    manifest chiamano `session.signedFetch(method, path, body, headers)` dal
    JS. Questo bridge NON implementa quella logica in Node (richiederebbe
    duplicare tutta la firma HMAC/gestione Turnstile già scritta in Python):
    inoltra invece la richiesta a `session_handler`, una funzione async
    Python fornita dal chiamante (tipicamente JSExtensionProvider, che la
    collega a SignedSessionClient/perform_signed_fetch).
    """

    def __init__(
        self,
        ext_path: str | Path,
        settings: dict | None = None,
        node_executable: str = "node",
        startup_timeout: float = 20.0,
        session_handler: "Callable[[str, str, Any, dict], Awaitable[dict]] | None" = None,
    ) -> None:
        self.ext_path        = Path(ext_path)
        self.settings        = settings or {}
        self.node_executable = node_executable
        self.startup_timeout = startup_timeout
        self.session_handler = session_handler

        self._proc:        subprocess.Popen | None = None
        self._seq          = 0
        self._pending:     dict[int, queue.Queue] = {}
        self._progress_cbs: dict[int, Callable[[float], None]] = {}
        self._lock         = threading.Lock()
        self._reader:      threading.Thread | None = None
        self._ready_event  = threading.Event()
        self._loop:        "asyncio.AbstractEventLoop | None" = None

    # ─────────────────────── lifecycle ────────────────────────

    def start(self) -> None:
        if not shutil.which(self.node_executable):
            raise ExtensionRuntimeError(
                f"Node.js non trovato ('{self.node_executable}'). "
                "Installa Node.js ≥ 16 per usare le estensioni JS."
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

        # --- FIX OPENSSL 3.0 (NODE 17+) ---
        # Controlla la versione di Node e attiva i vecchi algoritmi (Blowfish) se necessario
        env = os.environ.copy()
        try:
            v_out = subprocess.check_output([self.node_executable, "-v"], text=True)
            v_major = int(v_out.strip().lstrip('v').split('.')[0])
            if v_major >= 17:
                env["NODE_OPTIONS"] = (env.get("NODE_OPTIONS", "") + " --openssl-legacy-provider").strip()
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
            env=env,  # <-- Inietta le variabili d'ambiente modificate
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

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.stdin.close()
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
        self._proc = None

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
            self._proc.stdin.write(msg.encode())
            self._proc.stdin.flush()
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
        """
        Gestisce una richiesta `session.signedFetch(...)` fatta dal JS
        dell'estensione. Chiamato da _read_loop (un thread separato).

        IMPORTANTE: esegue session_handler in un event loop asyncio NUOVO E
        ISOLATO (asyncio.run), invece di provare a schedularlo sul loop del
        chiamante (es. via run_coroutine_threadsafe). Il motivo: JSRuntime.call()
        è sincrono e blocca il thread/loop chiamante con una queue.get() —
        se quel loop fosse condiviso e bloccato lì dentro, una coroutine
        schedulata su di esso (run_coroutine_threadsafe) non potrebbe MAI
        girare finché call() non ritorna, che a sua volta aspetta proprio
        questa risposta: deadlock. Un loop isolato qui evita il problema
        per costruzione, del tutto indipendente da come viene usato
        JSExtensionProvider (sync o async) nel resto del programma.
        """
        request_id = msg.get("requestId")
        method  = msg.get("method", "GET")
        path    = msg.get("path", "")
        body    = msg.get("body")
        headers = msg.get("headers") or {}

        def _respond(result: dict) -> None:
            try:
                line = json.dumps({
                    "type": "session_signed_fetch_response",
                    "requestId": request_id,
                    "result": result,
                }) + "\n"
                self._proc.stdin.write(line.encode())
                self._proc.stdin.flush()
            except Exception as e:
                logger.debug("[JSRuntime] impossibile rispondere a session.signedFetch: %s", e)

        if self.session_handler is None:
            _respond({"error": "session.signedFetch: nessun session_handler configurato"})
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