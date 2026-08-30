// SpotiFLAC/extensions/_netguard.js — keep extensions off the local network.
//
// Extensions are third-party JavaScript fetched from registries, and they
// run as an ordinary Node subprocess with an ordinary network stack. That
// means an extension can reach anything this machine can: SpotiFLAC's own
// `--web` server on 127.0.0.1, a NAS on the LAN, a router's admin page, or
// 169.254.169.254 for cloud credentials when SpotiFLAC runs in the Docker
// image it ships.
//
// None of that is anything a music provider needs. This preload (injected
// via NODE_OPTIONS --require, see extensions/runtime.py) refuses outbound
// connections to private and local addresses before the socket is opened,
// and re-checks after DNS resolves so that a hostname pointing at
// 127.0.0.1 is caught too — DNS rebinding is otherwise the obvious way
// past an address check done on the URL alone.
//
// What this is and is not
// -----------------------
// It runs inside the extension's own process, so an extension that set out
// to defeat it could. It is a guard against the accidental and the
// opportunistic — an extension that follows a redirect somewhere it should
// not, or resolves a URL that came from a playlist — not a containment
// boundary against hostile code. Real containment needs the OS (a network
// namespace, or sandbox-exec) and is a separate job.
//
// Set SPOTIFLAC_EXT_ALLOW_PRIVATE_NETWORK=1 to switch it off, which is
// wanted when an extension is deliberately pointed at a self-hosted API on
// the LAN (a local Qobuz mirror, say).

'use strict';

const net = require('net');
const dns = require('dns');

if (process.env.SPOTIFLAC_EXT_ALLOW_PRIVATE_NETWORK === '1') {
  return;
}

// IPv4 ranges that are not the public internet. Ordered by how likely they
// are to matter here rather than numerically.
const BLOCKED_V4 = [
  [[127, 0, 0, 0], 8],      // loopback — SpotiFLAC's own --web server
  [[169, 254, 0, 0], 16],   // link-local — cloud instance metadata
  [[10, 0, 0, 0], 8],       // RFC1918
  [[172, 16, 0, 0], 12],    // RFC1918
  [[192, 168, 0, 0], 16],   // RFC1918
  [[100, 64, 0, 0], 10],    // carrier-grade NAT
  [[0, 0, 0, 0], 8],        // "this host"
  [[192, 0, 0, 0], 24],     // IETF protocol assignments
  [[198, 18, 0, 0], 15],    // benchmarking
];

function v4Blocked(address) {
  const parts = address.split('.').map(Number);
  if (parts.length !== 4 || parts.some((n) => !Number.isInteger(n) || n < 0 || n > 255)) {
    return false;
  }
  const value = ((parts[0] << 24) >>> 0) + (parts[1] << 16) + (parts[2] << 8) + parts[3];
  for (const [prefix, bits] of BLOCKED_V4) {
    const base =
      ((prefix[0] << 24) >>> 0) + (prefix[1] << 16) + (prefix[2] << 8) + prefix[3];
    const mask = bits === 0 ? 0 : (~0 << (32 - bits)) >>> 0;
    if ((value & mask) >>> 0 === (base & mask) >>> 0) return true;
  }
  return false;
}

function v6Blocked(address) {
  const a = address.toLowerCase().split('%')[0];
  if (a === '::1' || a === '::' || a === '0:0:0:0:0:0:0:1') return true;
  // Unique-local (fc00::/7) and link-local (fe80::/10).
  if (/^f[cd]/.test(a) || /^fe[89ab]/.test(a)) return true;
  // ::ffff:127.0.0.1 and friends — an IPv4 address wearing an IPv6 coat.
  const mapped = a.match(/^::ffff:(\d+\.\d+\.\d+\.\d+)$/);
  if (mapped) return v4Blocked(mapped[1]);
  return false;
}

function isBlocked(address) {
  if (!address || typeof address !== 'string') return false;
  return address.includes(':') ? v6Blocked(address) : v4Blocked(address);
}

function refuse(address) {
  const err = new Error(
    `[netguard] blocked connection to a private/local address (${address}). ` +
      'Set SPOTIFLAC_EXT_ALLOW_PRIVATE_NETWORK=1 if this extension is meant ' +
      'to reach a self-hosted service.'
  );
  err.code = 'EACCES';
  return err;
}

// 1. The address the socket is actually about to dial. This is the check
//    that matters: everything above it in the stack — http, https, fetch,
//    any third-party client an extension bundles — ends up here.
// Node normalises connect()'s arguments before calling this, so the real
// arguments can arrive wrapped in an array — `connect([options, cb])` — as
// well as in the documented `connect(options)` and `connect(port, host)`
// forms. Reading args[0].host without unwrapping finds undefined on the
// array and lets every literal IP straight through, which is exactly what
// the first version of this file did.
function hostFromConnectArgs(args) {
  const flat = Array.isArray(args[0]) ? args[0] : args;
  for (const candidate of flat) {
    if (candidate && typeof candidate === 'object') {
      const host = candidate.host || candidate.hostname;
      if (typeof host === 'string' && host) return host;
    }
  }
  // connect(port, host[, cb])
  const positional = flat.find((value, index) => index > 0 && typeof value === 'string');
  return positional || null;
}

const originalConnect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function connect(...args) {
  const host = hostFromConnectArgs(args);
  if (isBlocked(host)) {
    const err = refuse(host);
    // Match Node's own asynchronous error delivery rather than throwing
    // from connect(), which callers do not expect.
    process.nextTick(() => this.emit('error', err));
    return this;
  }
  return originalConnect.apply(this, args);
};

// 2. And after resolution, so that a hostname that resolves to a private
//    address is caught as well. Without this, `http://localtest.me/`
//    (which resolves to 127.0.0.1) walks straight past the check above.
function guardLookup(original) {
  return function lookup(hostname, options, callback) {
    const cb = typeof options === 'function' ? options : callback;
    const opts = typeof options === 'function' ? {} : options;
    const wrapped = (err, address, family) => {
      if (!err) {
        if (Array.isArray(address)) {
          const allowed = address.filter((entry) => !isBlocked(entry.address));
          if (allowed.length === 0 && address.length > 0) {
            return cb(refuse(address[0].address));
          }
          return cb(null, allowed, family);
        }
        if (isBlocked(address)) return cb(refuse(address));
      }
      return cb(err, address, family);
    };
    return original.call(dns, hostname, opts, wrapped);
  };
}

dns.lookup = guardLookup(dns.lookup);
if (dns.promises && dns.promises.lookup) {
  const originalPromisesLookup = dns.promises.lookup.bind(dns.promises);
  dns.promises.lookup = async function lookup(hostname, options) {
    const result = await originalPromisesLookup(hostname, options);
    const address = typeof result === 'string' ? result : result && result.address;
    if (isBlocked(address)) throw refuse(address);
    return result;
  };
}
