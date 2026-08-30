// SpotiFLAC/extensions/_fsguard.js — keep extension writes where they belong.
//
// Extensions are third-party JavaScript running as an ordinary Node process
// with the user's own permissions. Nothing stopped one writing to
// ~/.ssh/authorized_keys, to a shell profile, or — most pointedly — to
// ~/.spotiflac/trusted_keys.json, the Ed25519 root of trust that decides
// which registry entries count as signed. An extension that can add a key
// there can sign its own successors, which is the one thing the signing
// scheme exists to prevent.
//
// A music provider needs to write in exactly three places: the file it was
// asked to produce, its own directory, and a temporary directory. This
// preload (injected alongside _netguard.js from extensions/runtime.py)
// enforces that and denies the rest.
//
// The allow-list is seeded from SPOTIFLAC_EXT_WRITABLE_DIRS and extended at
// run time by the bridge, which registers each download's output path as it
// dispatches the call — see the `download` case in _bridge.js. That matters
// because the output directory is chosen per download (`--output`, the GUI's
// folder picker) and is not knowable when the process starts, so a purely
// static list would either be too narrow to work or too wide to mean
// anything.
//
// Same honesty as _netguard.js: this runs inside the extension's own
// process and an extension that set out to defeat it could. It stops the
// accidental and the opportunistic — a path built from unsanitised
// metadata, a zip extracted with "../" in its names — not hostile code.
// Real containment needs the OS.
//
// Set SPOTIFLAC_EXT_ALLOW_ANY_WRITE=1 to switch it off.

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const url = require('url');

if (process.env.SPOTIFLAC_EXT_ALLOW_ANY_WRITE === '1') {
  return;
}

const allowed = new Set();

function allow(dir) {
  if (!dir) return;
  try {
    const resolved = path.resolve(dir);
    allowed.add(resolved);
    // Also the real path. On macOS os.tmpdir() is /var/folders/... while the
    // filesystem answers /private/var/folders/..., so comparing a
    // realpath-ed target against an un-realpath-ed allow-list rejects the
    // temp directory this file explicitly permits.
    try {
      allowed.add(fs.realpathSync.native(resolved));
    } catch {
      /* not created yet, or not readable — the lexical form still stands */
    }
  } catch {
    /* an unresolvable path is simply not added */
  }
}

allow(os.tmpdir());
allow(process.cwd()); // the extension's own directory — runtime.py sets cwd
for (const dir of (process.env.SPOTIFLAC_EXT_WRITABLE_DIRS || '').split(path.delimiter)) {
  allow(dir.trim());
}

// The bridge calls this with the output path the host passed for a given
// download, which is by definition a path the host sanctioned.
global.__spotiflacAllowWrite = function allowWrite(target) {
  if (typeof target !== 'string' || !target) return;
  allow(path.dirname(path.resolve(target)));
};

function isAllowed(target) {
  let resolved;
  try {
    resolved = path.resolve(target);
  } catch {
    return false;
  }
  // realpath where it exists, so a symlink pointing out of an allowed
  // directory does not smuggle a write past the prefix check.
  try {
    resolved = fs.realpathSync.native(path.dirname(resolved)) + path.sep +
      path.basename(resolved);
  } catch {
    /* the parent may not exist yet — fall back to the lexical path */
  }
  for (const dir of allowed) {
    if (resolved === dir || resolved.startsWith(dir + path.sep)) return true;
  }
  return false;
}

function refuse(target) {
  const err = new Error(
    `[fsguard] blocked a write outside the allowed directories (${target}). ` +
      'Extensions may write to the download target, their own directory and ' +
      'a temp directory. Set SPOTIFLAC_EXT_ALLOW_ANY_WRITE=1 to disable.'
  );
  err.code = 'EACCES';
  return err;
}

// Node accepts a path as a string, a Buffer, or a file:// URL, and all three
// reach the same syscall. Only the string form was ever checked, so
// `fs.writeFileSync(Buffer.from('/root/.ssh/authorized_keys'), …)` and the
// URL form walked straight past the guard. Anything that is not one of the
// three (a file descriptor to fs.open, say) returns null and is left alone —
// an fd names no path, so there is no prefix to check.
function asPath(target) {
  if (typeof target === 'string') return target;
  if (Buffer.isBuffer(target)) return target.toString('utf8');
  if (target instanceof URL || (target && target.protocol === 'file:' && target.href)) {
    try {
      return url.fileURLToPath(target);
    } catch {
      // A file: URL Node itself will reject. Refusing is the safe answer:
      // returning null here would wave it through unchecked.
      return '\u0000invalid-file-url';
    }
  }
  return null;
}

// Every fs function that creates or modifies something, with the index of
// the argument naming the path it acts on. `link`/`symlink`/`rename` write
// at their *second* argument; `copyFile` and `cp` too.
//
// `cp` is listed defensively rather than because it currently leaks: on the
// Node in use it is implemented over `copyFileSync`/`mkdirSync`, so it is
// already refused through those. That is an implementation detail of
// Node's, not a promise, and `cp` recursively copies a whole tree onto a
// destination exactly as `copyFile` does — so it is checked directly
// instead of being left to depend on how Node happens to build it.
const GUARDED = {
  writeFile: 0, appendFile: 0, open: 0, truncate: 0, unlink: 0,
  rmdir: 0, rm: 0, mkdir: 0, chmod: 0, chown: 0, utimes: 0,
  createWriteStream: 0, writev: 0, mkdtemp: 0,
  rename: 1, copyFile: 1, cp: 1, link: 1, symlink: 1,
};

function guard(namespace, name, index, { promise = false } = {}) {
  const original = namespace[name];
  if (typeof original !== 'function') return;
  namespace[name] = function guarded(...args) {
    const target = asPath(args[index]);
    if (target !== null && !isAllowed(target)) {
      const err = refuse(target);
      if (promise) return Promise.reject(err);
      // Callback-style: hand the error to the callback if there is one,
      // otherwise throw as the sync form does.
      const callback = args[args.length - 1];
      if (typeof callback === 'function') {
        process.nextTick(() => callback(err));
        return undefined;
      }
      throw err;
    }
    return original.apply(this, args);
  };
}

for (const [name, index] of Object.entries(GUARDED)) {
  guard(fs, name, index);
  guard(fs, `${name}Sync`, index);
  if (fs.promises) guard(fs.promises, name, index, { promise: true });
}
