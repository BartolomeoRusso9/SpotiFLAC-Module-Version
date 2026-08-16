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

  function makeMethod(name) {
    return function (...args) {
      return callApi(name, args);
    };
  }

  const REMOTE_METHODS = [
    'get_version', 'get_latest_version', 'get_artist_images', 'get_ffmpeg_status',
    'save_settings', 'load_settings', 'get_registries', 'add_registry', 'remove_registry',
    'get_history', 'get_profiles', 'load_profile_data', 'cache_image', 'get_spotify_home_feed',
    'search_provider', 'search_provider_async', 'search_code', 'remove_history_item',
    'get_network_status', 'save_profile_data', 'delete_profile_data', 'check_qobuz_api',
    'check_tidal_api', 'open_config_folder', 'open_url', 'download_track_lyrics',
    'download_track_cover', 'download_cover', 'download_album_cover', 'download_all_covers',
    'download_all_lyrics', 'get_track_preview', 'fetch_metadata', 'download_tracks',
    'run_health_check',
  ];

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
      if (typeof window[fn] === 'function') {
        try {
          window[fn](...args);
        } catch (e) {
          console.error('web-shim: error dispatching', fn, e);
        }
      }
    };
    ws.onclose = () => {
      // Best-effort reconnect; a page refresh also recovers.
      setTimeout(connectWs, 2000);
    };
  }
  connectWs();

  // pywebview normally fires this once its bridge is ready; app.js may
  // listen for it, so we fire it too, once the DOM is ready.
  document.addEventListener('DOMContentLoaded', () => {
    window.dispatchEvent(new Event('pywebviewready'));
  });
})();
