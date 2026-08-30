/**
 * _bridge.js — SpotiFLAC Extension Bridge (Node.js)
 *
 * Two modes (isMainThread):
 *   MAIN  – reads JSON commands from Python stdin, routes them to the Worker,
 *            handles the Worker's bridge requests (http/file) asynchronously,
 *            and writes JSON results to stdout.
 *   WORKER – runs the extension code; for each http/file call it uses
 *            Atomics.wait to block *synchronously* until the main thread
 *            has completed the request.
 *
 * SharedArrayBuffer protocol (layout):
 *   [0..3]  Int32  — state: 0=idle, 1=req_pending, 2=resp_ready
 *   [4..7]  Uint32 — payload length at [8..]
 *   [8..]   Buffer — JSON UTF-8 (request or response)
 *
 * stdin/stdout protocol with Python (JSON-line):
 *   Python→Node  { "id": N, "call": "download"|"handleURL"|..., "args": [...] }
 *   Node→Python  { "id": N, "result": ... }  |  { "id": N, "error": "..." }
 *   Node→Python  { "type": "ready" }          (on startup)
 *   Node→Python  { "type": "progress", "callId": N, "value": 0..1 }
 */

'use strict';

const {
  isMainThread, Worker, workerData, parentPort,
} = require('worker_threads');
const https  = require('https');
const http_  = require('http');
const fs     = require('fs');
const path   = require('path');
const rl     = require('readline');
const { URL } = require('url');

// ─────────────────────────────────────────────────────────────
//  WORKER THREAD
// ─────────────────────────────────────────────────────────────
if (!isMainThread) {
  const { sharedBuf, extPath, settings } = workerData;

  const STATE  = new Int32Array(sharedBuf, 0, 1);   // [0] stato
  const LEN    = new Uint32Array(sharedBuf, 4, 1);  // [0] lunghezza payload
  const DOFF   = 8;                                  // offset dati nel buffer

  // NEW: tracks the callId of the currently executing Python->Worker call,
  // so bridge_requests generated inside it (e.g. file.download) can be
  // associated with the same callId for routing progress messages to the
  // correct _progress_cbs[seq].
  let _currentCallId = null;

  /** Calls the main thread *synchronously* via SharedArrayBuffer. */
  function bridgeCall(method, args) {
    const payload = Buffer.from(
      JSON.stringify({ method, args, callId: _currentCallId }), // NEW: callId incluso
      'utf8'
    );
    if (DOFF + payload.length > sharedBuf.byteLength) {
      throw new Error(`Bridge payload too large: ${payload.length} bytes`);
    }
    payload.copy(Buffer.from(sharedBuf), DOFF);
    LEN[0] = payload.length;

    // Signal the main thread: request ready
    Atomics.store(STATE, 0, 1);
    parentPort.postMessage({ type: 'bridge_request' });

    // Wait for the main thread to write the response (state → 2)
    let waited = 0;
    while (Atomics.load(STATE, 0) !== 2) {
      const r = Atomics.wait(STATE, 0, 1, 200);
      waited += 200;
      if (waited > 60_000) throw new Error(`Bridge timeout for ${method}`);
    }

    const len  = LEN[0];
    const resp = JSON.parse(Buffer.from(sharedBuf, DOFF, len).toString('utf8'));
    Atomics.store(STATE, 0, 0);
    if (resp.error) throw new Error(resp.error);
    return resp.result;
  }

  // ── Globals esposti all'estensione ───────────────────────────
  const _mem = {};

  global.http = {
    get:  (url, headers)       => bridgeCall('http.get',  { url, headers: headers || {} }),
    post: (url, body, headers) => bridgeCall('http.post', { url, body,   headers: headers || {} }),
  };

  global.storage = {
    get:    (k)    => (_mem[k] !== undefined ? _mem[k] : null),
    set:    (k, v) => { _mem[k] = v; return true; },
    delete: (k)    => { delete _mem[k]; return true; },
  };

  global.file = {
    download: (url, outputPath, opts) =>
      bridgeCall('file.download', { url, outputPath, opts: opts || {} }),
    
    // New: synchronous methods to operate on files using native fs
    getSize: (filePath) => {
      try {
        const stats = require('fs').statSync(filePath);
        return { success: true, size: stats.size };
      } catch (e) {
        return { success: false, error: e.message };
      }
    },
    
    readBytes: (filePath, opts) => {
      try {
        const offset = opts.offset || 0;
        const length = opts.length || 0;
        const encoding = opts.encoding || 'base64';
        
        const fd = require('fs').openSync(filePath, 'r');
        const buffer = Buffer.alloc(length);
        const bytesRead = require('fs').readSync(fd, buffer, 0, length, offset);
        require('fs').closeSync(fd);
        
        // Se non abbiamo letto nulla, siamo a fine file (eof)
        if (bytesRead === 0) {
            return { success: true, bytes_read: 0, data: '', eof: true };
        }
        
        // Taglia il buffer ai byte effettivamente letti e codifica
        const data = buffer.subarray(0, bytesRead).toString(encoding);
        return { success: true, bytes_read: bytesRead, data: data, eof: bytesRead < length };
      } catch (e) {
        return { success: false, error: e.message };
      }
    },
    
    writeBytes: (filePath, dataB64, opts) => {
      try {
        const flag = opts.append ? 'a' : 'w';
        const buffer = Buffer.from(dataB64, opts.encoding || 'base64');
        require('fs').writeFileSync(filePath, buffer, { flag: flag });
        return { success: true, path: filePath };
      } catch (e) {
        return { success: false, error: e.message };
      }
    },
    
    delete: (filePath) => {
      try {
        require('fs').unlinkSync(filePath);
        return true;
      } catch (e) {
        return false;
      }
    }
  };

  global.log = {
    info:  (...a) => parentPort.postMessage({ type: 'log', level: 'info',  msg: a.join(' ') }),
    debug: (...a) => {},
    warn:  (...a) => parentPort.postMessage({ type: 'log', level: 'warn',  msg: a.join(' ') }),
    error: (...a) => parentPort.postMessage({ type: 'log', level: 'error', msg: a.join(' ') }),
  };

  global.utils = {
    randomUserAgent: () =>
      'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    appUserAgent: () => 'SpotiFLAC-Python/1.2',
    
    sha256: (input) =>
      require('crypto').createHash('sha256').update(String(input), 'utf8').digest('hex'),
      
    md5: (input) =>
      require('crypto').createHash('md5').update(String(input), 'utf8').digest('hex'),

    decryptBlockCipher: (dataB64, opts) => {
      try {
        const crypto = require('crypto');
        const key = Buffer.from(opts.key, opts.keyEncoding || 'hex');
        const iv = Buffer.from(opts.iv, opts.ivEncoding || 'hex');
        const input = Buffer.from(dataB64, opts.inputEncoding || 'base64');
        
        // Disattiva il padding se richiesto (Blowfish su Deezer usa padding personalizzato)
        const decipher = crypto.createDecipheriv('bf-cbc', key, iv);
        if (opts.padding === 'none') decipher.setAutoPadding(false);
        
        const output = Buffer.concat([decipher.update(input), decipher.final()]);
        return { success: true, data: output.toString(opts.outputEncoding || 'base64') };
      } catch (e) {
        return { success: false, error: e.message };
      }
    },
  };

  global.gobackend = global.utils;
  // Esposto SOLO se l'estensione dichiara "requiredRuntimeFeatures" con
  // "signedSession@1"/"sessionGrant@1" nel manifest (vedi provider.py):
  // il bridge Python inietta la config del manifest e gestisce lato Python
  // l'intero ciclo bootstrap/challenge/verify/exchange/refresh (incluso
  // l'eventuale apertura del browser per il Turnstile) tramite
  // SignedSessionClient/perform_signed_fetch — qui ci limitiamo a inoltrare
  // la richiesta firmata, esattamente come fa "session.signedFetch" nel
  // runtime reale dell'app (stessa forma di ritorno: {statusCode, body,
  // ok, headers, error, needsVerification, auth_url}).
  global.session = {
    signedFetch: (method, path, body, headers) =>
      bridgeCall('session.signedFetch', {
        method: method || 'GET',
        path: path || '',
        body: body === undefined ? null : body,
        headers: headers || {},
      }),
  };

  let _ext = null;
  global.registerExtension = (obj) => { _ext = obj; };

  // Carica il codice dell'estensione
  const code = fs.readFileSync(extPath, 'utf8');
  eval(code); // eslint-disable-line no-eval

  if (!_ext) throw new Error('Extension did not call registerExtension()');
  if (typeof _ext.initialize === 'function') _ext.initialize(settings || {});

  parentPort.postMessage({ type: 'ready' });

  // Receives commands from the main thread and executes them *synchronously*
  parentPort.on('message', ({ id, call, args }) => {
    _currentCallId = id; // NEW: rende id disponibile a bridgeCall() durante fn(...)
    try {
      const fn = _ext[call];
      if (typeof fn !== 'function') {
        parentPort.postMessage({ id, error: `No method: ${call}` });
        return;
      }
      // Wraps onProgress if the arg is the placeholder "__progress__"
      const finalArgs = (args || []).map(a =>
        a === '__progress__'
          ? (v) => parentPort.postMessage({ type: 'progress', callId: id, value: v })
          : a
      );
      // Tell the filesystem guard that the host sanctioned this path. The
      // output directory is chosen per download (--output, the GUI folder
      // picker), so it cannot be on a static allow-list — see _fsguard.js.
      if (call === 'download' && typeof global.__spotiflacAllowWrite === 'function') {
        for (const a of finalArgs) {
          if (typeof a === 'string' && (a.includes('/') || a.includes('\\'))) {
            global.__spotiflacAllowWrite(a);
          }
        }
      }
      const result = fn(...finalArgs);
      parentPort.postMessage({ id, result });
    } catch (e) {
      parentPort.postMessage({ id, error: (e && e.message) || String(e) });
    } finally {
      _currentCallId = null; // NEW
    }
  });

  return; // fine worker
}

// ─────────────────────────────────────────────────────────────
//  MAIN THREAD
// ─────────────────────────────────────────────────────────────

const EXT_PATH      = process.argv[2];
const EXT_SETTINGS  = JSON.parse(process.argv[3] || '{}');
const BUF_SIZE      = 8 + 16 * 1024 * 1024; // 16 MB data buffer

const sharedBuf  = new SharedArrayBuffer(BUF_SIZE);
const STATE      = new Int32Array(sharedBuf, 0, 1);
const LEN        = new Uint32Array(sharedBuf, 4, 1);
const DOFF       = 8;

let _pendingPy   = new Map();   // id → {resolve, reject}
let _progressCbs = new Map();   // callId → progressCallback (unused server-side, forwarded to stdout)
let _cmdSeq      = 0;

// Dedicated map for forwardToPython() (session.signedFetch): different from
// _pendingPy above, which is for call/arg responses sent FROM Python TO the
// Worker — here the Node main thread asks Python for something and waits for
// a response, so it needs its own ID space to avoid collisions.
let _pySessionPending = new Map();  // requestId → resolve
let _pySessionSeq     = 0;

/** Forwards a request to Python (via stdout) and waits for its response (via stdin). */
function forwardToPython(type, payload) {
  return new Promise((resolve) => {
    const requestId = ++_pySessionSeq;
    _pySessionPending.set(requestId, resolve);
    process.stdout.write(JSON.stringify({ type, requestId, ...payload }) + '\n');
  });
}

/** Esegue una bridge-request generata dal Worker. */
async function handleBridgeRequest() {
  if (Atomics.load(STATE, 0) !== 1) return;

  const len  = LEN[0];
  const req  = JSON.parse(Buffer.from(sharedBuf, DOFF, len).toString('utf8'));

  let result = null;
  let error  = null;

  try {
    const { method, args, callId } = req; // NEW: callId destrutturato
    if (method === 'http.get') {
      result = await nodeHttpRequest('GET', args.url, null, args.headers);
    } else if (method === 'http.post') {
      result = await nodeHttpRequest('POST', args.url, args.body, args.headers);
    } else if (method === 'file.download') {
      result = await nodeFileDownload(args.url, args.outputPath, args.opts, callId); // NEW: passa callId
    } else if (method === 'session.signedFetch') {
      // Not handled in pure Node: the real SignedSessionClient is in Python
      // (HMAC signing, bootstrap/challenge/verify/exchange flow, and
      // possible browser launch for Turnstile). Forward the request to Python
      // via stdout and wait for its response via stdin (see forwardToPython
      // below).
      result = await forwardToPython('session_signed_fetch', {
        method: args.method, path: args.path, body: args.body, headers: args.headers,
      });
    } else {
      error = `Unknown bridge method: ${method}`;
    }
  } catch (e) {
    error = (e && e.message) || String(e);
  }

  const resp = Buffer.from(JSON.stringify({ result, error }), 'utf8');
  resp.copy(Buffer.from(sharedBuf), DOFF);
  LEN[0] = resp.length;

  Atomics.store(STATE, 0, 2);
  Atomics.notify(STATE, 0, 1);
}

/** HTTP request asincrona con redirect e cookie base. */
function nodeHttpRequest(method, rawUrl, body, headers, _depth = 0) {
  return new Promise((resolve) => {
    if (_depth > 5) { resolve({ statusCode: 0, body: '', error: 'Too many redirects' }); return; }
    let u;
    try { u = new URL(rawUrl); } catch (e) {
      resolve({ statusCode: 0, body: '', error: `Invalid URL: ${rawUrl}` }); return;
    }
    const lib = u.protocol === 'https:' ? https : http_;

    const bodyBuf = body
      ? (Buffer.isBuffer(body) ? body : Buffer.from(typeof body === 'string' ? body : JSON.stringify(body), 'utf8'))
      : null;

    const opts = {
      hostname: u.hostname,
      port:     u.port || (u.protocol === 'https:' ? 443 : 80),
      path:     u.pathname + u.search,
      method,
      headers:  Object.assign({}, headers),
    };
    if (bodyBuf) opts.headers['Content-Length'] = bodyBuf.length;

    let data = '';
    const req = lib.request(opts, (res) => {
      const loc = res.headers['location'];
      if ([301, 302, 303, 307, 308].includes(res.statusCode) && loc) {
        const next = loc.startsWith('http') ? loc : `${u.protocol}//${u.host}${loc}`;
        res.resume();
        resolve(nodeHttpRequest('GET', next, null, headers, _depth + 1));
        return;
      }
      res.setEncoding('utf8');
      res.on('data', (d) => { data += d; });
      res.on('end', () => resolve({
        statusCode: res.statusCode,
        body:       data,
        headers:    res.headers,
        url:        rawUrl,
        error:      null,
      }));
    });
    req.on('error', (e) => resolve({ statusCode: 0, body: '', error: e.message, url: rawUrl }));
    req.setTimeout(30_000, () => { req.destroy(); resolve({ statusCode: 0, body: '', error: 'timeout' }); });
    if (bodyBuf) req.write(bodyBuf);
    req.end();
  });
}

/**
 * Downloads a URL to disk (streaming), emitting "progress" events to
 * Python each time at least PROGRESS_THRESHOLD bytes are received, with
 * speed calculated over the interval (not the total average) — the same
 * scheme as ItemProgressWriter in the Go backend
 * (progressUpdateThreshold = 128 * 1024).
 */
function nodeFileDownload(rawUrl, outputPath, opts, callId) {
  return new Promise((resolve) => {
    let u;
    try { u = new URL(rawUrl); } catch (e) {
      resolve({ success: false, error: `Invalid URL: ${rawUrl}` }); return;
    }
    const lib = u.protocol === 'https:' ? https : http_;

    // Crea directory se mancante
    const dir = path.dirname(outputPath);
    try { fs.mkdirSync(dir, { recursive: true }); } catch (_) {}

    const extraHeaders = (opts && opts.headers) || {};
    const reqOpts = {
      hostname: u.hostname,
      port:     u.port || (u.protocol === 'https:' ? 443 : 80),
      path:     u.pathname + u.search,
      method:   'GET',
      headers:  Object.assign({}, extraHeaders),
    };

    // ── Progress tracking (NEW) ──────────────────────────────────────
    const PROGRESS_THRESHOLD = 128 * 1024;
    let received = 0;
    let total = 0;
    let lastReportedBytes = 0;
    let lastReportedTime = Date.now();

    const emitProgress = (finalValue) => {
      if (!callId) return;
      const now = Date.now();
      const elapsedS = (now - lastReportedTime) / 1000;
      const bytesInInterval = received - lastReportedBytes;
      const speedMBps = elapsedS > 0 ? (bytesInInterval / (1024 * 1024)) / elapsedS : 0;
      const value = finalValue !== undefined
        ? finalValue
        : (total > 0 ? Math.min(0.999, received / total) : 0);

      process.stdout.write(JSON.stringify({
        type: 'progress',
        callId,
        value,
        bytesReceived: received,
        bytesTotal: total,
        speedMBps,
      }) + '\n');

      lastReportedBytes = received;
      lastReportedTime = now;
    };
    // ──────────────────────────────────────────────────────────────────

    const stream = fs.createWriteStream(outputPath);
    const req = lib.request(reqOpts, (res) => {
      if (res.statusCode >= 400) {
        res.resume();
        stream.close();
        fs.unlink(outputPath, () => {});
        resolve({ success: false, error: `HTTP ${res.statusCode}` });
        return;
      }

      total = parseInt(res.headers['content-length'] || '0', 10) || 0; // NEW

      res.on('data', (chunk) => { // NEW
        received += chunk.length;
        if (received - lastReportedBytes >= PROGRESS_THRESHOLD) {
          emitProgress();
        }
      });

      res.pipe(stream);
      stream.on('finish', () => {
        emitProgress(1); // NEW: segnale finale al 100%
        stream.close(() => {
          try {
            const sz = fs.statSync(outputPath).size;
            resolve({ success: true, path: outputPath, size: sz });
          } catch (e) {
            resolve({ success: false, error: e.message });
          }
        });
      });
    });
    req.on('error', (e) => {
      stream.close();
      fs.unlink(outputPath, () => {});
      resolve({ success: false, error: e.message });
    });
    req.setTimeout(120_000, () => {
      req.destroy();
      stream.close();
      resolve({ success: false, error: 'download timeout' });
    });
    req.end();
  });
}

// ── Avvia Worker ────────────────────────────────────────────
const worker = new Worker(__filename, {
  workerData: { sharedBuf, extPath: EXT_PATH, settings: EXT_SETTINGS },
});

worker.on('error', (e) => {
  process.stderr.write(`[BRIDGE WORKER ERROR] ${e.message}\n`);
  process.exit(1);
});

worker.on('message', async (msg) => {
  if (msg.type === 'bridge_request') {
    await handleBridgeRequest();
    return;
  }
  if (msg.type === 'ready') {
    process.stdout.write(JSON.stringify({ type: 'ready' }) + '\n');
    return;
  }
  if (msg.type === 'log') {
    process.stderr.write(`[EXT ${msg.level.toUpperCase()}] ${msg.msg}\n`);
    return;
  }
  if (msg.type === 'progress') {
    process.stdout.write(JSON.stringify({ type: 'progress', callId: msg.callId, value: msg.value }) + '\n');
    return;
  }
  // Response to a Python command
  const { id, result, error } = msg;
  const cb = _pendingPy.get(id);
  if (!cb) return;
  _pendingPy.delete(id);
  if (error) cb.reject(new Error(error));
  else        cb.resolve(result);
});

/** Sends a command to the Worker and waits for the response. */
function callWorker(call, args) {
  return new Promise((resolve, reject) => {
    const id = ++_cmdSeq;
    _pendingPy.set(id, { resolve, reject });
    worker.postMessage({ id, call, args });
  });
}

// ── Legge comandi JSON dal stdin (Python) ───────────────────
const stdinRL = rl.createInterface({ input: process.stdin, terminal: false });
stdinRL.on('line', async (line) => {
  line = line.trim();
  if (!line) return;
  let req;
  try { req = JSON.parse(line); } catch (_) { return; }

  // Response from Python to a forwardToPython('session_signed_fetch', ...):
  // resolves the Promise waiting in the Worker; it is not a command to
  // execute.
  if (req.type === 'session_signed_fetch_response') {
    const resolve = _pySessionPending.get(req.requestId);
    if (resolve) {
      _pySessionPending.delete(req.requestId);
      resolve(req.result);
    }
    return;
  }

  const { id, call, args } = req;
  try {
    const result = await callWorker(call, args);
    process.stdout.write(JSON.stringify({ id, result }) + '\n');
  } catch (e) {
    process.stdout.write(JSON.stringify({ id, error: (e && e.message) || String(e) }) + '\n');
  }
});

stdinRL.on('close', () => {
  worker.terminate();
  process.exit(0);
});