/**
 * _bridge.js — SpotiFLAC Extension Bridge (Node.js)
 *
 * Due modalità (isMainThread):
 *   MAIN  – legge comandi JSON dal stdin di Python, li smista al Worker,
 *            gestisce le bridge-request del Worker (http/file) in modo asincrono,
 *            scrive risultati JSON su stdout.
 *   WORKER – esegue il codice dell'estensione; per ogni chiamata http/file
 *            usa Atomics.wait per bloccarsi in modo *sincrono* finché il main
 *            non ha completato la richiesta.
 *
 * Protocollo SharedArrayBuffer (layout):
 *   [0..3]  Int32  — stato: 0=idle, 1=req_pending, 2=resp_ready
 *   [4..7]  Uint32 — lunghezza payload a [8..]
 *   [8..]   Buffer — JSON UTF-8 (richiesta o risposta)
 *
 * Protocollo stdin/stdout con Python (JSON-line):
 *   Python→Node  { "id": N, "call": "download"|"handleURL"|..., "args": [...] }
 *   Node→Python  { "id": N, "result": ... }  |  { "id": N, "error": "..." }
 *   Node→Python  { "type": "ready" }          (all'avvio)
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

  /** Chiama il main thread in modo *sincrono* via SharedArrayBuffer. */
  function bridgeCall(method, args) {
    const payload = Buffer.from(JSON.stringify({ method, args }), 'utf8');
    if (DOFF + payload.length > sharedBuf.byteLength) {
      throw new Error(`Bridge payload too large: ${payload.length} bytes`);
    }
    payload.copy(Buffer.from(sharedBuf), DOFF);
    LEN[0] = payload.length;

    // Segnala al main: richiesta pronta
    Atomics.store(STATE, 0, 1);
    parentPort.postMessage({ type: 'bridge_request' });

    // Aspetta che il main scriva la risposta (stato → 2)
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
  };

  let _ext = null;
  global.registerExtension = (obj) => { _ext = obj; };

  // Carica il codice dell'estensione
  const code = fs.readFileSync(extPath, 'utf8');
  eval(code); // eslint-disable-line no-eval

  if (!_ext) throw new Error('Extension did not call registerExtension()');
  if (typeof _ext.initialize === 'function') _ext.initialize(settings || {});

  parentPort.postMessage({ type: 'ready' });

  // Riceve comandi dal main e li esegue *sincrono* (siamo già in un Worker)
  parentPort.on('message', ({ id, call, args }) => {
    try {
      const fn = _ext[call];
      if (typeof fn !== 'function') {
        parentPort.postMessage({ id, error: `No method: ${call}` });
        return;
      }
      // Wrappa onProgress se l'arg è il placeholder "__progress__"
      const finalArgs = (args || []).map(a =>
        a === '__progress__'
          ? (v) => parentPort.postMessage({ type: 'progress', callId: id, value: v })
          : a
      );
      const result = fn(...finalArgs);
      parentPort.postMessage({ id, result });
    } catch (e) {
      parentPort.postMessage({ id, error: (e && e.message) || String(e) });
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

/** Esegue una bridge-request generata dal Worker. */
async function handleBridgeRequest() {
  if (Atomics.load(STATE, 0) !== 1) return;

  const len  = LEN[0];
  const req  = JSON.parse(Buffer.from(sharedBuf, DOFF, len).toString('utf8'));

  let result = null;
  let error  = null;

  try {
    const { method, args } = req;
    if (method === 'http.get') {
      result = await nodeHttpRequest('GET', args.url, null, args.headers);
    } else if (method === 'http.post') {
      result = await nodeHttpRequest('POST', args.url, args.body, args.headers);
    } else if (method === 'file.download') {
      result = await nodeFileDownload(args.url, args.outputPath, args.opts);
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

/** Scarica un URL su disco (streaming). */
function nodeFileDownload(rawUrl, outputPath, opts) {
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

    const stream = fs.createWriteStream(outputPath);
    const req = lib.request(reqOpts, (res) => {
      if (res.statusCode >= 400) {
        res.resume();
        stream.close();
        fs.unlink(outputPath, () => {});
        resolve({ success: false, error: `HTTP ${res.statusCode}` });
        return;
      }
      res.pipe(stream);
      stream.on('finish', () => {
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
    // Invia al Python come evento separato (non bloccante)
    process.stdout.write(JSON.stringify({ type: 'progress', callId: msg.callId, value: msg.value }) + '\n');
    return;
  }
  // Risposta a un comando Python
  const { id, result, error } = msg;
  const cb = _pendingPy.get(id);
  if (!cb) return;
  _pendingPy.delete(id);
  if (error) cb.reject(new Error(error));
  else        cb.resolve(result);
});

/** Invia un comando al Worker e aspetta la risposta. */
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
