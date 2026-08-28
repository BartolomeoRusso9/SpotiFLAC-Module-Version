// web-shim.js — makes the existing app.js work unmodified in a plain
// browser, talking to SpotiFLAC/webapp.py (FastAPI) instead of pywebview.
//
// Only loaded by webapp.py's index() route (web mode). The desktop build
// loads frontend/index.html directly from disk via pywebview and never
// sees this file, so real pywebview.api is completely unaffected.
//
// Methods listed in ALLOWED_METHODS on the Python side become
// `window.pywebview.api.<name>(...)` calls that POST to /api/<name> with
// the arguments as a JSON array, matching how pywebview itself calls into
// Python (positional args). Window-chrome methods that only make sense for
// a native window (minimize/maximize/resize/move/destroy) are no-ops here.
// choose_folder() is replaced by a small built-in folder browser modal
// that calls set_download_dir() once the user picks a directory.

(function () {
  'use strict';

  async function callApi(name, args) {
    const res = await fetch('/api/' + name, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(args || []),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error((data && data.error) || ('Request failed: ' + name));
    }
    return data.result;
  }


  // ── Login (multi-user mode only) ───────────────────────────────────────
  //
  // Until now `--web-multiuser` had no way in from a browser at all: the
  // README's instruction was to POST /api/auth/login with curl and let the
  // cookie carry you. This puts a form in front of the app when the server
  // says one is needed, and gets out of the way entirely when it isn't —
  // single-user instances never see any of it.

  function buildLoginOverlay() {
    const overlay = document.createElement('div');
    overlay.id = 'sf-login-overlay';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-labelledby', 'sf-login-title');
    overlay.style.cssText = [
      'position:fixed', 'inset:0', 'z-index:99999',
      'display:flex', 'align-items:center', 'justify-content:center',
      'background:rgba(8,12,14,.86)', 'backdrop-filter:blur(6px)',
      'font-family:system-ui,-apple-system,Segoe UI,sans-serif',
    ].join(';');

    const card = document.createElement('form');
    card.style.cssText = [
      'background:#141a1d', 'color:#e6edee', 'padding:28px 30px',
      'border:1px solid #263238', 'border-radius:10px',
      'min-width:min(340px,90vw)', 'display:flex', 'flex-direction:column',
      'gap:14px', 'box-shadow:0 24px 60px -20px rgba(0,0,0,.8)',
    ].join(';');

    const title = document.createElement('h2');
    title.id = 'sf-login-title';
    title.textContent = 'Sign in to SpotiFLAC';
    title.style.cssText = 'margin:0;font-size:1.15rem;font-weight:600';

    const user = document.createElement('input');
    user.type = 'text';
    user.name = 'username';
    user.placeholder = 'Username';
    user.autocomplete = 'username';
    user.required = true;

    const pass = document.createElement('input');
    pass.type = 'password';
    pass.name = 'password';
    pass.placeholder = 'Password';
    pass.autocomplete = 'current-password';
    pass.required = true;

    for (const field of [user, pass]) {
      field.style.cssText = [
        'padding:10px 12px', 'border-radius:6px', 'border:1px solid #2d3a40',
        'background:#0e1417', 'color:inherit', 'font-size:.95rem',
      ].join(';');
    }

    const submit = document.createElement('button');
    submit.type = 'submit';
    submit.textContent = 'Sign in';
    submit.style.cssText = [
      'padding:10px 12px', 'border-radius:6px', 'border:0', 'cursor:pointer',
      'background:#1db954', 'color:#062313', 'font-weight:600',
      'font-size:.95rem',
    ].join(';');

    const error = document.createElement('p');
    error.setAttribute('role', 'alert');
    error.style.cssText = 'margin:0;min-height:1.2em;color:#f0685b;font-size:.85rem';

    card.append(title, user, pass, submit, error);
    overlay.appendChild(card);

    card.addEventListener('submit', async (event) => {
      event.preventDefault();
      submit.disabled = true;
      error.textContent = '';
      try {
        const res = await fetch('/api/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username: user.value, password: pass.value }),
        });
        if (res.ok) {
          // Reload rather than patch state in place: the whole app boots
          // against one account, and this way it boots against the right one.
          window.location.reload();
          return;
        }
        const data = await res.json().catch(() => ({}));
        // 429 carries the backoff from LoginRateLimiter; say how long rather
        // than leaving the button looking broken.
        const retry = res.headers.get('Retry-After');
        error.textContent = res.status === 429 && retry
          ? `Too many attempts. Try again in ${retry}s.`
          : (data.error || 'Sign-in failed.');
      } catch {
        error.textContent = 'Could not reach the server.';
      } finally {
        submit.disabled = false;
        pass.value = '';
      }
    });

    return { overlay, user };
  }

  async function ensureSignedIn() {
    let status;
    try {
      status = await (await fetch('/api/auth/status')).json();
    } catch {
      return true;  // server unreachable: let the app show its own error
    }
    if (!status.multiuser || status.logged_in) return true;

    const { overlay, user } = buildLoginOverlay();
    document.body.appendChild(overlay);
    user.focus();
    return false;
  }

  function makeMethod(name) {
    return function (...args) {
      return callApi(name, args);
    };
  }

  const REMOTE_METHODS = [
    'get_version', 'get_latest_version', 'get_artist_images', 'get_ffmpeg_status', 'get_node_status',
    'save_settings', 'load_settings', 'get_registries', 'add_registry', 'remove_registry',
    'get_history', 'get_profiles', 'load_profile_data', 'cache_image', 'get_spotify_home_feed',
    'search_provider', 'search_provider_async', 'remove_history_item',
    'get_network_status', 'save_profile_data', 'delete_profile_data', 'check_qobuz_api',
    'check_tidal_api', 'open_config_folder', 'open_url', 'download_track_lyrics',
    'download_track_cover', 'download_cover', 'download_album_cover', 'download_all_covers',
    'download_all_lyrics', 'get_track_preview', 'fetch_metadata', 'download_tracks',
    'run_health_check', 'scan_local', 'apply_local_tags', 'set_download_dir',
    'get_registry_directories', 'add_registry_directory', 'remove_registry_directory',
    'discover_registries', 'get_dedup_status', 'scan_for_duplicates',
    'get_trusted_keys',
  ];

  // Deliberately NOT here (and not in webapp.py's ALLOWED_METHODS):
  //   add_trusted_key / remove_trusted_key — these write the Ed25519 trust
  //     store that decides which extension registry entries count as signed.
  //     Editing the root of trust must not be reachable from the same channel
  //     an untrusted caller can reach. Use tools/registry_signing_cli.py.
  //   search_code — a development helper that greps an arbitrary path and
  //     returns matching lines; the UI never called it.

  const api = {};
  for (const name of REMOTE_METHODS) {
    api[name] = makeMethod(name);
  }

  // Window-chrome: no-op. The browser tab already has its own chrome.
  api.window_minimize = () => Promise.resolve();
  api.window_restore = () => Promise.resolve();
  api.window_maximize = () => Promise.resolve();

  // choose_folder(): a browser can't open a native dialog that returns a
  // real server-side path, so this opens a tiny built-in folder browser
  // instead and, once the user confirms, calls set_download_dir() — the
  // web-mode equivalent already added to SpotiFLAC/app.py.
  api.choose_folder = function () {
    return openFolderBrowser();
  };

  function openFolderBrowser() {
    return new Promise((resolve) => {
      const overlay = document.createElement('div');
      overlay.style.cssText =
        'position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:99999;' +
        'display:flex;align-items:center;justify-content:center;';
      const box = document.createElement('div');
      box.style.cssText =
        'background:#151515;color:#eee;border-radius:8px;padding:16px;' +
        'width:420px;max-height:70vh;display:flex;flex-direction:column;' +
        'font:13px system-ui,sans-serif;gap:8px;';
      const pathLabel = document.createElement('div');
      pathLabel.style.cssText = 'opacity:.7;word-break:break-all;';
      const list = document.createElement('div');
      list.style.cssText = 'overflow:auto;flex:1;border:1px solid #333;border-radius:6px;';
      const buttons = document.createElement('div');
      buttons.style.cssText = 'display:flex;justify-content:flex-end;gap:8px;margin-top:4px;';
      const cancelBtn = document.createElement('button');
      cancelBtn.textContent = 'Cancel';
      const selectBtn = document.createElement('button');
      selectBtn.textContent = 'Select this folder';
      buttons.append(cancelBtn, selectBtn);
      box.append(pathLabel, list, buttons);
      overlay.appendChild(box);
      document.body.appendChild(overlay);

      let currentPath = null;

      function row(label, onClick) {
        const el = document.createElement('div');
        el.textContent = label;
        el.style.cssText = 'padding:6px 10px;cursor:pointer;';
        el.onmouseenter = () => (el.style.background = '#222');
        el.onmouseleave = () => (el.style.background = '');
        el.onclick = onClick;
        return el;
      }

      async function load(path) {
        const qs = path ? ('?path=' + encodeURIComponent(path)) : '';
        const res = await fetch('/api/browse-folder' + qs);
        const data = await res.json();
        if (data.error) return;
        currentPath = data.path;
        pathLabel.textContent = currentPath;
        list.innerHTML = '';
        if (data.parent) {
          list.appendChild(row('.. (up)', () => load(data.parent)));
        }
        for (const name of data.directories) {
          list.appendChild(row(name, () => load(currentPath + '/' + name)));
        }
      }

      cancelBtn.onclick = () => {
        document.body.removeChild(overlay);
        resolve(null);
      };
      selectBtn.onclick = async () => {
        document.body.removeChild(overlay);
        if (currentPath) {
          await callApi('set_download_dir', [currentPath]);
        }
        resolve(currentPath);
      };

      load(null);
    });
  }

  window.pywebview = window.pywebview || {};
  window.pywebview.api = api;

  // ── WebSocket push channel: mirrors what pywebview's evaluate_js does
  //    for real desktop windows. See SpotiFLAC/app.py's _push().
  //
  // `fn` below names the global function to invoke and travels over the
  // WebSocket message, so it's dispatched only against this explicit
  // allowlist (kept in sync with every `self._push("...")` call site in
  // app.py) rather than an unrestricted `window[fn](...)` — that keeps a
  // compromised/malicious message from invoking arbitrary global functions.
  const ALLOWED_PUSH_FNS = new Set([
    '__set_version_label',
    'app_cover_download_finished',
    'app_download_finished',
    'app_handle_provider_search_error',
    'app_handle_provider_search_results',
    'app_local_apply_error',
    'app_local_apply_finished',
    'app_local_apply_progress',
    'app_local_scan_error',
    'app_local_scan_results',
    'app_log',
    'app_set_metadata',
    'app_set_progress',
    'app_update_download_stats',
    'loadHistoryAndProfiles',
    'showFfmpegWarning',
    'showTracklist',
    'updateFolderLabel',
    'updateHealthResults',
  ]);

  function connectWs() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(proto + '//' + location.host + '/ws');
    ws.onmessage = (evt) => {
      let msg;
      try {
        msg = JSON.parse(evt.data);
      } catch {
        return;
      }
      const fn = msg && msg.fn;
      const args = (msg && msg.args) || [];
      if (fn === '__set_version_label') {
        if (typeof window.__set_version_label === 'function') {
          window.__set_version_label(...args);
        }
        return;
      }
      if (typeof fn === 'string' && ALLOWED_PUSH_FNS.has(fn) && typeof window[fn] === 'function') {
        try {
          window[fn](...args);
        } catch (e) {
          console.error('web-shim: error dispatching', fn, e);
        }
      } else if (fn) {
        console.warn('web-shim: ignoring push for non-allowlisted function', fn);
      }
    };
    ws.onclose = () => {
      // Best-effort reconnect; a page refresh also recovers.
      setTimeout(connectWs, 2000);
    };
  }
  // Gate the WebSocket on being signed in: an unauthenticated /ws is closed
  // with 1008 by the server anyway, and retrying it every 2s behind a login
  // form is just noise in the console.
  document.addEventListener('DOMContentLoaded', async () => {
    const signedIn = await ensureSignedIn();
    if (!signedIn) return;
    connectWs();
    // pywebview normally fires this once its bridge is ready; app.js may
    // listen for it, so we fire it too.
    window.dispatchEvent(new Event('pywebviewready'));
  });

  // Logout, for a UI that wants to offer it.
  api.logout = async () => {
    await fetch('/api/auth/logout', { method: 'POST' });
    window.location.reload();
  };
})();
