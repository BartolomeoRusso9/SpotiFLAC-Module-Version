// SpotiFLAC service worker — exists to make the web GUI installable
// (Chrome/Edge/Android's "Add to Home Screen" / standalone-window install
// prompt requires a registered service worker with a fetch handler), not
// to make it work offline in any meaningful sense: this is a thin client
// for a *local* backend (webapp.py, on the same host) — "offline" mostly
// means "the local server isn't running," which no amount of caching fixes.
//
// Deliberately NETWORK-FIRST for everything, cache used only as a last
// resort when a request outright fails (e.g. a page reload during a brief
// restart of the local server). webapp.py's own `_no_cache_frontend`
// middleware sends `Cache-Control: no-store` on every .js/.css/.html
// response specifically so a browser never runs stale frontend code after
// an update — a naive cache-first service worker would silently defeat
// that from underneath it. This one only ever falls back to a cached
// response; it never prefers one over a live network response.
//
// /api/* and the /ws WebSocket are never cached or intercepted for
// fallback — serving a stale API response (or trying to "fall back" a
// WebSocket upgrade, which fetch/caches can't represent anyway) would be
// actively wrong, not just unhelpful.

const CACHE_NAME = "spotiflac-shell-v1";
const SHELL_FILES = [
  "./",
  "index.html",
  "styles.css",
  "app.js",
  "toast-system.js",
  "web-shim.js",
  "manifest.json",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(SHELL_FILES))
      .catch(() => {
        // Best-effort: a single missing/blocked file must not abort
        // installation of the service worker itself.
      })
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(
          names
            .filter((name) => name !== CACHE_NAME)
            .map((name) => caches.delete(name))
        )
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;

  // Only ever handle same-origin GETs for the static shell. Everything
  // else (API calls, cross-origin requests, non-GET methods) passes
  // through untouched — the browser's default network handling applies.
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname === "/ws") return;

  event.respondWith(
    fetch(request)
      .then((response) => {
        // Refresh the shell cache with whatever the network just returned,
        // without ever using this response INSTEAD of that same live
        // response for the current request.
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
        return response;
      })
      .catch(() => caches.match(request))
  );
});
