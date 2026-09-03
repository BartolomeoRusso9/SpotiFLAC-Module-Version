let isDirty = false;
let initialSettings = {};

// ── Detect OS and apply system-specific styles ──────────────────────────────
function detectAndApplyOSStyles() {
  // Detect OS from the user agent
  const userAgent = navigator.userAgent.toLowerCase();
  let detectedOS = 'mac'; // Default to macOS (colorful dots)

  if (userAgent.includes('win')) {
    detectedOS = 'windows';
  } else if (userAgent.includes('linux')) {
    detectedOS = 'linux';
  } else if (userAgent.includes('x11')) {
    detectedOS = 'linux';
  }

  // If this is Windows, apply the CSS class for Windows-style buttons
  if (detectedOS === 'windows') {
    document.body.classList.add('windows-style');
  }

  console.log(`[OS Detection] Detected OS: ${detectedOS}`);
}

// Run detection on page load
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', detectAndApplyOSStyles);
} else {
  detectAndApplyOSStyles();
}

function showSkeletonTracks(count = 5) {
  const container = $('track-rows');
  if (!container) return;
  
  // Clear the table and insert skeletons using the SAME grid as real tracks
  container.innerHTML = Array(count).fill(0).map(() => `
    <div class="track-row" style="pointer-events: none; border-bottom: 1px solid var(--border);">
      <div><div class="skeleton" style="width:14px; height:14px; border-radius:2px;"></div></div>
      <div><div class="skeleton" style="width:16px; height:14px;"></div></div>
      <div class="tr-title-cell">
        <div class="skeleton" style="width:44px; height:44px; border-radius:6px; flex-shrink:0;"></div>
        <div style="display:flex; flex-direction:column; gap:6px; width:100%;">
          <div class="skeleton skeleton-text" style="margin:0; width:70%;"></div>
          <div class="skeleton skeleton-text short" style="margin:0; width:40%;"></div>
        </div>
      </div>
      <div><div class="skeleton skeleton-text" style="margin:0 auto; width:50%;"></div></div>
      <div><div class="skeleton skeleton-text" style="margin:0 22px 0 auto; width:40px;"></div></div>
      <div class="tr-actions">
        <div class="skeleton" style="width:30px; height:30px; border-radius:6px;"></div>
        <div class="skeleton" style="width:30px; height:30px; border-radius:6px;"></div>
        <div class="skeleton" style="width:30px; height:30px; border-radius:6px;"></div>
        <div class="skeleton" style="width:30px; height:30px; border-radius:6px;"></div>
      </div>
    </div>
  `).join("");
  
  $('track-table-wrap').classList.remove('hidden');
  
  // Hide the recents
  if ($('recent-wrap')) $('recent-wrap').style.display = 'none'; 
  
  // Hide the table header until the real data arrives
  const header = document.querySelector('.track-table-header');
  if (header) header.style.display = 'none';
}

// Initialize state after loaded settings
function initSettingsTracking() {
    initialSettings = buildConfig();
    isDirty = false;
    updateSaveButtonVisual();
}

function updateSaveButtonVisual() {
    const btn = document.querySelector('.s-actions .act-btn.primary');
    if (!btn) return;
    if (isDirty) {
        btn.style.opacity = "1";
        btn.textContent = "Save Changes (Unsaved)";
        btn.style.borderColor = "var(--red)"; // Visual cue
    } else {
        btn.textContent = "Save Changes";
        btn.style.borderColor = "var(--yellow-d)";
    }
}

function clearSearchUI() {
    const container = $('text-search-results');
    if (container) container.innerHTML = ''; // Clear the message
    $('text-search-container')?.classList.add('hidden'); // Hide the container
}
// ── Helpers ─────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const ts = () => new Date().toLocaleTimeString('en-US');
// ── Global Initialization ──────────────────────────────────────────────────
const toastMgr = new ToastManager();

// ── View switching ───────────────────────────────────────────────────────────
function switchView(name) {
  if (isDirty) {
        if (!confirm("You have unsaved changes. Do you want to leave this page?")) {
            return; // Cancel view change
        }
        isDirty = false; // Force reset when the user chooses to leave
        updateSaveButtonVisual();
    }
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.querySelectorAll('.nav-btn[id^="nav-"]').forEach(b => b.classList.remove('active'));
  $('view-' + name)?.classList.add('active');
  $('nav-' + name)?.classList.add('active');
  const networkBar = $('titlebar-network');
  if (name === 'settings') {
    networkBar?.classList.remove('hidden');
    loadNetworkStatus();
  } else {
    networkBar?.classList.add('hidden');
  }
}

let networkStatus = { ip: '', country_name: 'Italy', country_code: 'IT' };

// Fetches IP/country info for the Settings titlebar. Guarded throughout:
// safe to call even if titlebar-network isn't present in the current
// markup, and never throws on a failed/slow lookup.
async function loadNetworkStatus() {
  try {
    const api = window.pywebview?.api;
    if (!api || typeof api.get_network_status !== 'function') return;
    const status = await api.get_network_status();
    if (status) {
      networkStatus = { ...networkStatus, ...status };
    }
    const ipEl = $('network-ip');
    if (ipEl) ipEl.textContent = networkStatus.ip || '';
    const countryEl = $('network-country');
    if (countryEl) countryEl.textContent = networkStatus.country_name || '';
  } catch (err) {
    console.warn('[NetworkStatus] failed to load:', err);
  }
}

function togglePublicIp() {
  /* removed by design */
}

function switchTab(name, btn) {
  document.querySelectorAll('.stab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tc').forEach(c => c.classList.remove('active'));
  btn.classList.add('active');
  $('tc-' + name).classList.add('active');
  if (name === 'extensions') {
    loadRegistries();
    loadDirectories();
    loadTrustedKeys();
    loadExtensionHealth();
  }
}

// ── Appearance ───────────────────────────────────────────────────────────────
// The theme class goes on BOTH <html> and <body>. The tokens are declared on
// :root and overridden per theme, and `html { background: var(--bg) }` reads
// them from <html> — with the class only on <body>, <html> kept resolving
// --bg to the light default, so the page's outermost background (what shows
// through on overscroll and around the content) stayed light grey in dark
// mode. Keeping both in sync also means the pre-paint script in index.html
// and this function can't disagree about which element carries the state.
function setThemeClass(dark) {
  for (const el of [document.documentElement, document.body]) {
    if (!el) continue;
    el.classList.toggle('dark-theme', dark);
    el.classList.toggle('light-theme', !dark);
  }
}

function applyTheme(mode) {
  if (mode === 'light' || mode === 'dark') {
    setThemeClass(mode === 'dark');
  } else {
    setThemeClass(window.matchMedia('(prefers-color-scheme: dark)').matches);
  }
}

function changeTheme() {
  const val = $('config-theme').value;
  applyTheme(val);
  try {
    localStorage.setItem('spotiflac-theme-mode', val);
    // Mirror the choice into the settings blob as well. saveSettings() only
    // writes it when the user presses Save, so without this the blob kept
    // saying 'auto' while spotiflac-theme-mode said 'dark' — and the blob is
    // what applySettings() reads at boot, which is how a picked dark theme
    // came back light on the next launch.
    const stored = JSON.parse(localStorage.getItem(SETTINGS_STORAGE_KEY) || '{}');
    stored.theme = val;
    localStorage.setItem(SETTINGS_STORAGE_KEY, JSON.stringify(stored));
  } catch (e) {}
  // …and into gui-settings.json, which is the copy that actually survives.
  // Both localStorage writes above live in the web view's per-origin storage,
  // and the desktop window has not historically kept that across launches
  // (see run_gui() in app.py), so on its own the picker never stuck. This is
  // a theme-only merge, so it cannot clobber unsaved edits elsewhere in the
  // Settings form.
  try {
    window.pywebview?.api?.save_theme?.(val);
  } catch (e) {}
}

function syncSystemTheme(e) {
  const val = $('config-theme')?.value || 'auto';
  if (val === 'auto') applyTheme('auto');
}

//: Must match SpotiFLAC_API.ACCENTS in app.py and the --accent-* blocks in
//: styles.css. 'green' is the default and has no block of its own: it is
//: what :root already says, so it is applied by removing the attribute.
const ACCENTS = ['green', 'blue', 'purple', 'pink', 'orange', 'red', 'cyan', 'amber'];

// data-accent goes on <html> *and* <body>, the same pair the theme classes
// use (see setThemeClass) — styles.css keys the accent blocks off both, so
// that the token values are in scope no matter which element a rule resolves
// against.
function applyAccent(accent) {
  const val = ACCENTS.includes(accent) ? accent : 'green';
  for (const el of [document.documentElement, document.body]) {
    if (!el) continue;
    if (val === 'green') el.removeAttribute('data-accent');
    else el.setAttribute('data-accent', val);
  }
}

function changeAccent() {
  const val = $('config-accent')?.value || 'green';
  applyAccent(val);
  // Three copies for the same reason changeTheme() keeps three: the two
  // localStorage writes are per-origin and can vanish, gui-settings.json is
  // the one that survives a restart.
  try {
    localStorage.setItem('spotiflac-accent', val);
    const stored = JSON.parse(localStorage.getItem(SETTINGS_STORAGE_KEY) || '{}');
    stored.accent = val;
    localStorage.setItem(SETTINGS_STORAGE_KEY, JSON.stringify(stored));
  } catch (e) {}
  try {
    window.pywebview?.api?.save_accent?.(val);
  } catch (e) {}
}

function loadAccentFromStorage() {
  const stored = (() => {
    try { return localStorage.getItem('spotiflac-accent'); } catch (e) { return null; }
  })() || 'green';
  if ($('config-accent')) $('config-accent').value = stored;
  applyAccent(stored);
}

function loadThemeFromStorage() {
  const stored = (() => {
    try { return localStorage.getItem('spotiflac-theme-mode'); } catch (e) { return null; }
  })() || 'auto';
  if ($('config-theme')) $('config-theme').value = stored;
  applyTheme(stored);
}

// Applies the slider to the audio element (if one exists yet) and to the
// label beside it. Called live while dragging, so a preview that is playing
// changes volume under your hand instead of on the next clip.
function changePreviewVolume() {
  const el = $('config-preview-volume');
  if (el) previewVolume = Math.max(0, Math.min(100, Number(el.value) || 0));
  const out = $('preview-volume-value');
  if (out) out.textContent = `${previewVolume}%`;
  if (previewAudio) previewAudio.volume = previewVolume / 100;
  try {
    const stored = JSON.parse(localStorage.getItem(SETTINGS_STORAGE_KEY) || '{}');
    stored.preview_volume = previewVolume;
    localStorage.setItem(SETTINGS_STORAGE_KEY, JSON.stringify(stored));
  } catch (e) {}
}

function changeFont() {
  const font = $('config-font').value;
  document.documentElement.style.setProperty('--app-font', font);
  try {
    const stored = JSON.parse(localStorage.getItem(SETTINGS_STORAGE_KEY) || '{}');
    stored.font = font;
    localStorage.setItem(SETTINGS_STORAGE_KEY, JSON.stringify(stored));
  } catch (e) {}
}

function qualityFallbackChain(q) {
  const n = (q || '').toString().toUpperCase();
  // Dolby Atmos is Tidal-exclusive (see core/quality.py's
  // quality_for_provider()): on Tidal it falls back through hi-res to
  // plain lossless like shown here; on every other provider it's treated
  // as HI_RES_LOSSLESS from the start, which is still a fair chain to
  // show since this tooltip isn't provider-specific.
  const chains = {
      'DOLBY_ATMOS': ['DOLBY_ATMOS', 'HI_RES_LOSSLESS', 'LOSSLESS'],
      'HI_RES_LOSSLESS': ['HI_RES_LOSSLESS', 'LOSSLESS'],
      'LOSSLESS': ['LOSSLESS'],
  };
  return chains[n] || [n || 'LOSSLESS'];
}

function applySettings(settings = {}) {
  const cfg = { ...DEFAULT_SETTINGS, ...settings };
  if ($('config-quality')) {
      $('config-quality').value = cfg.quality;
      // show fallback chain as tooltip
      $('config-quality').title = qualityFallbackChain(cfg.quality).join(' → ');
      // update tooltip when user changes selection (use onchange to avoid duplicate listeners)
      $('config-quality').onchange = function() {
          const val = $('config-quality').value;
          $('config-quality').title = qualityFallbackChain(val).join(' → ');
          isDirty = true; updateSaveButtonVisual();
      };
  }
  if ($('config-fallback')) $('config-fallback').checked = cfg.allow_fallback;
  // The dedicated key wins over the blob's copy: it is what the pre-paint
  // script in index.html already acted on, and a stale cfg.theme here would
  // otherwise flip the UI back a moment after load. When the key is absent —
  // the normal state in the desktop window, whose storage does not always
  // survive a restart — cfg.theme (from gui-settings.json, written by
  // save_theme()) is the surviving copy, so seed the key back from it and
  // the *next* launch gets the right colour before the first paint.
  let themeMode = cfg.theme;
  try {
    themeMode = localStorage.getItem('spotiflac-theme-mode') || cfg.theme;
    localStorage.setItem('spotiflac-theme-mode', themeMode);
  } catch (e) {}
  if ($('config-theme')) $('config-theme').value = themeMode;
  // Same dedicated-key-wins-over-blob dance as the theme above, for the
  // same reason: gui-settings.json is what survives, localStorage is what
  // the next launch reads first.
  let accent = cfg.accent || 'green';
  try {
    accent = localStorage.getItem('spotiflac-accent') || accent;
    localStorage.setItem('spotiflac-accent', accent);
  } catch (e) {}
  if ($('config-accent')) $('config-accent').value = accent;
  if ($('config-font')) $('config-font').value = cfg.font;
  previewVolume = Number.isFinite(Number(cfg.preview_volume)) ? Number(cfg.preview_volume) : 100;
  if ($('config-preview-volume')) $('config-preview-volume').value = previewVolume;
  changeFont();
  changeTheme();
  changeAccent();
  changePreviewVolume();
  if ($('config-lyrics')) { $('config-lyrics').checked = cfg.lyrics; onLyricsChange(); }
  if ($('config-apple-wbw')) $('config-apple-wbw').checked = cfg.apple_lyrics_word_by_word !== false;
  if ($('config-enrich')) { $('config-enrich').checked = cfg.enrich_metadata; onEnrichChange(); }
  if ($('config-filename')) $('config-filename').value = cfg.filename_format;
  if ($('config-track-numbers')) { $('config-track-numbers').checked = cfg.use_track_numbers; onTNChange(); }
  if ($('config-album-track-numbers')) $('config-album-track-numbers').checked = cfg.use_album_track_numbers;
  if ($('config-artist-sub')) $('config-artist-sub').checked = cfg.use_artist_subfolders;
  if ($('config-album-sub')) $('config-album-sub').checked = cfg.use_album_subfolders;
  if ($('config-playlist-sub')) $('config-playlist-sub').checked = cfg.create_playlist_subfolders !== false;
  if ($('config-first-artist')) $('config-first-artist').checked = cfg.first_artist_only;
  if ($('config-artist-separator')) $('config-artist-separator').value = cfg.artist_separator || '';
  updateArtistSeparatorState(cfg.first_artist_only);
  if ($('config-transcode')) { $('config-transcode').value = cfg.transcode_to || 'none'; onTranscodeChange(); }
  if ($('config-transcode-bitrate')) $('config-transcode-bitrate').value = cfg.transcode_bitrate || '320k';
  if ($('config-transcode-keep')) $('config-transcode-keep').checked = cfg.transcode_keep_original;
  if ($('config-retries')) $('config-retries').value = cfg.track_max_retries;
  if ($('config-post-action')) { $('config-post-action').value = cfg.post_download_action; onPostChange(); }
  if ($('config-post-cmd')) $('config-post-cmd').value = cfg.post_download_command;
  if ($('config-qobuz-local-api')) $('config-qobuz-local-api').value = cfg.qobuz_local_api_url || '';
  if ($('config-tidal-api')) $('config-tidal-api').value = cfg.tidal_custom_api || '';
  if ($('config-acoustid-key')) $('config-acoustid-key').value = cfg.acoustid_api_key || '';
  if ($('config-loop')) $('config-loop').value = cfg.loop;
  if ($('config-loglevel')) $('config-loglevel').value = cfg.log_level;
  lastAppliedServices = Array.isArray(cfg.services) ? cfg.services : lastAppliedServices;
  applyListState('services-list', cfg.services);
  applyListState('lyrics-list', cfg.lyrics_providers);
  applyListState('enrich-list', cfg.enrich_providers);
  updateAllApiConfigDisplays();
}

function renumberList(el) {
  // The priority number is the row's position, so it has to be rewritten
  // whenever the rows move — after a drag, and after applyListState()
  // reorders them to match the saved settings. It was only ever written at
  // build time, so a saved order showed its rows numbered 8, 6, 3, 1…
  if (!el) return;
  el.querySelectorAll('.sort-item').forEach((item, i) => {
    const num = item.querySelector('.priority-num');
    if (num) num.textContent = i + 1;
  });
}

function applyListState(id, values = []) {
  const el = $(id);
  if (!el) return;
  const items = Array.from(el.querySelectorAll('.sort-item'));
  items.forEach(item => {
    const cb = item.querySelector('input[type="checkbox"]');
    if (cb) cb.checked = values.includes(item.dataset.value);
  });
  if (values.length) {
    values.forEach(value => {
      const item = el.querySelector(`.sort-item[data-value="${value}"]`);
      if (item) el.appendChild(item);
    });
    items.filter(i => !values.includes(i.dataset.value)).forEach(item => el.appendChild(item));
  }
  renumberList(el);
}

async function loadSettingsFromStorage() {
  try {
    let stored = null;
    if (window.pywebview?.api) {
      stored = await window.pywebview.api.load_settings();
    }
    if (!stored || !Object.keys(stored).length) {
      stored = JSON.parse(localStorage.getItem(SETTINGS_STORAGE_KEY) || 'null');
    }
    if (stored) applySettings(stored);
    else { loadThemeFromStorage(); loadAccentFromStorage(); }
  } catch(e) {
    loadThemeFromStorage();
    loadAccentFromStorage();
  }
}

// Track any changes in settings input fields
document.querySelector('.s-body').addEventListener('input', (e) => {
    const current = JSON.stringify(buildConfig());
    const initial = JSON.stringify(initialSettings);
    
    isDirty = (current !== initial);
    updateSaveButtonVisual();
});

function showToast(message, type = 'success') {
  toastMgr[type](message);
}

async function saveSettings() {
  try {
    const cfg = buildConfig();
    cfg.theme  = $('config-theme')?.value  || DEFAULT_SETTINGS.theme;
    cfg.accent = $('config-accent')?.value || DEFAULT_SETTINGS.accent;
    cfg.font   = $('config-font')?.value   || DEFAULT_SETTINGS.font;
    cfg.preview_volume = previewVolume;
    if (window.pywebview?.api) {
      await window.pywebview.api.save_settings(cfg);
    }
    localStorage.setItem(SETTINGS_STORAGE_KEY, JSON.stringify(cfg));
    isDirty = false;
    initialSettings = cfg;
    updateSaveButtonVisual();
    logMessage('Settings saved.', 'ok');
  } catch(e) {
    showToast('Unable to save settings.');
  }
}

function resetSettings() {
  try {
    localStorage.removeItem(SETTINGS_STORAGE_KEY);
    localStorage.removeItem('spotiflac-theme-mode');
    applySettings(DEFAULT_SETTINGS);
    isDirty = false; // Reset state
    initialSettings = buildConfig(); // Update the baseline to default
    updateSaveButtonVisual();
    logMessage('Settings reset to defaults.', 'ok');
  } catch (e) {
    logMessage('Unable to reset settings.', 'error');
  }
}

function openConfigFolder() {
  if (window.pywebview?.api?.open_config_folder) {
    window.pywebview.api.open_config_folder();
  } else {
    logMessage('Open config folder action is unavailable.', 'warn');
  }
}

// ── Settings helpers ─────────────────────────────────────────────────────────
function onTNChange() {
  $('album-track-row').style.display = $('config-track-numbers').checked ? 'flex' : 'none';
}
function onLyricsChange() {
  const on = $('config-lyrics').checked;
  $('lyrics-prov-wrap').style.opacity = on ? '1' : '.4';
  $('lyrics-prov-wrap').style.pointerEvents = on ? '' : 'none';
}
function onEnrichChange() {
  const on = $('config-enrich').checked;
  $('enrich-prov-wrap').style.opacity = on ? '1' : '.4';
  $('enrich-prov-wrap').style.pointerEvents = on ? '' : 'none';
}
function onPostChange() {
  $('post-cmd-row').style.display = $('config-post-action').value === 'command' ? 'flex' : 'none';
}
// Formats that carry no bitrate knob — must match core/transcode.LOSSLESS_FORMATS.
const LOSSLESS_TRANSCODE_FORMATS = ['flac', 'alac', 'wav', 'aiff', 'wavpack', 'tta'];

function onTranscodeChange() {
  const fmt = $('config-transcode') ? $('config-transcode').value : 'none';
  const on = fmt !== 'none';
  document.querySelectorAll('.transcode-opt').forEach(row => {
    row.style.display = on ? 'flex' : 'none';
  });
  // Bitrate is meaningless for a lossless target: the encoder re-encodes the
  // samples untouched, so the row would offer a setting that does nothing.
  const lossy = on && !LOSSLESS_TRANSCODE_FORMATS.includes(fmt);
  document.querySelectorAll('.transcode-lossy').forEach(row => {
    row.style.display = lossy ? 'flex' : 'none';
  });
}

// ── Sortable lists ───────────────────────────────────────────────────────────
function makeSortable(el) {
  let drag = null;
  function onDS(e) { drag = e.currentTarget; setTimeout(() => drag?.classList.add('dragging'), 0); }
  function onDE() {
    drag?.classList.remove('dragging');
    el.querySelectorAll('.sort-item').forEach(i => i.classList.remove('drag-over'));
    drag = null;
    renumberList(el);
  }
  function onDO(e) {
    e.preventDefault();
    if (!drag || drag.parentElement !== el) return;
    const items = [...el.querySelectorAll('.sort-item:not(.dragging)')];
    const after = items.find(i => e.clientY < i.getBoundingClientRect().top + i.getBoundingClientRect().height / 2);
    items.forEach(i => i.classList.remove('drag-over'));
    if (after) { after.classList.add('drag-over'); el.insertBefore(drag, after); }
    else el.appendChild(drag);
  }
  const apply = () => {
    el.querySelectorAll('.sort-item').forEach(item => {
      item.setAttribute('draggable', 'true');
      item.removeEventListener('dragstart', onDS); item.removeEventListener('dragend', onDE);
      item.addEventListener('dragstart', onDS); item.addEventListener('dragend', onDE);
    });
  };
  el.addEventListener('dragover', onDO);
  el.addEventListener('dragleave', e => {
    if (!el.contains(e.relatedTarget)) el.querySelectorAll('.sort-item').forEach(i => i.classList.remove('drag-over'));
  });
  apply(); return apply;
}

// ── Data definitions ─────────────────────────────────────────────────────────
const ALL_SERVICES = [
  { id:'tidal',       label:'Tidal',          badge:'FLAC', on:true,  icon:'T',  iconClass:'tidal', iconFile:'tidal_l.png' },
  { id:'qobuz',       label:'Qobuz',          badge:'FLAC', on:true,  icon:'Q',  iconClass:'qobuz', iconFile:'qbz.png' },
  { id:'deezer',      label:'Deezer',         badge:'FLAC', on:true,  icon:'D',  iconClass:'deezer', iconFile:'dzr.png' },
  { id:'amazon',      label:'Amazon Music',   badge:'FLAC', on:true,  icon:'AM', iconClass:'amazon', iconFile:'amzn.png' },
  { id:'joox',        label:'Joox',           badge:'FLAC', on:false, icon:'JX', iconClass:'joox', iconFile:'joox.svg' },
  { id:'netease',     label:'NetEase',        badge:'FLAC', on:false, icon:'NE', iconClass:'netease', iconFile:'netease.svg' },
  { id:'migu',        label:'Migu',           badge:'FLAC', on:false, icon:'MG', iconClass:'migu', iconFile:'migu.jpeg' },
  { id:'kuwo',        label:'Kuwo',           badge:'FLAC', on:false, icon:'KW', iconClass:'kuwo', iconFile:'kuwo.png' },
  { id:'soundcloud',  label:'SoundCloud',     badge:'MP3',  on:false, icon:'SC', iconClass:'soundcloud', iconFile:'soundcloud.svg' },
  { id:'youtube',     label:'YouTube Music',  badge:'M4A',  on:false, icon:'YT', iconClass:'youtube', iconFile:'youtube.svg' },
  { id:'apple',       label:'Apple Music',    badge:'M4A',  on:false, icon:'AM', iconClass:'apple', iconFile:'am.png' },
  { id:'pandora',     label:'Pandora',        badge:'MP3',  on:false, icon:'P',  iconClass:'pandora', iconFile:'pandora.svg' },
];
const ALL_LYRICS = [
  { id:'apple',      label:'Apple Music',on:true, iconFile:'am.png', iconClass:'apple' },
  { id:'lrclib',     label:'LRCLib',     on:true,  iconFile:'lrclib.png', iconClass:'lrclib' },
  { id:'amazon',     label:'Amazon',     on:false, iconFile:'amzn.png', iconClass:'amazon' },
  { id:'deezer',     label:'Deezer',     on:false, icon:'DZ', iconClass:'deezer' },
  { id:'genius',     label:'Genius',     on:false, icon:'G',  iconClass:'genius' },
  { id:'netease',    label:'NetEase',    on:false, icon:'NE', iconClass:'netease' },
  { id:'qq',         label:'QQ Music',   on:false, icon:'QQ', iconClass:'qq' },
  { id:'youtube',    label:'YouTube',    on:false, icon:'YT', iconClass:'youtube' },
  { id:'kugou',      label:'Kugou',      on:false, icon:'KG', iconClass:'kugou' },
  { id:'musixmatch', label:'Musixmatch', on:false, iconFile:'musixmatch.svg', iconClass:'musixmatch' },
  { id:'spotify',    label:'Spotify',    on:false, iconFile:'spotify.svg', iconClass:'spotify' },
];
const ALL_ENRICH = [
  { id:'deezer',     label:'Deezer',     on:true, iconFile:'dzr.png', iconClass:'deezer' },
  { id:'apple',      label:'Apple Music',on:true, iconFile:'am.png', iconClass:'apple' },
  { id:'qobuz',      label:'Qobuz',      on:true, iconFile:'qbz.png', iconClass:'qobuz' },
  { id:'tidal',      label:'Tidal',      on:true, iconFile:'tidal_l.png', iconClass:'tidal' },
  { id:'soundcloud', label:'SoundCloud', on:false, iconFile:'soundcloud.svg', iconClass:'soundcloud' },
];

const SETTINGS_STORAGE_KEY = 'spotiflac-settings';
const DEFAULT_SETTINGS = {
  theme: 'auto',
  accent: 'green',
  preview_volume: 100,
  font: "'JetBrains Mono', monospace",
  quality: 'LOSSLESS',
  allow_fallback: false,
  lyrics: true,
  enrich_metadata: true,
  filename_format: '{title} - {artist}',
  use_track_numbers: false,
  use_album_track_numbers: false,
  use_artist_subfolders: true,
  use_album_subfolders: true,
  create_playlist_subfolders: true,
  first_artist_only: false,
  artist_separator: '',
  track_max_retries: 0,
  post_download_action: 'none',
  post_download_command: '',
  transcode_to: 'none',
  transcode_bitrate: '320k',
  transcode_keep_original: false,
  qobuz_local_api_url: '',
  tidal_custom_api: '',
  acoustid_api_key: '',
  loop: 0,
  log_level: 'INFO',
  services: ['tidal','qobuz','deezer','amazon','joox','netease','migu','kuwo','apple','soundcloud','youtube','pandora'],
  lyrics_providers: ['apple', 'lrclib'],
  apple_lyrics_word_by_word: true,
  enrich_providers: ['deezer','apple','qobuz','tidal'],
};

// Ensure the version is populated even if pywebview API isn't ready yet
async function fetchVersionWithRetry(retries = 10, delayMs = 200) {
  for (let i = 0; i < retries; i++) {
    try {
      if (window.pywebview?.api && typeof window.pywebview.api.get_version === 'function') {
        const v = await window.pywebview.api.get_version();
        const tb = document.getElementById('tb-version');
        const hero = document.getElementById('hero-version');
        if (tb) tb.innerText = v && v !== 'unknown' ? `v${v}` : 'v...';
        if (hero) hero.innerText = v && v !== 'unknown' ? `v${v}` : 'v...';
        if (v && v !== 'unknown' && v !== '...') {
          await checkLatestVersion(v);
        }
        return;
      }
    } catch (e) {
      /* ignore */
    }
    await new Promise(r => setTimeout(r, delayMs));
  }
}

document.addEventListener('DOMContentLoaded', () => fetchVersionWithRetry(20, 200));

const UPDATE_RELEASE_URL = 'https://github.com/BartolomeoRusso9/SpotiFLAC-Module-Version/releases';

function normalizeVersionString(version) {
  return String(version || '').trim().replace(/^v/i, '');
}

function compareVersionStrings(a, b) {
  const normalize = (value) => String(value || '').split(/[.\-+]/).map(part => {
    const num = Number(part);
    return Number.isNaN(num) ? part : num;
  });
  const partsA = normalize(a);
  const partsB = normalize(b);
  const maxLen = Math.max(partsA.length, partsB.length);

  for (let i = 0; i < maxLen; i++) {
    const partA = partsA[i] !== undefined ? partsA[i] : 0;
    const partB = partsB[i] !== undefined ? partsB[i] : 0;

    if (typeof partA === 'number' && typeof partB === 'number') {
      if (partA !== partB) return partA > partB ? 1 : -1;
      continue;
    }

    const aStr = String(partA);
    const bStr = String(partB);
    if (aStr !== bStr) return aStr > bStr ? 1 : -1;
  }
  return 0;
}

function showUpdateBadge(latestVersion, publishedAt) {
  const tbBadge = document.getElementById('tb-update-badge');
  const heroBadge = document.getElementById('hero-update-badge');
  const title = latestVersion ? `Update available: v${latestVersion}` : 'Update available';
  if (tbBadge) {
    tbBadge.title = publishedAt ? `${title}\nReleased: ${publishedAt}` : title;
    tbBadge.classList.remove('hidden');
  }
  if (heroBadge) {
    heroBadge.title = publishedAt ? `${title}\nReleased: ${publishedAt}` : title;
    heroBadge.classList.remove('hidden');
  }
}

async function openReleasePage() {
  if (window.pywebview?.api?.open_url) {
    window.pywebview.api.open_url(UPDATE_RELEASE_URL);
  } else {
    window.open(UPDATE_RELEASE_URL, '_blank');
  }
}

async function checkLatestVersion(currentVersion) {
  const normalizedCurrent = normalizeVersionString(currentVersion);
  if (!normalizedCurrent || normalizedCurrent === 'unknown' || normalizedCurrent === '...') return;
  if (!window.pywebview?.api || typeof window.pywebview.api.get_latest_version !== 'function') return;

  try {
    const info = await window.pywebview.api.get_latest_version();
    const latestVersion = normalizeVersionString(info?.latest_version);
    if (latestVersion && compareVersionStrings(latestVersion, normalizedCurrent) > 0) {
      showUpdateBadge(latestVersion, info?.published_at || '');
    }
  } catch (error) {
    console.warn('Failed to check for updates:', error);
  }
}

function buildSortItem(item, index) {
  const d = document.createElement('div');
  d.className = `sort-item ${item.on ? '' : 'inactive'}`;
  d.dataset.value = item.id;
  
  const iconHtml = item.iconFile
    ? `<span class="svc-icon ${item.iconClass} icon-image"><img src="assets/icons/${item.iconFile}" alt="${item.label}" onerror="this.onerror=null; this.src='assets/icons/${item.id}.png';"></span>`
    : item.icon ? `<span class="svc-icon ${item.iconClass}">${item.icon}</span>` : '';
  
  // Add the number (index + 1) and the checkbox
  if (item.title) d.title = `Provided by ${item.title}`;
  d.innerHTML = `
    <span class="priority-num">${index + 1}</span>
    <span class="drag-h">⠿</span>
    ${iconHtml}
    <input type="checkbox" ${item.on ? 'checked' : ''} onclick="event.stopPropagation(); toggleItemActive(this)">
    <span class="svc-name">${item.label}</span>
    ${item.badge ? `<span class="svc-badge">${item.badge}</span>` : ''}
  `;
  return d;
}

function toggleItemActive(cb) {
  const item = cb.closest('.sort-item');
  item.classList.toggle('inactive', !cb.checked);
}

function populateList(id, items) {
  const el = $(id); el.innerHTML = '';
  items.forEach((i, idx) => el.appendChild(buildSortItem(i, idx)));
  makeSortable(el);
}
function getChecked(id) {
  return [...$(id).querySelectorAll('.sort-item')]
    .filter(el => el.querySelector('input[type="checkbox"]').checked)
    .map(el => el.dataset.value);
}

populateList('services-list', ALL_SERVICES);
populateList('lyrics-list',   ALL_LYRICS.map(x => ({ ...x, badge: null })));
populateList('enrich-list',   ALL_ENRICH.map(x => ({ ...x, badge: null })));

// ALL_SERVICES above is presentation only — the icon, the badge, the label —
// and it paints instantly so Settings is never empty. What can actually be
// downloaded from is decided by the installed extensions, which only the
// backend knows: refreshInstalledServices() replaces the list with those,
// exactly as the interactive wizard does (both read
// extensions/catalog.installed_download_services).
async function refreshInstalledServices() {
  if (typeof window.pywebview?.api?.get_download_services !== 'function') return;
  let services;
  try {
    const result = await window.pywebview.api.get_download_services();
    services = result?.services;
  } catch (e) {
    console.warn('[services] could not read the installed providers:', e);
    return;
  }
  // Nothing installed, or an older backend: keep the built-in list. An empty
  // picker is indistinguishable from a broken one.
  if (!Array.isArray(services) || !services.length) return;

  const known = new Map(ALL_SERVICES.map(x => [x.id, x]));
  const items = services.map(svc => {
    const preset = known.get(svc.id);
    if (preset) return { ...preset, title: (svc.extensions || []).join(', ') };
    // A provider this build has no artwork or label for — a third-party
    // extension, most likely. It still belongs in the list.
    return {
      id: svc.id,
      label: svc.label || svc.id,
      badge: null,
      on: false,
      icon: (svc.id || '?').slice(0, 2).toUpperCase(),
      iconClass: svc.id,
      title: (svc.extensions || []).join(', '),
    };
  });

  populateList('services-list', items);
  // Re-apply what was saved: populateList rebuilt the rows, so the order and
  // the ticks that applySettings() put there are gone with them.
  applyListState('services-list', lastAppliedServices.filter(id => known.has(id) || services.some(s => s.id === id)));
}

//: What applySettings() last put in the services list, so a refresh that
//: arrives after it can restore the user's order instead of the defaults.
let lastAppliedServices = ALL_SERVICES.filter(s => s.on).map(s => s.id);

// ── HC chips ─────────────────────────────────────────────────────────────────
const API_SOURCES = [
  { id:'apple',      type:'apple',      name:'Apple Music Lyrics', url:'' },
  { id:'lrclib',     type:'lrclib',     name:'LRCLIB',             url:'' },
  { id:'musixmatch', type:'musixmatch', name:'Musixmatch',          url:'' },
  { id:'spotify',    type:'spotify',    name:'Spotify Lyrics',     url:'' },
  { id:'amazon',     type:'amazon',     name:'Amazon Lyrics',      url:'' },
  { id:'deezer',     type:'deezer',     name:'Deezer Lyrics',      url:'' },
  { id:'genius',     type:'genius',     name:'Genius Lyrics',      url:'' },
  { id:'netease',    type:'netease',    name:'NetEase Lyrics',     url:'' },
  { id:'qq',         type:'qq',         name:'QQ Music Lyrics',    url:'' },
  { id:'youtube',    type:'youtube',    name:'YouTube Lyrics',    url:'' },
  { id:'kugou',      type:'kugou',      name:'Kugou Lyrics',      url:'' },
];
let apiStatusState = {
  checkingSources: {},
  statuses: {},
};

function renderStatusIcon(status) {
  if (status === 'online') return '<span class="status-icon-dot online">✓</span>';
  if (status === 'offline') return '<span class="status-icon-dot offline">✗</span>';
  if (status === 'checking') return '<span class="status-icon-dot checking"></span>';
  return '<span class="status-icon-dot idle"></span>';
}
/**
 * Copies the visible application logs to the clipboard.
 */
function copyLogs() {
    const logArea = $('logArea');
    if (!logArea) {
        logMessage('Error: Log area not found.', 'error');
        return;
    }

    // Extract just the plain text (without the HTML tags)
    const logsText = logArea.innerText;

    if (navigator.clipboard) {
        navigator.clipboard.writeText(logsText).then(() => {
            toastMgr.success('Logs copied to clipboard!');
        }).catch(err => {
            toastMgr.error('Error copying logs.');
        });
    } else {
        // Fallback for older browsers
        const textArea = document.createElement("textarea");
        textArea.value = logsText;
        document.body.appendChild(textArea);
        textArea.select();
        document.execCommand("copy");
        document.body.removeChild(textArea);
        toastMgr.info('Logs copied (fallback).');
    }
}

/**
 * Creates the HTML icon markup for a platform or extension type.
 * @param {string} type - The platform or extension identifier.
 * @returns {string} HTML markup containing the corresponding icon.
 */
function renderPlatformIcon(type) {
  if (type === 'extensions') {
    return '<span class="svc-icon icon-glyph extensions">🧩</span>';
  }
  const iconMap = {
    tidal: 'tidal_l.png',
    qobuz: 'qbz.png',
    deezer: 'dzr.png',
    amazon: 'amzn.png',
    apple: 'am.png',
    soundcloud: 'soundcloud.svg',
    pandora: 'pandora.svg',
    youtube: 'youtube.svg',
    musicbrainz: 'musicbrainz_l.png',
    kuwo: 'kuwo.png',
    joox: 'joox.svg',
    netease: 'netease.svg',
    migu: 'migu.jpeg',
    songstats: 'songstats.png',
  };
  const iconFile = iconMap[type];
  if (!iconFile) {
    // No artwork shipped for this one. The provider tables already carry a
    // letter glyph for exactly this case; guessing at `${type}.svg` and then
    // at `${type}.png` just put two 404s and a broken-image icon on screen.
    const known = [...ALL_SERVICES, ...ALL_LYRICS, ...ALL_ENRICH].find(x => x.id === type);
    const glyph = known?.icon || (type || '?').slice(0, 2).toUpperCase();
    return `<span class="svc-icon icon-glyph ${type}">${escHtml(glyph)}</span>`;
  }
  return `<span class="svc-icon icon-image ${type}"><img src="assets/icons/${iconFile}" alt="${type}" onerror="this.onerror=null; this.src='assets/icons/${type}.png';"></span>`;
}

function buildStatusCard(source) {
  const status = apiStatusState.statuses[source.id] || 'idle';
  const checking = apiStatusState.checkingSources[source.id] === true;
  return `<div class="status-card">
    <div class="status-card-header">
      <div class="status-card-left">
        ${renderPlatformIcon(source.id)}
        <div class="status-card-name">${source.name}</div>
      </div>
      ${renderStatusIcon(checking ? 'checking' : status)}
    </div>
  </div>`;
}

/** Renders the lyrics provider status cards. */
function renderStatusGrids() {
  const servicesGrid = $('status-services-grid');
  if (servicesGrid) {
    servicesGrid.innerHTML = API_SOURCES.map((source) => buildStatusCard(source)).join('');
  }
}

/**
 * Updates the health-check summary label.
 * @param {string} text - The summary text to display.
 */
function updateStatusSummary(text) {
  const label = $('hc-summary');
  if (label) label.textContent = text;
}

function updateOverallStatus(okCount, totalCount) {
  const el = $('status-overall');
  if (!el) return;
  const online = okCount > 0;
  el.className = `status-overall ${online ? 'online' : 'offline'}`;
  el.querySelector('.status-overall-icon').textContent = online ? '✓' : '✗';
  el.querySelector('.status-overall-text').textContent = totalCount > 0 ? `${okCount}/${totalCount} providers OK` : 'No checks yet';
}

/**
 * Checks the status of all configured providers and extensions.
 * Updates the health-check interface while the check is running and marks all sources offline if the check fails or no backend is available.
 */
function checkAll() {
  setFetchingState('start', 'checking provider status...');
  const sources = API_SOURCES.map((source) => source.id);
  sources.forEach((sourceId) => {
    apiStatusState.checkingSources[sourceId] = true;
    apiStatusState.statuses[sourceId] = 'checking';
  });
  renderStatusGrids();
  updateStatusSummary('Checking all providers...');
  if (window.pywebview?.api?.run_health_check) {
    window.pywebview.api.run_health_check(sources).catch(() => {
      setFetchingState('hide');
      sources.forEach((sourceId) => {
        apiStatusState.statuses[sourceId] = 'offline';
        apiStatusState.checkingSources[sourceId] = false;
      });
      renderStatusGrids();
      updateStatusSummary('Health check failed.');
      updateOverallStatus(0, sources.length);
    });
  } else {
    setTimeout(() => {
      sources.forEach((sourceId) => {
        apiStatusState.statuses[sourceId] = 'offline';
        apiStatusState.checkingSources[sourceId] = false;
      });
      renderStatusGrids();
      updateStatusSummary('Demo: all providers offline.');
      updateOverallStatus(0, sources.length);
    }, 800);
  }
}

function withTimeout(promise, ms, message) {
  return Promise.race([
    promise,
    new Promise((_, reject) => window.setTimeout(() => reject(new Error(message)), ms)),
  ]);
}

function checkOne(sourceId) {
  apiStatusState.checkingSources[sourceId] = true;
  apiStatusState.statuses[sourceId] = 'checking';
  renderStatusGrids();
  updateStatusSummary(`Checking ${sourceId}...`);
  if (window.pywebview?.api?.run_health_check) {
    window.pywebview.api.run_health_check([sourceId]).catch(() => {
      apiStatusState.statuses[sourceId] = 'offline';
      renderStatusGrids();
      updateStatusSummary(`Check failed for ${sourceId}.`);
    }).finally(() => {
      apiStatusState.checkingSources[sourceId] = false;
      renderStatusGrids();
    });
  } else {
    setTimeout(() => {
      apiStatusState.statuses[sourceId] = 'offline';
      apiStatusState.checkingSources[sourceId] = false;
      renderStatusGrids();
      updateStatusSummary(`Demo: ${sourceId} is offline.`);
    }, 800);
  }
}

/**
 * Updates health-check statuses for lyrics providers.
 * @param {Array<Object>} data - Health-check results containing provider identifiers and success states.
 */
function updateStatusesFromResults(data) {
  const statusMap = {};
  data.forEach((result) => {
    if (!result.provider) return;
    const current = statusMap[result.provider];
    if (result.ok) {
      statusMap[result.provider] = 'online';
    } else if (!current) {
      statusMap[result.provider] = 'offline';
    }
  });
  for (const source of API_SOURCES) {
    if (statusMap[source.id]) {
      apiStatusState.statuses[source.id] = statusMap[source.id];
    }
    apiStatusState.checkingSources[source.id] = false;
  }
  renderStatusGrids();
}

window.updateHealthResults = (results) => {
  setFetchingState('hide');
  const data = typeof results === 'string' ? JSON.parse(results) : results;
  updateStatusesFromResults(data);
  renderHealthResults(data);
};

renderStatusGrids();

// ── State ────────────────────────────────────────────────────────────────────
let currentTracks  = [];
let trackRenderToken = 0;
let currentUrl     = '';
let currentItemType = 'ALBUM'; // ALBUM, TRACK, ARTIST, PLAYLIST
let queue          = [];
let queueStats     = { downloaded:'0.00 MB', speed:'0.00 MB/s' };
let isDownloading  = false;
let queueStartTime = null;
let queueDurationInterval = null;
//: Queue view filters. A hundred-track playlist makes the queue a list you
//: have to search rather than read — most often to find the handful that
//: failed. null status = show everything.
let queueFilterStatus = null;   // null | 'waiting' | 'done' | 'skipped' | 'error'
let queueSearch = '';
let previewAudio = null;
let previewPlayingIndex = -1;
//: 0-100. A 30-second clip at whatever the system volume happens to be is
//: the one sound this app makes, and it was always full blast.
let previewVolume = 100;
// Destroy current audio to release OS media keys
function stopCurrentPreview() {
  if (previewAudio) {
    previewAudio.pause();
    previewAudio.removeAttribute('src'); // Removes the source
    previewAudio.load(); // Forces the browser to release the media session
  }
  if (previewPlayingIndex >= 0) {
    const prevBtns = document.querySelectorAll(`button.ta-preview[data-preview-index="${previewPlayingIndex}"]`);
    prevBtns.forEach(btn => setPreviewButtonState(btn, false));
    previewPlayingIndex = -1;
  }
}

// ── Pagination state ──────────────────────────────────────────────────────────
let currentPage = 1;
const TRACKS_PER_PAGE = 50;

// ── Logging & Python bridge ──────────────────────────────────────────────────
function logMessage(msg, type = '') {
  // A '-quiet' suffix means "colour this line like its base type, but never
  // toast it". It exists for lists the user genuinely wants itemised in the
  // panel — the unmatched rows of a CSV import, say — where itemising the
  // notifications instead would mean hundreds of popups. The panel keeps
  // every line, in its usual colour; a single summary toast is raised by the
  // caller alongside the list.
  const quiet = typeof type === 'string' && type.endsWith('-quiet');
  const base = quiet ? type.slice(0, -'-quiet'.length) : type;

  // Write to the log UI panel
  const area = $('logArea');
  if (area) {
    const line = document.createElement('div');
    line.className = 'log-line';
    line.innerHTML = `<span class="log-ts">${ts()}</span><span class="log-msg ${base}">${escHtml(msg)}</span>`;
    area.appendChild(line);
    area.scrollTop = area.scrollHeight;
  }

  // Also generate a visual Toast based on the event type.
  // 'debug' (and an absent type) stay in the panel above and never toast —
  // that is where startup diagnostics go, so a launch no longer greets the
  // user with a stack of notifications they did not ask for.
  if (quiet || base === 'debug' || !base) return;

  // A toast is a headline, not a transcript. A provider that fails logs its
  // whole Python traceback at error level, and passing that through put a
  // wall of stack frames over half the window — unreadable, and it buried
  // the one line that said what went wrong. The panel above still has all
  // of it, which is what the panel is for.
  const headline = toastHeadline(msg);

  if (base === 'ok') toastMgr.success(headline);
  else if (base === 'error') toastMgr.error(headline);
  else if (base === 'warn') toastMgr.warning(headline);
  else if (base === 'info') toastMgr.info(headline, { duration: 2500 });
}

const TOAST_MAX_CHARS = 160;

function toastHeadline(msg) {
  const text = String(msg ?? '');
  const lines = text.split('\n').map(l => l.trim()).filter(Boolean);
  if (!lines.length) return text;

  // A traceback's useful line is its last one ("SomeError: what happened"),
  // not its first ("Traceback (most recent call last):").
  const isTraceback = /^Traceback \(most recent call last\)/.test(lines[0]);
  let headline = isTraceback ? lines[lines.length - 1] : lines[0];

  if (headline.length > TOAST_MAX_CHARS) {
    headline = headline.slice(0, TOAST_MAX_CHARS - 1).trimEnd() + '…';
  } else if (lines.length === 1) {
    return headline;
  }
  return `${headline} (see Logs for the rest)`;
}

function clearLog() { $('logArea').innerHTML = ''; }

window.app_log = (msg, type = '') => logMessage(msg, type);
// The backend pushes a bare string, so whether the job is still running has
// to be read off the line itself: it ends in "…" while something is in
// flight ("Reading the file…"), or carries a counter ("Matching 812/1875 ·
// 806 found"), and finishes on a full stop ("Ready for download.", "Error.").
// Getting it wrong only spins or stops a small dial, which is why a
// heuristic is worth more here than a second event.
window.app_set_progress = (label) => setStatus(label || '', /…|\d+\/\d+/.test(label || ''));
window.app_set_metadata = (data) => {
  try {
    const d = typeof data === 'string' ? JSON.parse(data) : data;
    setAlbumCard(
      d.title,
      d.artist,
      d.cover,
      d.quality,
      d.description,
      d.followers,
      d.owner,
      d.owner_avatar,
      d.source,
      d.artist_listeners,
      d.artist_rank,
      d.artist_verified,
      d.artist_biography,
      d.release_date,
      d.track_count,
      d.artist_url,
      d.artists_data
    );
  } catch(e) {}
};
window.updateFolderLabel = (path) => {
  $('folder-path').textContent = path; $('folder-path').title = path;
};
    let currentDownloadToastId = null;

    window.app_update_download_stats = (payload) => {
      try {
        const data = typeof payload === 'string' ? JSON.parse(payload) : payload;
        if (!data) return;
        
        queueStats.downloaded = `${Number(data.total_downloaded || 0).toFixed(2)} MB`;
        queueStats.speed = `${Number(data.current_speed || 0).toFixed(2)} MB/s`;

        if (isDownloading) {
          const activeItem = queue.find(q => q.status === 'active');
          if (activeItem) {
            // Plain text, not an HTML fragment: toastMgr's rendering path
            // escapes the message (see toast-system.js escapeHtml), so
            // markup passed here would show up as literal tags instead of
            // being rendered.
            const msg = `${activeItem.title} — ${activeItem.progress}% · Speed: ${queueStats.speed}`;

            if (!currentDownloadToastId) {
              currentDownloadToastId = toastMgr.loading(msg, { title: 'Downloading Tracks...' });
            } else {
              // Update il testo del toast esistente
              const toastEl = document.getElementById(currentDownloadToastId);
              const toastMsgEl = toastEl && toastEl.querySelector('.toast-message');
              if (toastMsgEl) toastMsgEl.textContent = msg;
            }
          }
        }
        if (Array.isArray(data.queue) && data.queue.length) {
          // The queue is indexed once per push instead of being scanned once
          // per stat entry: those scans were quadratic in the queue length,
          // and Python pushes this payload four times a second for the whole
          // queue. First match wins, as findIndex() did here before.
          const byId = new Map(), bySpotifyId = new Map(), byName = new Map();
          queue.forEach(q => {
            if (q.id && !byId.has(q.id)) byId.set(q.id, q);
            if (q.spotify_id && !bySpotifyId.has(q.spotify_id)) bySpotifyId.set(q.spotify_id, q);
            const nameKey = `${q.title}\u0000${q.artist}`;
            if (!byName.has(nameKey)) byName.set(nameKey, q);
          });
          data.queue.forEach(stat => {
            const item = (stat.id ? byId.get(stat.id) : null)
              || (stat.spotify_id ? bySpotifyId.get(stat.spotify_id) : null)
              || (stat.track_name ? byName.get(`${stat.track_name}\u0000${stat.artist_name}`) : null);
            if (!item) return;
            if (stat.status === 'downloading') item.status = 'active';
            else if (stat.status === 'skipped') item.status = 'skipped';
            else if (stat.status === 'completed') item.status = 'done';
            else if (stat.status === 'failed') item.status = 'error';
            if (stat.total_size > 0) {
              item.progress = Math.min(100, Math.round((stat.progress / stat.total_size) * 100));
            } else if (stat.status === 'completed') {
              item.progress = 100;
            }
            if (stat.file_path) item.file_path = stat.file_path;
            if (stat.total_size > 0) item.file_size_mb = (stat.total_size / (1024 * 1024));
          });
        }
        renderQueue();
      } catch (e) {
        console.warn('Failed to parse download stats', e);
      }
    };

window.showTracklist = (tracksJson) => {
  setFetchingState('success');
  const tracks = typeof tracksJson === 'string' ? JSON.parse(tracksJson) : tracksJson;
  renderTracks(tracks, 1);
  $('fetchBtn').disabled = false;
  $('text-search-container')?.classList.add('hidden');
};

window.app_download_finished = (success = true) => {
  const activeItems = queue.map((item, qi) => item.status === 'active' ? qi : -1).filter(i => i >= 0);
  
  // Close out items from the completed batch
  if (activeItems.length > 0) {
    activeItems.forEach(qi => updateQueueItem(qi, success ? 'done' : 'error', success ? 100 : 0));
  }

  if (currentDownloadToastId) {
     toastMgr.dismiss(currentDownloadToastId);
     currentDownloadToastId = null;
  }
  
  // Check if there are still any downloads running concurrently
  const stillActive = queue.some(q => q.status === 'active');
  if (!stillActive) {
    isDownloading = false;
    resetQueueDuration();
    setStatus(success ? 'Download complete! ✓' : 'Error during download.');
    logMessage(success ? 'All downloads finished.' : 'Download failed.', success ? 'ok' : 'error');
  }
  
  // Safety fallback: trigger any newly added tracks that got stuck
  const waiting = queue.filter(q => q.status === 'waiting');
  if (waiting.length > 0) {
    startDownloadQueue();
  }
};
window.loadHistoryAndProfiles = async () => {
  if (!window.pywebview?.api) return;
  try {
    const hist     = await window.pywebview.api.get_history();
    renderRecent(hist);
    const profiles = await window.pywebview.api.get_profiles();
    const sel      = $('profile-select');
    sel.innerHTML  = '<option value="">Select…</option>';
    profiles.forEach(p => {
      const o = document.createElement('option'); o.value = p; o.textContent = p;
      sel.appendChild(o);
    });
    try {
      const v = await window.pywebview.api.get_version();
      const tb = document.getElementById('tb-version');
      const hero = document.getElementById('hero-version');
      if (tb) tb.innerText = v;
      if (hero) hero.innerText = v && v !== 'unknown' ? `v${v}` : 'v...';
    } catch(e) { /* ignore */ }
  } catch(e) { logMessage('Could not load history/profiles: ' + e, 'warn'); }
};

// ── Status bar ────────────────────────────────────────────────────────────────
function setStatus(msg, loading = false) {
  const statusText = $('status-text');
  if (statusText) statusText.textContent = msg || '';
  // The strip only exists while it has something to say; an empty message is
  // how every caller clears it.
  const bar = $('status-bar');
  if (bar) bar.classList.toggle('hidden', !msg);
  const spinner = $('spinner');
  if (spinner) spinner.style.display = loading && msg ? 'block' : 'none';
}
function setTrackRenderStatus(msg, visible = false) {
  const el = $('track-render-status');
  if (!el) return;
  el.textContent = msg;
  el.classList.toggle('hidden', !visible);
}

function setPlaycountHeaderLabel(label) {
  const header = document.querySelector('.track-table-header');
  if (!header) return;
  // header children: [empty, #, Title, Playcount, Duration, Actions]
  if (header.children && header.children[3]) header.children[3].textContent = label;
}

// ── Album card ────────────────────────────────────────────────────────────────
let g_albumReleaseDate = '';
let g_albumTrackCount = 0;

// Samples the cover's average colour into --album-glow on #album-card (see
// styles.css #album-card::before) for a soft artwork-derived tint instead of
// a flat surface. Best-effort: a cover served without CORS headers taints
// the canvas and getImageData() throws — caught silently, the card just
// keeps its plain background. Never touches the <img> itself.
function applyAlbumGlow(imgEl) {
  const cardEl = $('album-card');
  if (!cardEl) return;
  try {
    const SIZE = 24; // downsample hard — this is an average, not a picture
    const canvas = document.createElement('canvas');
    canvas.width = SIZE; canvas.height = SIZE;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(imgEl, 0, 0, SIZE, SIZE);
    const { data } = ctx.getImageData(0, 0, SIZE, SIZE);
    let r = 0, g = 0, b = 0, n = 0;
    for (let i = 0; i < data.length; i += 4) {
      if (data[i + 3] < 16) continue; // skip near-transparent pixels
      r += data[i]; g += data[i + 1]; b += data[i + 2]; n++;
    }
    if (!n) { cardEl.style.removeProperty('--album-glow'); return; }
    r = Math.round(r / n); g = Math.round(g / n); b = Math.round(b / n);
    cardEl.style.setProperty('--album-glow', `rgba(${r}, ${g}, ${b}, .5)`);
  } catch (err) {
    // Cross-origin cover, unsupported canvas, whatever — no glow this time.
    cardEl.style.removeProperty('--album-glow');
  }
}

// Reads the clipboard into the fetch bar and runs the fetch — the paste and
// the press, which are always done together for a link that was just copied
// out of Spotify.
async function pasteAndFetch() {
  let text = '';
  try {
    text = (await navigator.clipboard.readText() || '').trim();
  } catch (e) {
    // Denied or unavailable: say so rather than appearing to do nothing.
    toastMgr.warning('Could not read the clipboard. Paste with ' +
      (navigator.platform.startsWith('Mac') ? '⌘V' : 'Ctrl+V') + ' instead.');
    return;
  }
  if (!text) { toastMgr.info('The clipboard is empty.'); return; }
  const input = $('urlInput');
  input.value = text;
  input.dispatchEvent(new Event('input'));
  // In search mode a pasted string is a query, and onFetch() handles both —
  // it reads the mode itself, so nothing here needs to know which we are in.
  onFetch();
}

// The button is only worth showing where the clipboard can actually be read:
// pywebview's web view and a plain browser tab differ here, and a button that
// always fails is worse than no button. Checked once at boot via the
// Permissions API where it exists; where it doesn't, the button stays and the
// catch above covers the refusal.
async function initPasteButton() {
  const btn = $('pasteBtn');
  if (!btn) return;
  if (!navigator.clipboard || !navigator.clipboard.readText) return; // stays hidden
  try {
    const status = await navigator.permissions?.query?.({ name: 'clipboard-read' });
    if (status && status.state === 'denied') return; // stays hidden
  } catch (e) { /* Permissions API missing or does not know this name — show it */ }
  btn.classList.remove('hidden');
}

// Navigates to an artist's own page the same way clicking a recent-fetch
// card does: drop the URL in the fetch bar and run the normal fetch flow,
// rather than a one-off code path that would skip whatever that flow does
// (recent-card highlight, search-mode reset, etc.) and drift from it later.
function goToUrl(url) {
  const safeUrl = httpUrlOrNull(url);
  if (!safeUrl) return;
  $('urlInput').value = safeUrl;
  if ($('searchMode').value === 'search') toggleSearchMode();
  onFetch();
}

// Kept as its own name because that is what the call sites mean, and because
// an artist link is the one that has to survive being clicked from inside a
// row (see the delegated listener below).
function goToArtist(url) { goToUrl(url); }

// One delegated listener rather than an onclick="" per artist name: the URL
// only ever goes into a data- attribute (escHtml covers both quote chars),
// never into a JS string built by interpolation — the cover-URL comment
// above setAlbumCard's <img> explains why that path is avoided here too.
document.addEventListener('click', (e) => {
  const el = e.target.closest('.artist-link');
  if (!el) return;
  e.stopPropagation();
  goToArtist(el.dataset.artistUrl);
});

// Wraps an artist name as a clickable span when a URL is known, or leaves
// it as plain text otherwise — used everywhere an artist name is set, so a
// track from a provider that never returned an artist_url just shows a
// name, same as before this existed.
function artistNameHtml(name, url) {
  const safeName = escHtml(name || '');
  const safeUrl = httpUrlOrNull(url);
  if (!safeUrl) return safeName;
  return `<span class="artist-link" data-artist-url="${escHtml(safeUrl)}">${safeName}</span>`;
}

// A credit line where *each* artist is its own link. artistsData is the
// backend's per-artist [{id,name,url}] list (see _artist_nodes in
// core/spotify_metadata.py); when it's missing — another provider, an
// older payload — this falls back to the joined string with one link on
// the whole thing, which is what it did before.
//
// The joined string is never split back apart to do this: "Tyler, The
// Creator" is one artist whose name contains the separator, and splitting
// is exactly what turns that into two wrong links.
function artistsCreditHtml(artistsData, joinedNames, fallbackUrl) {
  const list = Array.isArray(artistsData) ? artistsData.filter(a => a && a.name) : [];
  if (!list.length) return artistNameHtml(joinedNames, fallbackUrl);
  return list.map(a => artistNameHtml(a.name, a.url)).join(', ');
}

let g_albumArtistUrl = '';
let g_albumArtistsData = [];

function setAlbumCard(title, artist, coverUrl, quality, description, followers, owner, ownerAvatar, source, artistListeners, artistRank, artistVerified, artistBiography, releaseDate, trackCount, artistUrl, artistsData) {
  g_albumReleaseDate = releaseDate || '';
  g_albumTrackCount = trackCount || 0;
  g_albumArtistUrl = artistUrl || '';
  g_albumArtistsData = Array.isArray(artistsData) ? artistsData : [];
  
  const metaSection = $('track-meta-section');
  if (metaSection) {
    metaSection.innerHTML = '';
    metaSection.style.display = 'none';
  }
  // Cleared now, repopulated once the tracks are in (updateAlbumMeta, or
  // showSingleTrackCard) — otherwise the previous fetch's sheet lingers
  // while the new one loads.
  renderAlbumTech([]);
  $('album-cover').querySelector('.cover-duration')?.remove();
  $('album-subtitle').style.display = '';
  // showSingleTrackCard()'s quality/duration chip row is cleaned up from
  // renderTracks()'s own single-track-vs-not branch, not from here — see
  // the comment there for why (setAlbumCard and renderTracks run off two
  // separate, not-strictly-ordered backend callbacks).

  $('album-actions').innerHTML = `
    <button class="act-btn primary" onclick="downloadAll()">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
      Download All
    </button>
    <button class="act-btn secondary" onclick="downloadSelected()" id="dl-selected-btn" style="display:none;">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><polyline points="9 12 11 14 15 10"/></svg>
      Download Selected
    </button>
    <button class="act-btn secondary" id="save-all-covers-btn" data-tip="Save all covers as .jpg" onclick="downloadAllCovers(this)">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>
    </button>
    <button class="act-btn secondary" id="save-all-lyrics-btn" data-tip="Save all lyrics as .lrc" onclick="downloadAllLyrics(this)">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
    </button>
    
  `;
  $('album-title').innerHTML = escHtml(title || '—') + (artistVerified
    ? ` <span class="artist-verified-badge" title="Verified Artist"><svg width="20" height="20" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="12" fill="#1d9bf0"/><path d="M8 12.5l2.5 2.5 5.5-5.5" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg></span>`
    : '');
  const subtitle = $('album-subtitle');
  
  // For artists, show rank or listeners; for playlists, show quality
  const isArtistCard = !!(artistRank || artistListeners || artistVerified || artistBiography);

  if (isArtistCard) {
    const bio = artistBiography || description || '';
    // textContent, not innerHTML: the bio comes from whichever metadata
    // provider (or third-party extension) answered, and the server-side
    // `re.sub(r"<[^>]+>", "", ...)` in spotify_metadata.py is a tag stripper,
    // not a sanitiser — it doesn't cover every provider and doesn't survive
    // an unterminated tag. Nothing renders differently here: .artist-bio is
    // a -webkit-box line clamp, so markup never contributed anything anyway.
    subtitle.textContent = bio;
    subtitle.className = bio ? 'artist-bio' : '';
    subtitle.style.display = bio ? '' : 'none';
  } else {
    subtitle.textContent = description || quality || '';
    subtitle.className = '';
    subtitle.style.display = (description || quality) ? '' : 'none';
  }

  const ownerEl = $('album-owner');
  const followersEl = $('album-followers');
  const sourceEl = $('album-source');
  const metaDetails = $('album-meta-details');
  const avatarEl = $('album-owner-avatar');
  
  // Create a safe container for artist stats without breaking the original HTML
  let artistStatsRow = $('artist-stats-row');
  if (!artistStatsRow) {
    artistStatsRow = document.createElement('div');
    artistStatsRow.id = 'artist-stats-row';
    artistStatsRow.style.cssText = 'display:flex;align-items:center;gap:8px;';
    metaDetails.insertBefore(artistStatsRow, metaDetails.firstChild);
  }
  
  const ownerRow = $('album-owner-row');

  if (isArtistCard) {
    const parts = [];
    if (artistRank)      parts.push(`#${artistRank} rank`);
    if (followers)       parts.push(`${Number(followers).toLocaleString('en-US')} followers`);
    if (artistListeners) parts.push(`${Number(artistListeners).toLocaleString('en-US')} listeners`);
    
    artistStatsRow.innerHTML = parts.map(p => `<span>${escHtml(p)}</span>`).join('<span class="dot-sep"> · </span>');
    artistStatsRow.style.display = 'flex';
    if (ownerRow) ownerRow.style.display = 'none'; // Hide the original row while keeping it intact
    
    metaDetails.classList.remove('hidden');
    if (avatarEl) avatarEl.classList.add('hidden');
  } else {
    artistStatsRow.style.display = 'none';
    if (ownerRow) ownerRow.style.display = 'flex';
    
    if (ownerEl) ownerEl.textContent = owner || '';
    // Number('') and Number(null) are both 0, not NaN — so an album with no
    // follower count was showing "0 followers", and that non-empty string
    // then kept the whole meta-details row visible.
    const followerCount = (followers === 0 || followers) ? Number(followers) : NaN;
    if (followersEl) followersEl.textContent = Number.isFinite(followerCount) && followerCount > 0 ? `${followerCount.toLocaleString()} followers` : '';
    if (sourceEl) sourceEl.textContent = source || '';
  }

  if (!isArtistCard) {
    const hasMetaDetails = !!(
      (ownerEl && ownerEl.textContent) ||
      (followersEl && followersEl.textContent) ||
      (sourceEl && sourceEl.textContent) ||
      ownerAvatar
    );
    metaDetails.classList.toggle('hidden', !hasMetaDetails);
  }

  // If owner present, prefer showing owner as the album artist (playlist behavior)
  //
  // This is the single place #album-artist is written. It used to be the
  // second: an earlier line set the linked markup and this one overwrote it
  // with plain textContent a few statements later, so the artist name in
  // the single-track card — the one view where this span is what's actually
  // on screen — was never clickable however well the backend resolved it.
  //
  // innerHTML: one clickable span per credited artist when the backend sent
  // the per-artist list, a single link when it only sent one URL, plain
  // escaped text when it sent neither (an artist's own page sends neither —
  // no point linking a page to itself).
  const artistEl = $('album-artist');
  if (owner) {
    artistEl.textContent = "";
  } else {
    artistEl.innerHTML = artistsCreditHtml(artistsData, artist, artistUrl);
  }

  if (ownerAvatar) {
    avatarEl.style.backgroundImage = `url('${encodeURI(ownerAvatar)}')`;
    avatarEl.textContent = '';
    avatarEl.classList.remove('hidden');
  } else if (owner) {
    avatarEl.style.backgroundImage = '';
    avatarEl.textContent = owner.trim().charAt(0).toUpperCase();
    avatarEl.classList.remove('hidden');
  } else {
    avatarEl.style.backgroundImage = '';
    avatarEl.textContent = '';
    avatarEl.classList.add('hidden');
  }

  const descriptionEl = $('album-description');
  if (!isArtistCard && description) {
    descriptionEl.textContent = description;
    descriptionEl.classList.add('visible');
  } else {
    descriptionEl.textContent = '';
    descriptionEl.classList.remove('visible');
  }

  const coverEl = $('album-cover');
  const safeCover = httpUrlOrNull(coverUrl);
  if (safeCover) {
    const displayArtist = artist || title || 'Unknown';
    // Built with DOM calls rather than a template string: coverUrl is remote
    // metadata, and the old markup dropped it unescaped into both a src=""
    // attribute and a JS string literal inside onclick="" — one apostrophe in
    // a cover URL was enough to break out and run as script.
    coverEl.textContent = '';
    $('album-card')?.style.removeProperty('--album-glow'); // don't carry the previous cover's tint while this one loads

    const img = document.createElement('img');
    img.alt = 'cover';
    img.className = 'cover-loading'; // blur-up: styles.css clears it once decode() resolves
    img.src = safeCover;
    img.addEventListener('error', () => {
      coverEl.textContent = '🎵';
      $('album-card')?.style.removeProperty('--album-glow');
    });
    (img.decode ? img.decode().catch(() => {}) : Promise.resolve()).then(() => {
      img.classList.remove('cover-loading');
      applyAlbumGlow(img);
    });
    coverEl.appendChild(img);

    const btn = document.createElement('button');
    btn.id = 'cover-download-btn';
    btn.className = 'cover-download-btn';
    btn.title = 'Download cover';
    btn.style.cssText = 'left: 50%; top: 50%; transform: translate(-50%, -50%);';
    // Static markup, no interpolation.
    btn.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>';
    btn.addEventListener('click', () => {
      downloadAlbumCover(btn, safeCover, title || 'album', displayArtist, owner || '');
    });
    coverEl.appendChild(btn);
  } else {
    coverEl.textContent = '🎵';
    $('album-card')?.style.removeProperty('--album-glow');
  }
  $('album-card').classList.remove('hidden');
  $('text-search-container')?.classList.add('hidden');
}
function updateAlbumMeta(trackCount) {
  const searchMode = $('searchMode')?.value === 'search';
  const url = currentUrl.toLowerCase();
  let badgeType = 'ALBUM';

  if (searchMode) {
    badgeType = 'SEARCH';
  } else if (url.includes('/track/') || url.includes('spotify:track:') || url.includes('watch?v=') || url.includes('youtu.be/')) {
    badgeType = 'TRACK';
  } else if (url.includes('/playlist/')) {
    badgeType = 'PLAYLIST';
  } else if (url.includes('/artist/') || url.includes('/browse/artist')) {
    badgeType = 'ARTIST';
  }

  currentItemType = badgeType;
  $('album-type-badge').textContent = badgeType;

  // Hide "Save all covers" button for albums since they only have one cover
  const saveAllCoversBtn = $('save-all-covers-btn');
  if (saveAllCoversBtn) {
    saveAllCoversBtn.style.display = badgeType === 'ALBUM' ? 'none' : '';
  }

  if (badgeType === 'ARTIST') {
    const albumSet = new Set(
      currentTracks.map(t => t.album || t.album_name || t.release).filter(Boolean)
    );
    const artistStatsRow = $('artist-stats-row');
    if (artistStatsRow) {
      const ac = albumSet.size;
      const albumTrackText = `${ac} album${ac !== 1 ? 's' : ''} · ${trackCount} track${trackCount !== 1 ? 's' : ''}`;
      
      // Append the album/track part to the existing row
      artistStatsRow.innerHTML += `<span class="dot-sep"> · </span><span>${escHtml(albumTrackText)}</span>`;
    }
  }
  
  // For albums, show artist, date, and track count in the subtitle
  if (badgeType === 'ALBUM') {
    const artistEl = $('album-artist');
    const artist = artistEl.textContent?.trim() || '';
    const subtitleEl = $('album-subtitle');

    // Built as HTML, not a joined text string: the artist segment needs to
    // be the same clickable span as everywhere else an artist name shows,
    // the date/track-count segments stay plain text.
    let subtitleParts = [];
    if (artist) subtitleParts.push(artistsCreditHtml(g_albumArtistsData, artist, g_albumArtistUrl));
    if (g_albumReleaseDate) {
      const dateStr = String(g_albumReleaseDate).split('T')[0];
      if (dateStr) subtitleParts.push(escHtml(dateStr));
    }
    if (trackCount > 0) {
      subtitleParts.push(escHtml(`${trackCount} track${trackCount !== 1 ? 's' : ''}`));
    }

    const subtitleHtml = subtitleParts.join(' · ');
    subtitleEl.innerHTML = subtitleHtml;
    subtitleEl.style.display = subtitleHtml ? '' : 'none';
  }

  
  
  const artistEl = $('album-artist');
  const hasArtist = Boolean(artistEl.textContent && artistEl.textContent.trim());
  $('album-meta').classList.toggle('no-artist', !hasArtist);
  artistEl.style.display = hasArtist ? '' : 'none';
  const trackCountEl = $('album-tracks-count');
  if (trackCountEl) {
    trackCountEl.textContent = `${trackCount} track${trackCount !== 1 ? 's' : ''}`;
  }
  // For an album the subtitle already reads "<artist> · <date> · <n> tracks";
  // the standalone artist line right under it was the same word again.
  $('album-meta').style.display = badgeType === 'ALBUM' ? 'none' : '';

  // Technical sheet (right column of the card). Per-track fields are shared
  // across an album, so the first track stands in for the release; ISRC is
  // genuinely per-track and stays out of an album-level sheet.
  if (badgeType === 'ALBUM' || badgeType === 'PLAYLIST') {
    const t0 = currentTracks[0] || {};
    const totalMs = currentTracks.reduce((s, t) => s + (Number(t.duration_ms) || 0), 0);
    const released = g_albumReleaseDate
      ? String(g_albumReleaseDate).split('T')[0]
      : (t0.release_date ? String(t0.release_date).split('T')[0] : '');
    // Ordered by how much the fact is worth here, not by how the metadata
    // happens to arrive: copyright is four lines of legal boilerplate and
    // the least actionable thing in the sheet, and leading with it pushed
    // the release date, the track count and the runtime to the bottom of
    // the column. Those go first now; the ℗ line brings up the rear.
    renderAlbumTech([
      ['Released', withRelativeAge(released)],
      ['Tracks', trackCount > 0 ? String(trackCount) : ''],
      ['Total time', formatLongDuration(totalMs)],
      ['Label', t0.publisher || t0.label],
      ['UPC', t0.upc],
      ['Copyright', t0.copyright],
    ]);
  } else if (badgeType !== 'TRACK') {
    // ARTIST / SEARCH — showSingleTrackCard owns the TRACK case.
    renderAlbumTech([]);
  }

  // Also update the tracks table header label
  setPlaycountHeaderLabel(badgeType === 'PLAYLIST' ? 'Album' : 'Playcount');
}

function showSingleTrackCard(t) {
  
  // Duration overlay sul cover
  const coverEl = $('album-cover');
  coverEl.querySelector('.cover-duration')?.remove();
  const dur = formatDuration(t.duration_ms);
  if (dur && dur !== '—') {
    const badge = document.createElement('span');
    badge.className = 'cover-duration';
    badge.textContent = dur;
    coverEl.appendChild(badge);
  }

  // Explicit badge inline nel titolo
  const titleEl = $('album-title');
  titleEl.innerHTML = escHtml(t.title || t.name || '—');
  if (t.explicit) {
    titleEl.innerHTML = escHtml(t.title || t.name || '—') +
      ' <span class="track-explicit-title">E</span>';
  }

  // setAlbumCard() put the quality string into #album-subtitle, but that
  // sits above the artist name — showing it there too would duplicate the
  // quality chip added below (see track-quality-row below), just in a less
  // useful spot. Hidden here, same as before.
  $('album-subtitle').style.display = 'none';

  // The old below-the-title grid is superseded by the technical sheet in the
  // card's right column — same facts, plus ISRC and label, in the space that
  // was empty anyway.
  const section = $('track-meta-section');
  if (section) { section.innerHTML = ''; section.style.display = 'none'; }
  const playcountRaw = t.plays ?? t.playcount ?? t.playCount ?? t.plays_count;
  const playcountVal = playcountRaw != null && String(playcountRaw).trim() && String(playcountRaw) !== '0'
    ? Number(playcountRaw).toLocaleString('en-US')
    : null;

  // Same ordering rationale as the album sheet in updateAlbumMeta(): the
  // facts you actually read first, with the copyright boilerplate last.
  renderAlbumTech([
    ['Album', t.album || t.album_name || t.release],
    ['Released', withRelativeAge(t.release_date ? String(t.release_date).split('T')[0] : (t.year || ''))],
    ['Plays', playcountVal],
    ['Genre', t.genre],
    ['ISRC', t.isrc],
    ['Label', t.publisher || t.label],
    ['Copyright', t.copyright],
  ]);

  // Bottoni azione specifici per la track
  const previewUrl = t.preview_url || t.previewUrl || '';
  const extUrl     = t.external_url || t.externalUrl || t.link || '';
  const trackId    = t.id || '';
  $('album-actions').innerHTML = `
  <button class="act-btn primary" data-tip="Download" onclick="downloadSingle(0)">
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
    Download
  </button>
  <button class="act-btn secondary ta-preview" data-tip="Play Preview" data-preview-index="0" data-track-id="${trackId}" onclick="playPreview(0)">
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg>
  </button>
  <button class="act-btn secondary ta-lyrics" data-tip="Save Lyrics (.lrc)" data-track-index="0" onclick="downloadLyrics(0)">
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
  </button>
  <button class="act-btn secondary ta-cover" data-tip="Save Cover (.jpg)" data-track-index="0" onclick="downloadCover(0)">
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>
  </button>
  ${extUrl ? `
  <button class="act-btn secondary" data-tip="Open in Spotify" onclick="openExternal(0)">
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
  </button>` : ''}
`;

  // The left column (cover + title + artist + actions) is almost always
  // shorter than the right column's technical sheet, leaving the card's
  // lower-left empty. Quality is real, relevant info that was set into
  // #album-subtitle by setAlbumCard() but never shown here (that element
  // sits above the artist name and stays hidden — see above); duration is
  // otherwise only a small overlay on the cover. A quiet chip row under the
  // actions fills the gap with facts about *this* track, not a re-listing
  // of the technical sheet (that already covers ISRC/label/genre/etc).
  let factsRow = document.getElementById('track-quality-row');
  if (!factsRow) {
    factsRow = document.createElement('div');
    factsRow.id = 'track-quality-row';
    factsRow.className = 'track-quality-row';
    $('album-actions').insertAdjacentElement('afterend', factsRow);
  }
  const quality = $('album-subtitle').textContent?.trim() || '';
  const chips = [];
  if (quality) chips.push(`<span class="track-quality-chip">${escHtml(quality)}</span>`);
  if (dur && dur !== '—') chips.push(`<span class="track-quality-chip">${escHtml(dur)}</span>`);
  factsRow.innerHTML = chips.join('');
  factsRow.classList.toggle('hidden', chips.length === 0);
}

function closeAlbumCard() {
  setFetchingState(false);
  stopCurrentPreview();
  $('album-card').classList.add('hidden');
  $('text-search-container')?.classList.add('hidden');
  $('album-subtitle').style.display = '';
  $('track-controls').classList.add('hidden');
  $('track-table-wrap').classList.add('hidden');
  $('recent-wrap').style.display = '';
  $('dl-selected-btn').style.display = 'none';
  currentTracks  = [];
  queue          = [];
  isDownloading  = false;
  renderQueue();
  setStatus('Ready — paste a link and press Fetch');
  $('fetchBtn').disabled  = false;
  $('urlInput').disabled  = false;
  const metaSection = $('track-meta-section');
  if (metaSection) {
  metaSection.innerHTML = '';
  metaSection.style.display = 'none';
}
  renderAlbumTech([]);
  $('album-cover').querySelector('.cover-duration')?.remove();
  document.getElementById('artist-tabs-section')?.remove();
  loadHistoryAndProfiles();
}

// ── Escape HTML ───────────────────────────────────────────────────────────────
function escHtml(s) {
  return String(s || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// Returns the URL only if it's an ordinary http(s) one, otherwise null.
// Cover/avatar URLs arrive from whichever metadata provider answered, and
// they end up in src attributes and in fetches — neither should ever be
// handed a javascript:, data: or file: URL.
function httpUrlOrNull(u) {
  if (!u) return null;
  try {
    const parsed = new URL(String(u), window.location.href);
    return (parsed.protocol === 'http:' || parsed.protocol === 'https:')
      ? parsed.href
      : null;
  } catch {
    return null;
  }
}

function formatDuration(ms) {
  if (!ms) return '—';
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60); const sec = s % 60;
  return `${m}:${sec.toString().padStart(2, '0')}`;
}

// "1 hr 14 min" / "38 min" — for a whole album's runtime, where mm:ss would
// just be a big number.
function formatLongDuration(ms) {
  const total = Math.round((Number(ms) || 0) / 1000);
  if (!total) return '';
  const h = Math.floor(total / 3600);
  const m = Math.round((total % 3600) / 60);
  return h ? `${h} hr ${m} min` : `${m} min`;
}

// "2026-05-15 · 3 months ago" — the date on its own answers "when", but not
// the question actually being asked of a release date in a downloader ("is
// this new?"), which otherwise needs mental arithmetic against today. Coarse
// on purpose: one unit, and nothing at all under a day ("today"), because a
// release date has no time-of-day to be precise about.
function withRelativeAge(dateStr) {
  const raw = String(dateStr || '').trim();
  if (!raw) return '';
  // A year-only release ("1998") has no month or day to measure from.
  if (!/^\d{4}-\d{2}-\d{2}/.test(raw)) return raw;

  const then = new Date(raw + 'T00:00:00');
  if (Number.isNaN(then.getTime())) return raw;

  const days = Math.floor((Date.now() - then.getTime()) / 86400000);
  if (days < 0) return `${raw} · upcoming`;
  if (days === 0) return `${raw} · today`;

  const units = [
    [365, 'year'],
    [30, 'month'],
    [7, 'week'],
    [1, 'day'],
  ];
  for (const [size, label] of units) {
    const n = Math.floor(days / size);
    if (n >= 1) return `${raw} · ${n} ${label}${n === 1 ? '' : 's'} ago`;
  }
  return raw;
}

// ── Album card: the technical sheet ─────────────────────────────────────────
// Fills the column on the right of #album-card. `rows` is [[key, value], …];
// a row whose value is empty or "—" is dropped, and an empty result hides
// the whole column so artist pages (which have none of this) don't show an
// empty rule. Values are plain text — an ISRC belongs in a box you can
// select from, not behind a link.
function renderAlbumTech(rows) {
  const el = $('album-tech');
  if (!el) return;
  const clean = (rows || []).filter(r => {
    const v = r && r[1] != null ? String(r[1]).trim() : '';
    return v && v !== '—';
  });
  el.innerHTML = clean.map(([k, v]) => {
    const val = String(v);
    return `<div class="tech-row"><div class="tech-k">${escHtml(k)}</div>` +
      `<div class="tech-v" title="${escHtml(val)}">${escHtml(val)}</div></div>`;
  }).join('');
  el.classList.toggle('hidden', clean.length === 0);
}

function injectArtistTabs(tracks) {
  document.getElementById('artist-tabs-section')?.remove();

  // Raggruppa per album
  const albumMap = new Map();
  tracks.forEach((t, idx) => {
    // Keyed on the album's own URL when the backend resolved one: two
    // different albums can share a name (a re-release, a deluxe edition),
    // and grouping those together put one cover on someone else's tracks.
    const name = t.album || t.album_name || t.release || '—';
    const key = t.album_url || name;
    if (!albumMap.has(key)) {
      albumMap.set(key, {
        name,
        url: t.album_url || '',
        cover: t.cover_url || t.cover || t.image || '',
        year: t.release_date ? String(t.release_date).split('T')[0].substring(0, 4) : (t.year || ''),
        indices: []
      });
    }
    albumMap.get(key).indices.push(idx);
  });

  const section = document.createElement('div');
  section.id = 'artist-tabs-section';

  // ── Tab bar ──
  const tabBar = document.createElement('div');
  tabBar.className = 'artist-tabs-bar';
  [
    { id: 'albums',  label: `Albums · ${albumMap.size}` },
    { id: 'tracks',  label: `All Tracks · ${tracks.length}` },
    { id: 'gallery', label: 'Gallery' },
  ].forEach((tab, i) => {
    const btn = document.createElement('button');
    btn.className = 'artist-tab' + (i === 1 ? ' active' : '');
    btn.id = `artist-tab-${tab.id}-btn`;
    btn.textContent = tab.label;
    btn.onclick = () => switchArtistTab(tab.id);
    tabBar.appendChild(btn);
  });
  section.appendChild(tabBar);

  // ── Albums Panel ──
  const albumsPanel = document.createElement('div');
  albumsPanel.id = 'artist-panel-albums';
  albumsPanel.className = 'artist-albums-grid';
  albumsPanel.style.display = 'none';
  albumMap.forEach(album => {
    const card = document.createElement('div');
    card.className = 'artist-album-card';
    const coverHtml = album.cover
      ? `<img src="${escHtml(album.cover)}" alt="cover" loading="lazy" onerror="this.parentElement.innerHTML='🎵'">`
      : '🎵';
    card.innerHTML = `
      <div class="aac-cover">${coverHtml}
        <button class="aac-dl" title="Download this album" aria-label="Download this album">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
        </button>
      </div>
      <div class="aac-body">
        <div class="aac-name" title="${escHtml(album.name)}">${escHtml(album.name)}</div>
        <div class="aac-meta">${album.year ? album.year + ' · ' : ''}${album.indices.length} track${album.indices.length !== 1 ? 's' : ''}</div>
      </div>`;

    // Clicking the card *opens* the album — the same view you would get by
    // pasting its link. It used to queue every track and start downloading
    // immediately, which is a surprising amount to set in motion for one
    // click on a picture, and left no way to simply look at an album.
    // Downloading is still one click, on the button on the cover.
    const openAlbum = () => {
      if (album.url) { goToUrl(album.url); return; }
      // No album URL from this provider: fall back to selecting the album's
      // tracks in the list below, which is at least non-destructive.
      switchArtistTab('tracks');
      selectOnlyTracks(album.indices);
    };
    card.onclick = openAlbum;
    card.querySelector('.aac-dl').onclick = (e) => {
      e.stopPropagation();
      addToQueue(album.indices);
      startDownloadQueue();
      $('queue-drawer').classList.add('open');
    };
    albumsPanel.appendChild(card);
  });
  section.appendChild(albumsPanel);

  // ── Gallery Panel ──
  const galleryPanel = document.createElement('div');
  galleryPanel.id = 'artist-panel-gallery';
  galleryPanel.className = 'artist-gallery-grid';
  galleryPanel.style.display = 'none';
  galleryPanel.innerHTML = `<div class="artist-gallery-empty">⏳ Loading gallery…</div>`;
  section.appendChild(galleryPanel);

  // Insert before track-controls
  const listContainer = document.querySelector('.list-container');
  listContainer.insertBefore(section, $('track-controls'));

  // Load gallery in background
  loadArtistGallery(galleryPanel);
}

async function loadArtistGallery(panel) {
  try {
    // Prova API Python se disponibile
    if (window.pywebview?.api?.get_artist_images) {
      const images = await window.pywebview.api.get_artist_images(currentUrl);
      if (images?.length) {
        panel.innerHTML = images.map(url =>
          `<img class="artist-gallery-img" src="${escHtml(url)}" alt="Artist photo" loading="lazy" onerror="this.remove()">`
        ).join('');
        return;
      }
    }
  } catch(e) {}

  // Fallback: cover degli album come gallery
  const covers = [...new Set(currentTracks.map(t => t.cover_url || t.cover || t.image).filter(Boolean))];
  if (covers.length) {
    panel.innerHTML = covers.map(url =>
      `<img class="artist-gallery-img" src="${escHtml(url)}" alt="Cover" loading="lazy" onerror="this.remove()">`
    ).join('');
  } else {
    panel.innerHTML = `<div class="artist-gallery-empty">🖼 No gallery images available.</div>`;
  }
}

function switchArtistTab(tabName) {
  document.querySelectorAll('.artist-tab').forEach(b => b.classList.remove('active'));
  $(`artist-tab-${tabName}-btn`)?.classList.add('active');

  const albumsPanel   = $('artist-panel-albums');
  const galleryPanel  = $('artist-panel-gallery');
  const trackControls = $('track-controls');
  const trackTable    = $('track-table-wrap');

  if (albumsPanel)  albumsPanel.style.display  = tabName === 'albums'  ? 'grid' : 'none';
  if (galleryPanel) galleryPanel.style.display  = tabName === 'gallery' ? 'grid' : 'none';

  if (tabName === 'tracks') {
    trackControls?.classList.remove('hidden');
    trackTable?.classList.remove('hidden');
  } else {
    trackControls?.classList.add('hidden');
    trackTable?.classList.add('hidden');
  }
}

// ── Track rendering ───────────────────────────────────────────────────────────
function renderTracks(tracks, page = 1) {
  stopCurrentPreview();
  
  // Save original order so it can be restored later
  tracks.forEach((t, idx) => {
    if (t._originalIndex === undefined) {
      t._originalIndex = idx;
    }
  });

  currentTracks = tracks;
  currentPage = page;
  
  // Calculate pagination
  const totalPages = Math.ceil(tracks.length / TRACKS_PER_PAGE);
  const startIdx = (currentPage - 1) * TRACKS_PER_PAGE;
  const endIdx = startIdx + TRACKS_PER_PAGE;
  const pageTrackS = tracks.slice(startIdx, endIdx);
  
  const container = $('track-rows');
  container.innerHTML = '';
  const header = document.querySelector('.track-table-header');
  if (header) header.style.display = '';
  const renderToken = ++trackRenderToken;
  const batchSize = 40;
  let index = 0;
  setTrackRenderStatus(`Rendering 0/${pageTrackS.length} tracks…`, pageTrackS.length > 0);

  // Detect if current view is a playlist so we can show album instead of playcount
  const searchMode = $('searchMode')?.value === 'search';
  const url = (currentUrl || '').toLowerCase();
  const isPlaylist = url && (url.includes('/playlist/') || (url.includes('list=') && !url.includes('olak5uy_')));
  setPlaycountHeaderLabel(isPlaylist ? 'Album' : 'Playcount');

  const renderBatch = () => {
    if (renderToken !== trackRenderToken) return;
    const fragment = document.createDocumentFragment();
    const end = Math.min(index + batchSize, pageTrackS.length);

    for (; index < end; index += 1) {
      const t = pageTrackS[index];
      const globalIndex = startIdx + index; // For compatibility with global indices
      const row = document.createElement('div');
      row.className = 'track-row';
      row.id = `track-row-${globalIndex}`;
      // Same identity addToQueue() uses for a queue item's spotify_id — lets
      // syncTrackRowsWithQueue() find this row even after a re-sort changes
      // which #track-row-N id it currently holds.
      if (t.id || t.external_url) row.dataset.spotifyId = t.id || t.external_url;

      const explicit = t.explicit ? `<span class="explicit-badge">E</span>` : '';
      const coverUrl = t.cover_url || t.cover || t.image || '';
      let thumb;
      if (coverUrl) {
        // AGGIUNTO data-url QUI SOTTO
        thumb = `<div class="tr-thumb" data-url="${escHtml(coverUrl)}" style="background-image:url('${encodeURI(coverUrl)}')">
                   <img src="${escHtml(coverUrl)}" alt="cover" loading="lazy" decoding="async" onerror="this.parentElement.innerHTML='🎵';console.warn('cover load failed', this.src)">
                 </div>`;
      } else {
        thumb = `<div class="tr-thumb">🎵</div>`;
      }
      if (!coverUrl) console.debug('renderTracks: missing cover for', globalIndex, t);

      const dur = formatDuration(t.duration_ms);
      const playcountValue = t.plays ?? t.playcount ?? t.playCount ?? t.plays_count;
      const playcount = playcountValue ? String(playcountValue).replace(/\B(?=(\d{3})+(?!\d))/g, ',') : '—';
      const albumName = t.album || t.album_name || t.release || t.release_name || '';
      const playcountCell = isPlaylist ? escHtml(albumName || '—') : playcount;
      let previewUrl = t.preview_url || '';
      
      // If it is not present, check whether it is a property of the track object
      if (!previewUrl && t.previewUrl) previewUrl = t.previewUrl;
      
      // Lazy Loading: the button is always enabled, but it will fetch the preview on click if necessary
      const previewBtn = `<button class="ta-btn ta-preview" data-preview-index="${globalIndex}" data-track-id="${t.id || ''}" data-tip="Play Preview" onclick="playPreview(${globalIndex})">
             <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg>
           </button>`;

      const extUrl  = t.external_url || t.externalUrl || t.link || t.url || '';
      const linkBtn = extUrl
        ? `<button class="ta-btn ta-link" data-tip="Open in Spotify" onclick="openExternal(${globalIndex})">
             <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
           </button>`
        : `<button class="ta-btn" data-tip="No link" disabled style="opacity:.3">
             <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
           </button>`;

      row.innerHTML = `
        <div class="tr-check"><input type="checkbox" class="track-cb" value="${globalIndex}" checked onchange="onCheckChange()"></div>
        <div class="tr-num">${globalIndex + 1}</div>
        <div class="tr-title-cell">
          ${thumb}
          <div class="tr-info">
            <div class="tr-name">${escHtml(t.title || t.name || '?')} ${explicit}</div>
            <div class="tr-artist">${artistsCreditHtml(t.artists_data, t.artists || t.artist || '', t.artist_url)}</div>
          </div>
        </div>
        <div class="tr-playcount">${playcountCell}</div>
        <div class="tr-dur">${dur}</div>
        <div class="tr-actions">
          <button class="ta-btn dl" data-tip="Download" onclick="downloadSingle(${globalIndex})">
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
          </button>
          <button class="ta-btn ta-lyrics" data-tip="Save Lyrics (.lrc)" onclick="downloadLyrics(${globalIndex})">
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
          </button>
          ${previewBtn}
          <button class="ta-btn ta-cover" data-tip="Save Cover (.jpg)" onclick="downloadCover(${globalIndex})">
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>
          </button>
          ${linkBtn}
        </div>
      `;
      

      
      fragment.appendChild(row);
    }

    container.appendChild(fragment);
    setTrackRenderStatus(`Rendering ${Math.min(index, pageTrackS.length)}/${pageTrackS.length} tracks…`, index < pageTrackS.length);

    if (index < pageTrackS.length) {
      if (window.requestIdleCallback) {
        requestIdleCallback(renderBatch, { timeout: 200 });
      } else {
        requestAnimationFrame(renderBatch);
      }
    } else {
      setTrackRenderStatus('', false);
      updateAlbumMeta(tracks.length);
      // If this is an artist page, inject the album section above the tracks
      const urlLower = (currentUrl || '').toLowerCase();
      const isArtist = urlLower.includes('/artist/') || urlLower.includes('spotify:artist:') || urlLower.includes('/browse/artist');
      document.getElementById('artist-tabs-section')?.remove();
      if (isArtist) injectArtistTabs(tracks);
      const isTrackUrl = urlLower.includes('/track/') || urlLower.includes('spotify:track:') || urlLower.includes('watch?v=') || urlLower.includes('youtu.be/');
      if (isTrackUrl && tracks.length === 1) {
        $('track-controls').classList.add('hidden');
        $('track-table-wrap').classList.add('hidden');
        showSingleTrackCard(tracks[0]);
      } else {
        $('track-controls').classList.remove('hidden');
        $('track-table-wrap').classList.remove('hidden');
        // showSingleTrackCard()'s quality/duration chip row, if the previous
        // fetch on this same card was a single track — removing it here
        // (same render pass that decides "not a single track this time")
        // instead of from setAlbumCard avoids a race: setAlbumCard and
        // renderTracks are driven by two separate backend callbacks
        // (app_set_metadata / showTracklist) that aren't guaranteed to run
        // in a fixed order, so a removal in setAlbumCard could fire *after*
        // showSingleTrackCard had already (re)built the row for the fetch
        // that was actually meant to show it — silently deleting it again.
        document.getElementById('track-quality-row')?.remove();
      }
      $('recent-wrap').style.display = 'none';
      
      // Show/hide pagination
      updatePaginationControls(totalPages);
    }
  };

  renderBatch();

onCheckChange();
}

function updatePaginationControls(totalPages) {
  const pagetionDiv = $('pagetion-controls');
  if (totalPages > 1) {
    pagetionDiv.classList.remove('hidden');
    pagetionDiv.style.display = 'flex';
    
    const pageInfo = $('page-info');
    pageInfo.textContent = `Page ${currentPage} of ${totalPages} (${TRACKS_PER_PAGE} per page)`;
    
    $('page-prev').disabled = currentPage === 1;
    $('page-next').disabled = currentPage === totalPages;
  } else {
    pagetionDiv.classList.add('hidden');
    pagetionDiv.style.display = 'none';
  }
}

function previousPage() {
  if (currentPage > 1) {
    currentPage--;
    renderTracks(currentTracks, currentPage);
    $('track-table-wrap').scrollTop = 0;
  }
}

function nextPage() {
  const totalPages = Math.ceil(currentTracks.length / TRACKS_PER_PAGE);
  if (currentPage < totalPages) {
    currentPage++;
    renderTracks(currentTracks, currentPage);
    $('track-table-wrap').scrollTop = 0;
  }
}

// ── Action button feedback helper ─────────────────────────────────────────────
const _SPIN_SVG  = `<svg class="ta-spin" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><circle cx="12" cy="12" r="9" stroke-opacity=".25"/><path d="M12 3a9 9 0 0 1 9 9"/></svg>`;
const _CHECK_SVG = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`;
const _X_SVG     = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`;

function setTaBtnState(btn, state) {
  if (!btn) return;
  btn.classList.remove('ta-loading', 'ta-state-success', 'ta-state-error');
  if (state === 'loading') {
    btn._savedInner = btn.innerHTML;
    btn.classList.add('ta-loading');
    btn.innerHTML = _SPIN_SVG;
  } else if (state === 'success') {
    btn.classList.add('ta-state-success');
    btn.innerHTML = _CHECK_SVG;
  } else if (state === 'error') {
    btn.classList.add('ta-state-error');
    btn.innerHTML = _X_SVG;
  } else {
    // restore default
    if (btn._savedInner) { btn.innerHTML = btn._savedInner; btn._savedInner = null; }
  }
}

function resetTaBtnAfter(btn, ms) {
  // Cancel any previous timer for this button
  if (btn._resetTimer) {
    clearTimeout(btn._resetTimer);
  }
  // Schedule new timer and store the ID on the button element
  btn._resetTimer = setTimeout(() => {
    setTaBtnState(btn, 'default');
    btn._resetTimer = null;
  }, ms);
}

// ── Track actions ─────────────────────────────────────────────────────────────
function openExternal(i) {
  const t   = currentTracks[i];
  const url = t?.external_url;
  if (!url) { logMessage('No external URL for this track', 'warn'); return; }
  if (window.pywebview?.api) window.pywebview.api.open_url(url);
  else window.open(url, '_blank');
}

function downloadLyrics(i) {
  const t = currentTracks[i];
  if (!t) return;
  
  // Select both the hidden table button and the visible card button
  const btns = document.querySelectorAll(`#track-row-${i} .ta-btn.ta-lyrics, .ta-lyrics[data-track-index="${i}"]`);
  btns.forEach(btn => setTaBtnState(btn, 'loading'));
  logMessage(`Fetching lyrics: ${t.title}…`, 'info');
  
  if (window.pywebview?.api) {
    Promise.resolve(window.pywebview.api.download_track_lyrics(t))
      .then(() => { btns.forEach(btn => { setTaBtnState(btn, 'success'); resetTaBtnAfter(btn, 2200); }); })
      .catch(() => { btns.forEach(btn => { setTaBtnState(btn, 'error'); resetTaBtnAfter(btn, 2200); }); });
  } else {
    logMessage('Python not connected — demo mode', 'warn');
    setTimeout(() => { btns.forEach(btn => { setTaBtnState(btn, 'success'); resetTaBtnAfter(btn, 2200); }); }, 700);
  }
}

function downloadCover(i) {
  const t = currentTracks[i];
  if (!t || !t.id) return;
  
  // Select both the hidden table button and the visible card button
  const btns = document.querySelectorAll(`#track-row-${i} .ta-btn.ta-cover, .ta-cover[data-track-index="${i}"]`);
  
  // If already loading, ignore further clicks
  if (btns[0] && btns[0].classList.contains('ta-loading')) return;

  // Cancel any previously scheduled reset timers before setting loading state
  btns.forEach(btn => {
    if (btn._resetTimer) {
      clearTimeout(btn._resetTimer);
      btn._resetTimer = null;
    }
  });

  btns.forEach(btn => setTaBtnState(btn, 'loading'));
  logMessage(`Fetching cover: ${t.title}…`, 'info');
  
  if (window.pywebview?.api) {
    // Only starts the process. The 'success'/'error' state gets set
    // by the app_cover_download_finished listener below.
    window.pywebview.api.download_track_cover(t).catch((err) => {
        btns.forEach(btn => { setTaBtnState(btn, 'error'); resetTaBtnAfter(btn, 2200); });
        logMessage('Error starting cover download: ' + err, 'error');
    });
  } else {
    logMessage('Python not connected — demo mode', 'warn');
    setTimeout(() => { btns.forEach(btn => { setTaBtnState(btn, 'success'); resetTaBtnAfter(btn, 2200); }); }, 1500);
  }
}

// ── In ascolto per il completamento REALE dal backend ──
window.app_cover_download_finished = function(payload) {
  const trackId = payload.id;
  const success = payload.success;
  
  const idx = currentTracks.findIndex(t => t.id === trackId);
  if (idx === -1) return;
  
  const btns = document.querySelectorAll(`#track-row-${idx} .ta-btn.ta-cover, .ta-cover[data-track-index="${idx}"]`);
  btns.forEach(btn => {
      setTaBtnState(btn, success ? 'success' : 'error');
      resetTaBtnAfter(btn, 2200);
  });
};

function downloadAlbumCover(btn, imageUrl, title = 'album', artist = 'Unknown', owner = '') {
  const itemType = currentItemType || 'ALBUM';
  const displayName = itemType === 'PLAYLIST' ? owner || title : artist;
  
  // Set loading state
  btn.classList.add('loading');
  btn.disabled = true;
  btn.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>';
  
  if (window.pywebview?.api) {
    logMessage(`Downloading cover for: ${displayName}…`, 'info');
    try {
      window.pywebview.api.download_cover({
        "title": title,
        "artist": artist,
        "owner": owner,
        "cover": imageUrl,
        "type": itemType
      });
      
      // Simulate completion after 2.5 seconds
      setTimeout(() => {
        btn.classList.remove('loading');
        btn.classList.add('success');
        btn.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
        
        // Reset after 2 seconds
        setTimeout(() => {
          btn.classList.remove('success');
          btn.disabled = false;
          btn.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>';
        }, 2000);
      }, 2500);
    } catch (e) {
      btn.classList.remove('loading');
      btn.classList.add('error');
      btn.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
      logMessage('Error downloading cover: ' + e, 'error');
      
      // Reset after 3 seconds
      setTimeout(() => {
        btn.classList.remove('error');
        btn.disabled = false;
        btn.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>';
      }, 3000);
    }
  } else {
    logMessage('Download feature not available in demo mode', 'warn');
    btn.classList.remove('loading');
    btn.disabled = false;
  }
}

async function downloadAllCovers(btn) {
  if (!currentTracks.length) { logMessage('No tracks loaded.', 'warn'); return; }
  setTaBtnState(btn, 'loading');
  logMessage(`Saving covers for ${currentTracks.length} tracks…`, 'info');

  if (window.pywebview?.api) {
    try {
      await window.pywebview.api.download_all_covers(currentTracks);
      setTaBtnState(btn, 'success');
      logMessage('All covers saved.', 'ok');
    } catch (e) {
      setTaBtnState(btn, 'error');
      logMessage('Error saving covers: ' + e, 'error');
    } finally {
      resetTaBtnAfter(btn, 2500);
    }
  } else {
    // Demo mode
    let done = 0;
    for (const t of currentTracks) {
      await new Promise(r => setTimeout(r, 40));
      done++;
      logMessage(`Cover ${done}/${currentTracks.length}: ${t.title}`, 'info');
    }
    setTaBtnState(btn, 'success');
    resetTaBtnAfter(btn, 2500);
    logMessage('Demo: all covers saved.', 'ok');
  }
}

async function downloadAllLyrics(btn) {
  if (!currentTracks.length) { logMessage('No tracks loaded.', 'warn'); return; }
  setTaBtnState(btn, 'loading');
  logMessage(`Fetching lyrics for ${currentTracks.length} tracks…`, 'info');

  if (window.pywebview?.api) {
    try {
      await window.pywebview.api.download_all_lyrics(currentTracks);
      setTaBtnState(btn, 'success');
      logMessage('All lyrics saved.', 'ok');
    } catch (e) {
      setTaBtnState(btn, 'error');
      logMessage('Error saving lyrics: ' + e, 'error');
    } finally {
      resetTaBtnAfter(btn, 2500);
    }
  } else {
    // Demo mode
    let done = 0;
    for (const t of currentTracks) {
      await new Promise(r => setTimeout(r, 40));
      done++;
      logMessage(`Lyrics ${done}/${currentTracks.length}: ${t.title}`, 'info');
    }
    setTaBtnState(btn, 'success');
    resetTaBtnAfter(btn, 2500);
    logMessage('Demo: all lyrics saved.', 'ok');
  }
}

function playPreview(i) {
  const t = currentTracks[i];
  let previewUrl = t?.preview_url || t?.previewUrl || t?.preview || t?.preview_uri || t?.previewUri || '';
  
  const buttons = document.querySelectorAll(`button.ta-preview[data-preview-index="${i}"]`);
  const trackId = buttons[0]?.dataset.trackId || t?.id || '';

  if (!t || !trackId) {
    logMessage('Track ID missing', 'warn');
    return;
  }

  if (!previewAudio) {
    previewAudio = document.createElement('audio');
    previewAudio.id = 'preview-player';
    previewAudio.style.display = 'none';
    previewAudio.preload = 'none';
    document.body.appendChild(previewAudio);

    previewAudio.addEventListener('ended', () => {
      stopCurrentPreview();
    });
  }
  // The element is created lazily on the first preview, so the saved volume
  // has to be applied here as well as in applyPreviewVolume() — otherwise the
  // first clip of a session always plays at full volume whatever the setting.
  previewAudio.volume = previewVolume / 100;

  // Toggle pause if already playing this track
  if (previewPlayingIndex === i && !previewAudio.paused) {
    stopCurrentPreview(); 
    return;
  }

  // Stop previous track
  if (previewPlayingIndex !== -1 && previewPlayingIndex !== i) {
    stopCurrentPreview(); 
  }

  // Show spinner while loading on all matched buttons
  buttons.forEach(b => setTaBtnState(b, 'loading'));

  if (!previewUrl) {
    console.log(`Fetching preview for track ${trackId}…`);
    pywebview.api.get_track_preview(trackId).then((url) => {
      if (url) {
        previewUrl = url;
        t.preview_url = url; 
        playPreviewWithUrl(i, previewUrl, buttons, t);
      } else {
        buttons.forEach(b => setTaBtnState(b, 'error'));
        setTimeout(() => buttons.forEach(b => setTaBtnState(b, 'default')), 2200);
        logMessage('No preview available for this track', 'warn');
      }
    }).catch((err) => {
      console.error('Error fetching preview:', err);
      buttons.forEach(b => setTaBtnState(b, 'error'));
      setTimeout(() => buttons.forEach(b => setTaBtnState(b, 'default')), 2200);
      logMessage('Failed to fetch preview', 'error');
    });
  } else {
    playPreviewWithUrl(i, previewUrl, buttons, t);
  }
}

function playPreviewWithUrl(i, previewUrl, buttons, t) {
  previewAudio.src = previewUrl;
  previewAudio.currentTime = 0;
  previewAudio.play().then(() => {
    buttons.forEach(b => {
      b.classList.remove('ta-loading', 'ta-state-success', 'ta-state-error');
      setPreviewButtonState(b, true);
    });
    previewPlayingIndex = i;
    logMessage(`Playing preview: ${t.title}`, 'info');
  }).catch(() => {
    buttons.forEach(b => setTaBtnState(b, 'error'));
    setTimeout(() => buttons.forEach(b => setTaBtnState(b, 'default')), 2200);
    logMessage('Preview playback failed, opening in browser…', 'warn');
    window.open(previewUrl, '_blank');
  });
}

function setPreviewButtonState(button, active) {
  if (!button) return;
  button.classList.toggle('active', active);
  
  // Check whether this is the single-card button (.act-btn) or the table button
  const isCardBtn = button.classList.contains('act-btn');
  const svgSize = isCardBtn ? "13" : "11";

  // Dynamic tooltip handling
  if (isCardBtn) {
    button.title = active ? 'Pause preview' : 'Play Preview';
  } else {
    button.dataset.tip = active ? 'Pause preview' : 'Play Preview';
  }

  // Cambio icona dinamico mantenendo le proporzioni corrette
  button.innerHTML = active
    ? `<svg width="${svgSize}" height="${svgSize}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="5" width="4" height="14"/><rect x="14" y="5" width="4" height="14"/></svg>`
    : `<svg width="${svgSize}" height="${svgSize}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg>`;
  }

// ── Effetto Typewriter per il Placeholder ──
// ── Effetto Typewriter per il Placeholder ──
const placeholderLinks = [
  // Spotify
  "open.spotify.com/track/...",
  "open.spotify.com/album/...",
  "open.spotify.com/playlist/...",
  "open.spotify.com/artist/...",
  
  // Tidal
  "https://listen.tidal.com/track/12345678",
  "https://listen.tidal.com/album/12345678",
  "https://listen.tidal.com/playlist/12345678",
  "https://listen.tidal.com/artist/12345678/discography/albums",
  
  // Apple Music
  "https://music.apple.com/us/song/track-name/12345678",
  "https://music.apple.com/us/album/album-name/12345678",
  "https://music.apple.com/us/playlist/playlist-name/pl.123456",
  "https://music.apple.com/us/artist/artist-name/12345678",
  
  // SoundCloud
  "https://soundcloud.com/artist/track-slug",
  "https://soundcloud.com/artist/sets/set-slug",
  "https://on.soundcloud.com/abcd123",
  
  // YouTube / YT Music
  "https://youtube.com/watch?v=dQw4w9WgXcQ",
  "https://youtu.be/dQw4w9WgXcQ",
  "https://music.youtube.com/playlist?list=OLAK5uy_...",
  "https://youtube.com/playlist?list=PL...",
  
  // Pandora
  "https://pandora.com/artist/artist-name/album-name/song-name/TR:12345",
  "https://pandora.app.link/abcd123"
];

const searchPlaceholderLinks = [
  "Drake", "Taylor Swift", "Latest Hits", "Techno", "Summer Vibes", "Lo-fi"
];

let phIndex = 0;
let phCharIndex = 0;
let phIsDeleting = false;
let phTimeout;

function runTypewriter() {
  const mode = $('searchMode').value;
  const input = $('urlInput');
  
  // Choose the correct array based on the current mode
  const links = (mode === 'search') ? searchPlaceholderLinks : placeholderLinks;
  const currentText = links[phIndex];

  if (phIsDeleting) {
    input.placeholder = currentText.substring(0, phCharIndex - 1);
    phCharIndex--;
  } else {
    input.placeholder = currentText.substring(0, phCharIndex + 1);
    phCharIndex++;
  }

  let typeSpeed = phIsDeleting ? 25 : 60;

  if (!phIsDeleting && phCharIndex === currentText.length) {
    typeSpeed = 2500;
    phIsDeleting = true;
  } else if (phIsDeleting && phCharIndex === 0) {
    phIsDeleting = false;
    // Scegli un indice casuale dall'array corrente
    phIndex = Math.floor(Math.random() * links.length);
    typeSpeed = 400;
  }

  phTimeout = setTimeout(runTypewriter, typeSpeed);
}

// ── Check all ────────────────────────────────────────────────────────────────
function toggleAll(cb) {
  document.querySelectorAll('.track-cb').forEach(c => c.checked = cb.checked);
  onCheckChange();
}
// Ticks exactly the given track indices and unticks the rest — the fallback
// for an album card with no album URL to open (a provider that never sent
// one): you end up with that album's tracks selected in the list, ready for
// "Download Selected", instead of a click that does nothing.
function selectOnlyTracks(indices) {
  const wanted = new Set(indices.map(Number));
  document.querySelectorAll('.track-cb').forEach(cb => {
    cb.checked = wanted.has(Number(cb.value));
  });
  onCheckChange();
  const first = document.getElementById(`track-row-${indices[0]}`);
  first?.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function onCheckChange() {
  const checked = document.querySelectorAll('.track-cb:checked').length;
  const total   = document.querySelectorAll('.track-cb').length;
  const selectBtn = $('dl-selected-btn');

  const checkAllEl = $('check-all');
  if (checkAllEl) {
    checkAllEl.checked = total > 0 && checked === total;
    checkAllEl.indeterminate = checked > 0 && checked < total;
  }

  if (selectBtn) {
    selectBtn.style.display = checked > 0 ? 'flex' : 'none';
  }
}
// 1. Handling Ricerche Recenti nel LocalStorage
function saveRecentSearch(query) {
    if (!query || query.length < 2) return;
    let searches = JSON.parse(localStorage.getItem('recent_searches') || '[]');
    searches = searches.filter(s => s !== query);
    searches.unshift(query);
    if (searches.length > 15) searches.pop();
    localStorage.setItem('recent_searches', JSON.stringify(searches));
}

function renderRecentSearches() {
    const searches = JSON.parse(localStorage.getItem('recent_searches') || '[]');
    const grid = $('recent-grid');
    grid.innerHTML = '';
    // Chips that wrap, not one full-width row per search: a past query is
    // two or three words, and giving each of them a row of its own pushed
    // everything below the fold to list five words. See #recent-grid.searches
    // in styles.css — the same grid is a track-artwork grid for fetches.
    grid.classList.add('searches');
    const label = $('recent-wrap').querySelector('.recent-label');
    if (label) label.textContent = 'RECENT SEARCHES';

    searches.forEach(q => {
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'search-chip';
        chip.title = q;
        // Same stroke icon as the search-mode toggle, not an emoji: it sits
        // in a themed chip and has to take its colour from the theme.
        chip.innerHTML = `<span class="rc-search-icon"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.35-4.35"/></svg></span><span class="search-chip-text">${escHtml(q)}</span>`;
        chip.onclick = () => {
            $('urlInput').value = q;
            $('urlInput').dispatchEvent(new Event('input'));
        };
        grid.appendChild(chip);
    });
}

function toggleSearchMode() {
    clearSearchUI();
    clearTimeout(_searchDebounceTimer);
    _searchDebounceTimer = null;
    
    // Resetta le variabili del typewriter
    phIsDeleting = false;
    phCharIndex = 0;
    phIndex = 0;
    clearTimeout(phTimeout);

    const toggle = $('searchModeToggle');
    const input = $('urlInput');
    const mode = $('searchMode');
    const label = $('searchModeText');
    const fetchBtn = $('fetchBtn');

    if (mode.value === 'link') {
        mode.value = 'search';
        toggle.classList.add('active');
        label.textContent = 'Search';
        toggle.title = 'Switch to Fetch Mode';
        
        fetchBtn.style.display = 'none';
        renderRecentSearches();
        
        input.placeholder = searchPlaceholderLinks[0];
        $('track-table-wrap')?.classList.add('hidden');
        $('track-controls')?.classList.add('hidden');
        $('album-card')?.classList.add('hidden');
    } else {
        mode.value = 'link';
        toggle.classList.remove('active');
        label.textContent = 'Fetch';
        toggle.title = 'Switch to Search Mode';
        
        fetchBtn.style.display = 'inline-flex';
        
        const rl = $('recent-wrap').querySelector('.recent-label');
        if (rl) rl.textContent = 'RECENT FETCHES';
        if (window.pywebview?.api) window.pywebview.api.get_history().then(renderRecent);
        
        input.placeholder = placeholderLinks[0];
    }
    runTypewriter();
}

function updateSearchMode() {
  const mode = $('searchMode').value;
  const input = $('urlInput');
  const toggle = $('searchModeToggle');
  const label = $('searchModeText');
  
  if (mode === 'search') {
    // Text mode: stop the animation and set the fixed text
    clearTimeout(phTimeout);
    input.placeholder = 'Search Spotify with keywords, artist or track name…';
    toggle.classList.add('active');
    label.textContent = 'Search';
    toggle.title = 'Switch to Fetch Mode';
    $('track-table-wrap')?.classList.add('hidden');
    $('track-controls')?.classList.add('hidden');
    $('album-card')?.classList.add('hidden');
  } else {
    // Link mode: reset and restart the animation
    toggle.classList.remove('active');
    label.textContent = 'Fetch';
    toggle.title = 'Switch to Search Mode';
    
    phIsDeleting = false;
    phCharIndex = 0;
    clearTimeout(phTimeout);
    runTypewriter();
  }
}

function renderCodeResults(results) {
  const container = $('track-rows');
  container.innerHTML = '';
  if (!results || results.length === 0) {
    container.innerHTML = `<div class="queue-empty">No matches found.</div>`;
    $('track-controls').classList.add('hidden');
    $('track-table-wrap').classList.remove('hidden');
    return;
  }
  results.forEach((r, idx) => {
    const row = document.createElement('div');
    row.className = 'track-row';
    row.id = `code-row-${idx}`;
    const pathHtml = `<div style="font-family: 'JetBrains Mono', monospace; color: var(--text2); font-size:12px;">${escHtml(r.path)}:${r.line}</div>`;
    const snippet = `<pre style="white-space:pre-wrap;margin:6px 0 0;color:var(--text);font-size:13px;">${escHtml(r.snippet)}</pre>`;
    row.innerHTML = `
      <div style="padding:10px 12px; grid-column: 1 / -1;">
        ${pathHtml}
        ${snippet}
      </div>
    `;
    container.appendChild(row);
  });
  // hide album/track UI and show the code results area
  $('track-controls').classList.add('hidden');
  $('track-table-wrap').classList.remove('hidden');
}

window.app_handle_provider_search_results = function(results) {
  const isSearchMode = $('searchMode')?.value === 'search';
  if (!isSearchMode) { 
    return; 
  }
  if (!isSearchMode) {
    setFetchingState('success');
  } else {
    isFetchingData = false;
    const fetchBtn = $('fetchBtn');
    if (fetchBtn) fetchBtn.disabled = false;
  }

  // ── Build data ────────────────────────────────────────────────────────────
  const allItems = [
    ...(results.tracks   || []).map(i => ({ ...i, _kind: 'track' })),
    ...(results.albums   || []).map(i => ({ ...i, _kind: 'album' })),
    ...(results.artists  || []).map(i => ({ ...i, _kind: 'artist' })),
    ...(results.playlists|| []).map(i => ({ ...i, _kind: 'playlist' })),
  ];
  const counts = {
    all:      allItems.length,
    track:    (results.tracks    || []).length,
    album:    (results.albums    || []).length,
    artist:   (results.artists   || []).length,
    playlist: (results.playlists || []).length,
  };

  // ── State ─────────────────────────────────────────────────────────────────
  let activeTab = 'track';
  let filterVal = '';

  // ── Helpers ───────────────────────────────────────────────────────────────
  function fmtMs(ms) {
    if (!ms) return '';
    const s = Math.round(ms / 1000);
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  }

  function defaultIcon(kind) {
    return kind === 'artist' ? '👤' : kind === 'album' ? '💿' : kind === 'playlist' ? '📋' : '🎵';
  }

  function resolveSearchImage(value) {
    if (!value) return '';
    if (typeof value === 'string') return value;
    if (Array.isArray(value)) {
      for (const entry of value) {
        const resolved = resolveSearchImage(entry);
        if (resolved) return resolved;
      }
      return '';
    }
    if (typeof value === 'object') {
      return value.url || value.src || value.href || '';
    }
    return '';
  }

  function makeItemHTML(item) {
    const url  = item.external_url || item.external_urls || '';
    const img  = resolveSearchImage(item.cover_url || item.cover || item.image || item.images);
    const name = escHtml(item.name || item.title || '');
    const meta = escHtml(item.artists || item.artist || item.owner || '');
    const dur  = item._kind === 'track' ? fmtMs(item.duration_ms) : '';
    const typeLabel = item._kind === 'artist' ? 'Artist' : '';
    const thumbClass = item._kind === 'artist' ? 'search-result-thumbnail artist-thumb' : 'search-result-thumbnail';
    // Debug log per gli artisti
    if (item._kind === 'artist') {
      console.log('[Artist] Name:', name, 'cover_url:', item.cover_url, 'images:', item.images, 'cover:', item.cover, 'image:', item.image, 'full item:', item);
    }
    return `
      <div class="search-result-item" data-url="${escHtml(url)}" data-name="${name}|${meta}">
        <div class="${thumbClass}">
          ${img ? `<img src="${escHtml(img)}" onerror="this.parentElement.innerHTML='${defaultIcon(item._kind)}'">` : defaultIcon(item._kind)}
        </div>
        <div class="search-result-info">
          <div class="search-result-title">${name}</div>
          ${typeLabel ? `<div class="search-result-meta">${typeLabel}</div>` : ''}
          ${meta && typeLabel ? `<div class="search-result-meta">${meta}</div>` : meta ? `<div class="search-result-meta">${meta}</div>` : ''}
        </div>
        ${dur ? `<span class="sr-duration">${dur}</span>` : ''}
      </div>`;
  }

  // ── Render ────────────────────────────────────────────────────────────────
  const container = $('text-search-results');
  container.innerHTML = '';

  const panel = document.createElement('div');
  panel.className = 'sr-panel';

  // Tab bar
  const tabBar = document.createElement('div');
  tabBar.className = 'sr-tab-bar';
  const tabs = [
    { id: 'track',    label: 'Tracks' },
    { id: 'album',    label: 'Albums' },
    { id: 'artist',   label: 'Artists' },
    { id: 'playlist', label: 'Playlists' },
  ];
  tabs.forEach(t => {
    const btn = document.createElement('button');
    btn.className = 'sr-tab' + (t.id === activeTab ? ' active' : '');
    btn.dataset.tab = t.id;
    btn.innerHTML = `${t.label}<span class="sr-tab-badge">${counts[t.id]}</span>`;
    btn.onclick = () => {
      activeTab = t.id;
      tabBar.querySelectorAll('.sr-tab').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      renderList();
    };
    tabBar.appendChild(btn);
  });
  panel.appendChild(tabBar);

  // Filter row
  const filterRow = document.createElement('div');
  filterRow.className = 'sr-filter-row';
  const filterInp = document.createElement('input');
  filterInp.className = 'sr-filter-input';
  filterInp.placeholder = 'Filter results…';
  filterInp.oninput = () => { filterVal = filterInp.value.toLowerCase(); renderList(); };
  filterRow.appendChild(filterInp);
  panel.appendChild(filterRow);

  // Content area
  const contentArea = document.createElement('div');
  contentArea.className = 'sr-tab-content';
  panel.appendChild(contentArea);

  container.appendChild(panel);

  function renderList() {
    let items = activeTab === 'all' ? allItems : allItems.filter(i => i._kind === activeTab);
    if (filterVal) {
      items = items.filter(i => {
        const name = (i.name || i.title || '').toLowerCase();
        const meta = (i.artists || i.artist || i.owner || '').toLowerCase();
        return name.includes(filterVal) || meta.includes(filterVal);
      });
    }
    if (!items.length) {
      contentArea.innerHTML = `<div class="search-result-empty">No results found.</div>`;
      return;
    }
    contentArea.innerHTML = items.slice(0, 100).map(makeItemHTML).join('');
    // Attach click → auto-switch to fetch mode + load
    contentArea.querySelectorAll('.search-result-item').forEach(el => {
      el.onclick = () => onSearchResultClick(el.dataset.url);
    });
  }

  renderList();

  $('text-search-container').classList.remove('hidden');
  $('track-table-wrap').classList.add('hidden');
};

// Click on a search result: switch to link mode, populate URL, fetch
function onSearchResultClick(url) {
  if (!url) return;
  // Switch to link mode if still in search mode
  const hiddenMode = $('searchMode');
  if (hiddenMode && hiddenMode.value === 'search') {
    hiddenMode.value = 'link';
    updateSearchMode();
  }
  $('urlInput').value = url;
  onFetch();
}

window.app_handle_provider_search_error = function(message) {
  clearSearchUI();
  setFetchingState('error');
  $('track-rows').innerHTML = `<div class="queue-empty">Provider search failed.</div>`;
  $('track-controls').classList.add('hidden');
  $('track-table-wrap').classList.remove('hidden');
  setStatus('Provider search error.', false);
  logMessage(`Provider search error: ${message}`, 'error');
  $('urlInput').disabled = false;
  $('fetchBtn').disabled = false;
};

function filterTracks() {
  const q = $('trackSearch').value.toLowerCase();
  document.querySelectorAll('.track-row').forEach(row => {
    const title  = row.querySelector('.tr-name')?.textContent?.toLowerCase()   || '';
    const artist = row.querySelector('.tr-artist')?.textContent?.toLowerCase() || '';
    row.style.display = (!q || title.includes(q) || artist.includes(q)) ? '' : 'none';
  });
}
function reverseTracks() {
  const c = $('track-rows'); const rows = [...c.children];
  rows.reverse().forEach(r => c.appendChild(r));
}
function sortTracks() {
  const val = $('sort-select').value;
  const sorted = [...currentTracks]; // Always works on a copy
  
  // Restore the array using the hidden index saved previously
  if (val === 'default') { 
    sorted.sort((a, b) => a._originalIndex - b._originalIndex);
    renderTracks(sorted, 1); 
    return; 
  }
  
  const pc = t => parseInt(t.plays ?? t.playcount ?? t.playCount ?? t.plays_count ?? '0', 10) || 0;
  if (val === 'title_asc')     sorted.sort((a, b) => (a.title || '').localeCompare(b.title || ''));
  if (val === 'title_desc')    sorted.sort((a, b) => (b.title || '').localeCompare(a.title || ''));
  if (val === 'artist_asc')    sorted.sort((a, b) => (a.artist || a.artists || '').localeCompare(b.artist || b.artists || ''));
  if (val === 'artist_desc')   sorted.sort((a, b) => (b.artist || b.artists || '').localeCompare(a.artist || a.artists || ''));
  if (val === 'duration_asc')  sorted.sort((a, b) => (a.duration_ms || 0) - (b.duration_ms || 0));
  if (val === 'duration_desc') sorted.sort((a, b) => (b.duration_ms || 0) - (a.duration_ms || 0));
  if (val === 'plays_asc')     sorted.sort((a, b) => pc(a) - pc(b));
  if (val === 'plays_desc')    sorted.sort((a, b) => pc(b) - pc(a));
  renderTracks(sorted, 1);
}

// ── Recent fetches ────────────────────────────────────────────────────────────
function detectUrlType(url) {
  if (!url) return '';
  const u = url.toLowerCase();
  if (u.includes('spotify:track:') || u.includes('/track/') || u.includes('watch?v=') || u.includes('youtu.be/')) return 'track';
  if (u.includes('spotify:album:') || u.includes('/album/') || (u.includes('playlist') && u.includes('olak5uy_'))) return 'album';
  if (u.includes('spotify:playlist:') || u.includes('/playlist/') || (u.includes('list=') && !u.includes('olak5uy_'))) return 'playlist';
  if (u.includes('spotify:artist:') || u.includes('/artist/') || u.includes('/browse/artist')) return 'artist';
  return '';
}

// Convert legacy Spotify URIs (spotify:track:ID) and similar short URIs into web links
function normalizeHistoryUrl(url) {
  if (!url) return '';
  const u = String(url).trim();
  try {
    if (u.startsWith('spotify:')) {
      const parts = u.split(':');
      if (parts.length >= 3) {
        const type = parts[1];
        const id = parts.slice(2).join(':');
        return `https://open.spotify.com/${type}/${id}`;
      }
    }
    // If it already looks like an http(s) link, return as-is
    if (u.startsWith('http://') || u.startsWith('https://')) return u;
    // Support bare open.spotify.com/... without protocol. Match the host
    // exactly (end of string, or followed by '/', ':' or '?') so a hostile
    // value like "open.spotify.com.evil.com" isn't mistaken for the real
    // domain by a plain prefix check.
    const bareHostMatch = /^(open|play)\.spotify\.com(?:[/:?]|$)/.exec(u);
    if (bareHostMatch) return `https://${u}`;
    return u;
  } catch (e) {
    return url;
  }
}

//: The three screens that can open on nothing say so the same way.
const EMPTY_ICONS = {
  disc: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2.5"/>',
  user: '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
  chart: '<line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/>',
};

function emptyState(icon, title, hint) {
  return `<div class="empty-state">
    <div class="es-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${EMPTY_ICONS[icon] || EMPTY_ICONS.disc}</svg></div>
    <div class="es-title">${escHtml(title)}</div>
    <div class="es-hint">${escHtml(hint)}</div>
  </div>`;
}

function renderRecent(hist) {
  const grid = $('recent-grid'); grid.innerHTML = '';
  // Artwork tiles, not the one-line rows renderRecentSearches() builds.
  grid.classList.remove('searches');
  if (!hist || !hist.length) {
    grid.innerHTML = `<div style="grid-column:1/-1;">${emptyState(
      'disc',
      'No recent fetches yet',
      'Paste a Spotify track, album or playlist link above — what you fetch shows up here.',
    )}</div>`;
    return;
  }
  const BADGE_CFG = {
    playlist: { label:'Playlist', icon:'☰' },
    artist:   { label:'Artist',  icon:'♪' },
    album:    { label:'Album',   icon:'◎' },
    track:    { label:'Track',   icon:'♩' },
  };
  hist.slice(0, 16).forEach(item => {
    const card = document.createElement('div');
    card.className = 'recent-card';
  const rawUrl = item.url || '';
  const link = normalizeHistoryUrl(rawUrl);
  card.onclick = () => {
    if (!link) return;
    $('urlInput').value = link;
    highlightRecentCard(link);
    onFetch();
  };
 
    const coverUrl = item.cover || item.cover_url || item.image || '';
    const coverBg  = coverUrl ? `background-image:url('${encodeURI(coverUrl)}');` : '';
 
    const urlType = item.url_type || detectUrlType(item.url || '');
    const badge   = BADGE_CFG[urlType] || null;
 
    // Subtitle: artist name for tracks, count for everything else
    let subtitle = '';
    if (urlType === 'track') {
      subtitle = escHtml(item.artist || '');
    } else if (item.track_count > 0) {
      subtitle = `${item.track_count} tracks`;
    } else if (item.album_count > 0) {
      subtitle = `${item.album_count} albums`;
    }
 
    const badgeHtml = badge
      ? `<span class="rc-badge ${urlType}">${badge.icon} ${badge.label}</span>`
      : '';
    const subHtml = subtitle ? `<div class="rc-sub">${subtitle}</div>` : '';
 
    card.innerHTML = `
      <div class="rc-cover" style="${coverBg}">${coverUrl ? '' : '🎵'}
        <button class="rc-remove" title="Remove from history"
          onclick="event.stopPropagation();removeRecent(this.closest('.recent-card').dataset.url)">✕</button>
      </div>
      <div class="rc-info">
        <div class="rc-title">${escHtml(item.label || item.title || item.url || '—')}</div>
        ${subHtml}
        ${badgeHtml}
      </div>
    `;
    card.dataset.url = link || item.url || '';
    grid.appendChild(card);
  });
}

function highlightRecentCard(url) {
  document.querySelectorAll('.recent-card').forEach(card => {
    card.classList.toggle('active', card.dataset.url === url);
  });
}

async function removeRecent(url) {
  if (!url || !window.pywebview?.api) return;
  try {
    await window.pywebview.api.remove_history_item(url);
    const hist = await window.pywebview.api.get_history();
    renderRecent(hist);
  } catch (e) {
    logMessage('Could not remove history item: ' + e, 'error');
  }
}

// ── Download queue ────────────────────────────────────────────────────────────
function addToQueue(indices) {
  console.log('addToQueue called', { indices, currentTracksLength: currentTracks.length, queueLengthBefore: queue.length });
  let added = false;
  const skipped = [];
  indices.forEach(i => {
      const t = currentTracks[i];
      if (!t) {
        console.warn('Skipped invalid track index', i);
        return;
      }

      // Usa l'indice originale per evitare che Python scarichi la track sbagliata
      const realIndex = t._originalIndex !== undefined ? t._originalIndex : i;

      const itemId = t.id || t.external_url || `queue-${realIndex}-${Math.random().toString(16).slice(2)}`;
      const spotifyId = t.id || t.external_url || itemId;

      // Deduped on the track's own identity, never on its position: a
      // second fetch puts a *different* track at index 0, and matching on
      // the index there silently refused to download it.
      const duplicate = spotifyId
        ? queue.find(q => q.spotify_id === spotifyId)
        : queue.find(q => q.index === realIndex && q.title === t.title);
      if (duplicate) {
        console.warn('Track already in queue', spotifyId || realIndex);
        skipped.push(duplicate);
        return;
      }
      queue.push({
        id: itemId,
        spotify_id: spotifyId,
        index: realIndex,
        title: t.title,
        artist: t.artist || t.artists || '',
        album: t.album || '',
        status: 'waiting',
        progress: 0,
        file_path: '',
        file_size_mb: 0,
      });
    added = true;
  });
  console.log('queue state after add', { queueLengthAfter: queue.length, queue });
  renderQueue();
  const emptyMsg = $('queue-empty');
  if (emptyMsg) emptyMsg.style.display = queue.length > 0 ? 'none' : 'flex';

  // Nothing was added and something was recognised: say so. Refusing in
  // silence is what made a re-queued track look like a dead button.
  if (!added && skipped.length) {
    const one = skipped[0];
    const done = skipped.every(q => q.status === 'done');
    toastMgr.info(
      skipped.length === 1
        ? `${one.title} is already ${done ? 'downloaded' : 'in the queue'}.`
        : `Those ${skipped.length} tracks are already ${done ? 'downloaded' : 'in the queue'}.`,
    );
  }
  return added;
}

function updateQueueDuration() {
  const durationEl = $('qd-duration');
  if (!durationEl) return;
  
  if (!queueStartTime) {
    durationEl.textContent = '0s';
    return;
  }
  
  // Calculate elapsed time in seconds
  const seconds = Math.floor((Date.now() - queueStartTime) / 1000);
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  
  durationEl.textContent = m > 0 ? `${m}m ${s}s` : `${seconds}s`;
}

function resetQueueDuration() {
  queueStartTime = null;
  if (queueDurationInterval) {
    clearInterval(queueDurationInterval);
    queueDurationInterval = null;
  }
  updateQueueDuration();
}

// Mirrors each queue item's status/progress onto its row in the (still
// visible) track table — a queue-drawer-only view meant scrolling away to
// check on a download in progress. Matched by data-spotifyId first (stable
// across a re-sort, which reshuffles which #track-row-N id a track holds);
// falls back to the original pre-sort index for tracks with neither an id
// nor an external_url (the same fallback addToQueue() already accepts for
// its own dedup lookup, so this isn't a new class of mismatch).
function syncTrackRowsWithQueue() {
  const rows = document.getElementById('track-rows');
  if (!rows || !rows.children.length) return;
  // The rows are indexed once per call instead of running a document query
  // per queue item: with a few hundred tracks queued those N queries were
  // the bulk of a render, and a render happens four times a second while a
  // download is running.
  const byId = new Map();
  for (const row of rows.children) {
    const sid = row.dataset && row.dataset.spotifyId;
    if (sid) byId.set(sid, row);
  }
  queue.forEach(item => {
    let row = (item.spotify_id && byId.get(item.spotify_id)) || null;
    if (!row && item.index != null) {
      row = document.getElementById(`track-row-${item.index}`);
    }
    if (!row) return;
    row.classList.toggle('dl-active', item.status === 'active');
    row.classList.toggle('done', item.status === 'done');
    row.classList.toggle('failed', item.status === 'error');
    row.classList.toggle('skipped', item.status === 'skipped');
    const pct = item.status === 'active' ? item.progress : (item.status === 'done' ? 100 : 0);
    // Writing the custom property unconditionally invalidates the row's
    // style on every render even when the number has not moved.
    if (row._rowProgress !== pct) {
      row._rowProgress = pct;
      row.style.setProperty('--row-progress', pct + '%');
    }
  });
}

// Rendering the queue used to mean `list.innerHTML = ''` followed by one
// innerHTML parse per item, on every single state change. "Download All"
// fired one full rebuild per track (startDownloadQueue marks each waiting
// item active one at a time) and Python pushes download stats four times a
// second on top of that, so a long tracklist locked the window solid.
// Item nodes are now built once, reused, and only the fields that actually
// changed are written; renders are coalesced into at most one per frame.
const queueNodes = new Map();
let queueRenderScheduled = false;

function renderQueue() {
  if (queueRenderScheduled) return;
  queueRenderScheduled = true;
  const run = () => {
    if (!queueRenderScheduled) return;
    queueRenderScheduled = false;
    renderQueueNow();
  };
  // The frame callback is the fast path, but it never fires while the window
  // is hidden or minimised — which would leave the queue frozen mid-download
  // until it came back — so a timer races it and whichever arrives first
  // renders.
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(run);
  setTimeout(run, 120);
}

function queueItemNode(item) {
  let el = queueNodes.get(item.id);
  if (el) return el;
  el = document.createElement('div');
  el.className = 'queue-item';
  el.innerHTML = `
      <div class="qi-top">
        <div class="qi-meta">
          <div class="qi-title"></div>
          <div class="qi-artist"></div>
        </div>
        <div class="qi-pill"></div>
      </div>
      <div class="qi-bottom"></div>
      <div class="qi-bottom-meta"><span class="qi-bm-size"></span><span class="qi-bm-path"></span></div>
    `;
  el._parts = {
    title:  el.querySelector('.qi-title'),
    artist: el.querySelector('.qi-artist'),
    pill:   el.querySelector('.qi-pill'),
    bottom: el.querySelector('.qi-bottom'),
    meta:   el.querySelector('.qi-bottom-meta'),
    size:   el.querySelector('.qi-bm-size'),
    path:   el.querySelector('.qi-bm-path'),
  };
  queueNodes.set(item.id, el);
  return el;
}

function paintQueueItem(el, item, qi) {
  const id = `qi-${qi}`;
  if (el.id !== id) el.id = id;

  const artistAlbum = item.album
    ? `${item.artist || ''} • ${item.album}`
    : (item.artist || '');
  // Nothing below depends on anything outside this signature, so an item
  // whose signature is unchanged needs no DOM write at all.
  const sig = [item.status, item.progress, item.title, artistAlbum,
               item.file_path, item.file_size_mb].join('\u0000');
  if (el._sig === sig) return;
  el._sig = sig;

  const p = el._parts;
  const statusLabel = { waiting:'Queued', active:'Downloading', done:'completed', error:'Failed', skipped:'Skipped' }[item.status] || 'Queued';
  const statusText = item.status === 'active'
    ? `Downloading… ${item.progress}%`
    : item.status === 'done'
    ? 'Completed'
    : item.status === 'error'
    ? 'Failed'
    : item.status === 'skipped'
    ? 'Skipped'
    : 'Queued';

  p.title.textContent  = item.title || '';
  p.artist.textContent = artistAlbum;
  p.pill.className = `qi-pill ${item.status}`;
  p.pill.textContent = statusLabel;

  if (item.status === 'done') {
    const hasSize = item.file_size_mb > 0;
    const hasPath = !!item.file_path;
    p.size.textContent = hasSize ? `${item.file_size_mb.toFixed(2)} MB` : '';
    p.size.style.display = hasSize ? '' : 'none';
    p.path.textContent = hasPath ? item.file_path : '';
    p.path.title = hasPath ? item.file_path : '';
    p.path.style.display = hasPath ? '' : 'none';
    p.meta.style.display = (hasSize || hasPath) ? '' : 'none';
    p.bottom.style.display = 'none';
  } else {
    p.meta.style.display = 'none';
    p.bottom.style.display = '';
    p.bottom.textContent = statusText;
  }
}

function renderQueueNow() {
  const list = $('queue-list');
  if (!list) return;
  let empty = $('queue-empty');
  if (!empty) {
    empty = document.createElement('div');
    empty.id = 'queue-empty';
    empty.innerHTML = `
      <div class="q-icon">📥</div>
      <div>No items in queue.<br>Add tracks to start downloading.</div>
    `;
  }

  const queuedCount = queue.filter(q => q.status === 'waiting').length;
  const completedCount = queue.filter(q => q.status === 'done').length;
  const skippedCount = queue.filter(q => q.status === 'skipped').length;
  const failedCount = queue.filter(q => q.status === 'error').length;

  $('q-queued').textContent = queuedCount;
  $('q-completed').textContent = completedCount;
  $('q-skipped').textContent = skippedCount;
  $('q-failed').textContent = failedCount;

  // Which counter is currently acting as the filter, and whether the "Clear
  // filter" escape hatch is needed at all.
  const filterMap = { waiting: 'queued', done: 'completed', skipped: 'skipped', error: 'failed' };
  document.querySelectorAll('.queue-summary .qs-item').forEach(el => {
    const on = !!queueFilterStatus && el.classList.contains(filterMap[queueFilterStatus]);
    el.classList.toggle('is-filtering', on);
    el.setAttribute('aria-pressed', String(on));
  });
  $('queue-clear-filters')?.classList.toggle('hidden', !queueFilterStatus && !queueSearch);

  const dock = $('queue-dock');
  if (queue.length === 0) {
    queueStats = { downloaded:'0.00 MB', speed:'0.00 MB/s' };
    if (dock) dock.classList.remove('visible');
    $('queue-drawer')?.classList.remove('open');

    empty.style.display = 'flex';
    if (list.firstChild !== empty) list.insertBefore(empty, list.firstChild);
    while (list.children.length > 1) list.removeChild(list.lastChild);
    queueNodes.clear();
    $('q-count').textContent = '0 tracks'; $('q-done').textContent = '';
    const downloaded = $('qd-downloaded');
    const speed = $('qd-speed');
    if (downloaded) downloaded.textContent = queueStats.downloaded;
    if (speed) speed.textContent = 'Idle';
    const bar = $('qd-bar-fill');
    if (bar) bar.style.width = '0%';
    dock?.classList.remove('done');
    resetQueueDuration();
    // Queue just went empty (cleared, or its last item removed) — nothing
    // left for syncTrackRowsWithQueue() to match, so any row still carrying
    // dl-active/done/failed/skipped from before would otherwise be stuck.
    document.querySelectorAll('#track-rows .track-row').forEach(row => {
      row.classList.remove('dl-active', 'done', 'failed', 'skipped');
      row.style.removeProperty('--row-progress');
    });
    return;
  }

  empty.style.display = 'none';
  if (dock) dock.classList.add('visible');

  // Filtering happens here, not over the rendered nodes: the counts above
  // must keep describing the whole queue (that is what makes them useful as
  // filter buttons), and re-rendering is what this function does anyway.
  // 'active' counts as queued for filtering — a download in flight is one
  // you are waiting on, and having it vanish from "Queued" the moment it
  // starts is not what anyone means by the word.
  const q = (queueSearch || '').toLowerCase();
  const visible = [];
  queue.forEach((item, qi) => {
    if (queueFilterStatus) {
      const matchesStatus = queueFilterStatus === 'waiting'
        ? (item.status === 'waiting' || item.status === 'active')
        : item.status === queueFilterStatus;
      if (!matchesStatus) return;
    }
    if (q && !`${item.title || ''} ${item.artist || ''} ${item.album || ''}`.toLowerCase().includes(q)) return;
    visible.push([item, qi]);
  });

  // Reconcile in place: existing nodes are moved at most once and dropped
  // only when they leave the list, so a filter keystroke or a progress tick
  // no longer rebuilds every row from scratch.
  let cursor = 0;
  const place = (node) => {
    if (list.children[cursor] !== node) list.insertBefore(node, list.children[cursor] || null);
    cursor++;
  };

  if (!visible.length) {
    let none = list.querySelector('.queue-no-match');
    if (!none) {
      none = document.createElement('div');
      none.className = 'queue-no-match';
    }
    none.textContent = queueFilterStatus || q
      ? 'Nothing in the queue matches this filter.'
      : 'Queue is empty.';
    place(none);
  }

  visible.forEach(([item, qi]) => {
    const el = queueItemNode(item);
    paintQueueItem(el, item, qi);
    place(el);
  });

  while (list.children.length > cursor) list.removeChild(list.lastChild);

  // Drop the cached nodes of items that are no longer in the queue (removed
  // or cleared), otherwise the map would grow for the life of the session.
  if (queueNodes.size > queue.length) {
    const live = new Set(queue.map(item => item.id));
    for (const key of [...queueNodes.keys()]) if (!live.has(key)) queueNodes.delete(key);
  }
  
  $('q-count').textContent = `${queue.length} track${queue.length !== 1 ? 's' : ''}`;
  const done = queue.filter(q => q.status === 'done').length;
  $('q-done').textContent = done > 0 ? `${done} done` : '';
  
  // Sync the new Stats Bar inside the drawer
  const qsbDownloaded = $('qsb-downloaded');
  const qsbSpeed = $('qsb-speed');
  if (qsbDownloaded) qsbDownloaded.textContent = queueStats.downloaded;
  if (qsbSpeed) qsbSpeed.textContent = queueStats.speed;

  // Sync the Dock indicators
  const downloaded = $('qd-downloaded');
  const speed = $('qd-speed');
  if (downloaded) downloaded.textContent = queueStats.downloaded;
  if (speed) {
    // A live rate only exists while a provider is reporting one. Printing
    // "0.00 MB/s" the rest of the time made a working download look stalled;
    // what is actually known then is how far through the queue we are.
    const rate = parseFloat(queueStats.speed);
    const active = queue.some(q => q.status === 'active');
    speed.textContent = rate > 0
      ? queueStats.speed
      : active
        ? `Downloading ${done + 1} of ${queue.length}…`
        : `${done} of ${queue.length} done`;
  }
  const bar = $('qd-bar-fill');
  if (bar) bar.style.width = `${queue.length ? Math.round((done / queue.length) * 100) : 0}%`;
  dock?.classList.toggle('done', queue.length > 0 && done === queue.length);

  updateQueueDuration();
  syncTrackRowsWithQueue();
}

function toggleQueueDrawer() {
  const drawer = $('queue-drawer');
  if (!drawer) return;
  drawer.classList.toggle('open');
}

// The four counters double as filters: the number and the way to see what it
// counts are the same control, which is one fewer thing on screen than a
// count plus a separate status dropdown. Clicking the active one clears it.
function filterQueue(status) {
  queueFilterStatus = (queueFilterStatus === status) ? null : status;
  renderQueue();
}

function onQueueSearch(value) {
  queueSearch = value || '';
  renderQueue();
}

function clearQueueFilters() {
  queueFilterStatus = null;
  queueSearch = '';
  const input = $('queue-search');
  if (input) input.value = '';
  renderQueue();
}

function updateQueueItem(qi, status, progress) {
  if (qi < 0 || qi >= queue.length) return;
  queue[qi].status = status; queue[qi].progress = progress;
  renderQueue();
}

function clearQueue() {
  queue = []; isDownloading = false;
  queueStats = { downloaded:'0.00 MB', speed:'0.00 MB/s' };
  resetQueueDuration();
  renderQueue();
  setStatus('Queue cleared.');
}

function exportFailures() {
  const failures = queue.filter(q => q.status === 'error');
  
  if (!failures.length) {
    showToast('No failed tracks to export.');
    logMessage('Export aborted: No failed tracks found.', 'info');
    return;
  }
  
  // Construct the text content for the file
  let text = 'SpotiFLAC Failed Downloads Export\n';
  text += 'Date: ' + new Date().toLocaleString() + '\n';
  text += 'Total Failures: ' + failures.length + '\n';
  text += '-'.repeat(40) + '\n\n';
  
  failures.forEach(f => {
    text += `Title:  ${f.title || 'Unknown'}\n`;
    text += `Artist: ${f.artist || 'Unknown'}\n`;
    text += `ID/URL: ${f.spotify_id || f.id || 'Unknown'}\n`;
    text += '-'.repeat(40) + '\n';
  });
  
  // Create and trigger the download blob
  const blob = new Blob([text], { type: 'text/plain' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  
  a.href = url;
  a.download = `spotiflac_failures_${Date.now()}.txt`;
  document.body.appendChild(a);
  a.click();
  
  // Cleanup
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
  
  showToast(`${failures.length} failures exported.`);
  logMessage(`Exported ${failures.length} failed tracks to text file.`, 'ok');
}

// ── Download functions (actually work!) ───────────────────────────────────────
function downloadSingle(i) {
  addToQueue([i]);
  startDownloadQueue();
}

function downloadAll() {
  const all = currentTracks.map((_, i) => i);
  console.log('[downloadAll] Total tracks:', all.length);
  if (!all.length) { setStatus('No tracks loaded.'); return; }
  addToQueue(all);
  console.log('[downloadAll] Queue length after addToQueue:', queue.length);
  startDownloadQueue();
  $('queue-drawer').classList.add('open');
}

function downloadSelected() {
  const sel = [...document.querySelectorAll('.track-cb:checked')].map(cb => parseInt(cb.value));
  console.log('[downloadSelected] Selected tracks:', sel.length);
  if (!sel.length) { setStatus('No tracks selected.'); return; }
  addToQueue(sel);
  console.log('[downloadSelected] Queue length after addToQueue:', queue.length);
  startDownloadQueue();
  $('queue-drawer').classList.add('open');
}

// Execute downloads immediately without waiting for previous batches
async function startDownloadQueue() {
  console.log('[startDownloadQueue] Starting... Queue status:', queue.map(q => q.status));
  
  const waiting = queue.filter(q => q.status === 'waiting');
  console.log('[startDownloadQueue] Waiting items:', waiting.length);
  
  if (!waiting.length) {
    console.warn('[startDownloadQueue] No waiting items, returning');
    return false;
  }

  console.log('[startDownloadQueue] Proceeding with download for:', waiting.length, 'items');



  // Force downloading state but do NOT block concurrent executions
  isDownloading = true;

  // Start duration timer if it's the first active batch
  if (!queueStartTime) {
    queueStartTime = Date.now();
    updateQueueDuration();
    queueDurationInterval = setInterval(updateQueueDuration, 1000);
  }

  // Mark all currently waiting items as active
  for (let qi = 0; qi < queue.length; qi++) {
    if (queue[qi].status === 'waiting') updateQueueItem(qi, 'active', 0);
  }
  
  setStatus(`Downloading track(s)…`, true);

  const config = buildConfig();
  const indices = waiting.map(w => w.index);
  console.log('[startDownloadQueue] Indices to download:', indices);
  console.log('[startDownloadQueue] Config:', config);
  console.log('[startDownloadQueue] pywebview available:', !!window.pywebview?.api);

  if (window.pywebview?.api) {
    try {
      console.log('[startDownloadQueue] Calling download_tracks with indices:', indices);
      // Send the tracks directly to the Python backend
      const op = window.pywebview.api.download_tracks(indices, config);
      console.log('[startDownloadQueue] download_tracks returned:', op);
      if (op && typeof op.catch === 'function') {
        op.catch(e => {
          console.error('[startDownloadQueue] Download error:', e);
          indices.forEach(idx => {
            const qi = queue.findIndex(q => q.index === idx);
            if (qi >= 0 && queue[qi].status === 'active') updateQueueItem(qi, 'error', 0);
          });
          logMessage('Download error: ' + e, 'error');
          setStatus('Error during download.');
        });
      }
    } catch(e) {
      console.error('[startDownloadQueue] Exception:', e);
      logMessage('Download error: ' + e, 'error');
    }
  } else {
    console.warn('[startDownloadQueue] pywebview not available, using demo fallback');
    // Demo fallback for immediate download execution
    for (let idx of indices) {
      const qi = queue.findIndex(q => q.index === idx);
      if (qi < 0 || queue[qi].status !== 'active') continue;
      
      const demoProgress = async () => {
        for (let p = 20; p <= 100; p += 20) {
          updateQueueItem(qi, 'active', p);
          await new Promise(r => setTimeout(r, 150));
        }
        updateQueueItem(qi, 'done', 100);
      };
      demoProgress();
    }
  }
}

// ── UI State Helpers ────────────────────────────────────────────────────────
// Global flag to prevent spam clicks
let isFetchingData = false;
let toastTimeout;

let currentFetchToastId = null;

function setFetchingState(state, customMsg = null) {
  // Backward compatibility
  if (state === true) state = 'start';
  if (state === false) state = 'hide';

  const rw = $('recent-wrap');
  const fetchBtn = $('fetchBtn');
  const urlInput = $('urlInput');

  // Update global lock state
  isFetchingData = (state === 'start');

  // Lock/Unlock UI components
  if (rw) {
    if (isFetchingData) rw.classList.add('fetching-disabled');
    else rw.classList.remove('fetching-disabled');
  }
  if (fetchBtn) fetchBtn.disabled = isFetchingData;
  const isSearchMode = $('searchMode')?.value === 'search';
  if (urlInput) urlInput.disabled = isFetchingData && !isSearchMode;

  if (state === 'start') {
    const title = customMsg || 'fetching metadata...';
    // If a toast is already open, close it before opening another
    if (currentFetchToastId) toastMgr.dismiss(currentFetchToastId);
    
    // Plain text, not markup: toastMgr escapes the message (see
    // toast-system.js's escapeHtml), so the <div> that used to be passed
    // here rendered as a literal tag in the corner of the window — which
    // is what made a normal fetch look like a debug popup.
    currentFetchToastId = toastMgr.loading(
      'please wait...',
      { title: title, position: 'bottom-left' } // Lo teniamo a sinistra come l'originale
    );
  }
  else if (state === 'success') {
    if (currentFetchToastId) toastMgr.dismiss(currentFetchToastId);
    toastMgr.success('Tracklist loaded.', { title: 'Done', position: 'bottom-left', duration: 2500 });
    currentFetchToastId = null;
  }
  else if (state === 'error') {
    if (currentFetchToastId) toastMgr.dismiss(currentFetchToastId);
    const errorTitle = customMsg || 'error occurred';
    toastMgr.error('Unable to retrieve data', { title: errorTitle, position: 'bottom-left', duration: 3500 });
    currentFetchToastId = null;
  }
  else if (state === 'hide') {
    if (currentFetchToastId) toastMgr.dismiss(currentFetchToastId);
    currentFetchToastId = null;
  }
}

// ── Main fetch action ─────────────────────────────────────────────────────────
async function onFetch() {
  if (isFetchingData) return;

  const mode = $('searchMode').value;
  const url = $('urlInput').value.trim();
  // 1. Basic check: input must not be empty in any mode
  if (!url) {
    setFetchingState('error', "Input empty. Please enter a URL or search term.");
    return;
  }

  if (mode === 'search') {
     saveRecentSearch(url);
  }

  if (mode === 'link') {
    const isUrl = url.startsWith('http') || url.startsWith('https') || url.startsWith('spotify:');
    if (!isUrl) {
      setFetchingState('error', "Invalid URL. Please enter a valid URL.");
      return; // Blocca l'esecuzione
    }

    if (url.toLowerCase().includes('amazon.')) {
      showToast("Amazon links cannot be inserted.");
      return; 
    }
  }

  if (mode === 'search') {
    if (url.length < 2) {
      setFetchingState('error', "Search term not valid. Please enter a non-link search term.");
      return;
    }
  }

  setFetchingState('start');

  if (mode === 'search') {
    highlightRecentCard(url);
    setStatus(`Searching "${url}"...`, true);
    // -quiet: still a line in the log panel, but setFetchingState('start')
    // above already raised a bottom-left "fetching" toast for this same
    // search — logMessage's own auto-toast would otherwise stack a second
    // "fetching" notice in the opposite corner for the same action.
    logMessage(`Text search: ${url}`, 'info-quiet');
    currentUrl = url;

    if (window.pywebview?.api) {
      window.pywebview.api.search_provider_async(url, 50)
        .then(() => {
          setStatus(`Searching "${url}"...`, true);
        })
        .catch((e) => {
          setStatus('Provider search error.', false);
          logMessage('Search error: ' + e, 'error');
          setFetchingState('error');
        });
    } else {
      const searchUrl = `https://open.spotify.com/search/$${encodeURIComponent(url)}`;
      window.open(searchUrl, '_blank');
      setStatus('Demo: opened Spotify in browser (Python not connected)', false);
      setFetchingState('success'); // Sblocca demo
    }
    return;
  }

  highlightRecentCard(url);
  setStatus('Fetching metadata…', true);
  // -quiet, same reason as the search-mode branch above: setFetchingState('start')
  // already put up a "fetching metadata…" toast bottom-left for this fetch.
  logMessage(`Fetching: ${url}`, 'info-quiet');
  currentUrl = url;
  showSkeletonTracks(5);

  if (window.pywebview?.api) {
    try {
      await window.pywebview.api.fetch_metadata(url);
      // We do not unlock the UI here. Wait for Python callbacks!
    } catch (e) {
      logMessage('Fetch error: ' + e, 'error');
      setFetchingState('error'); // Sblocca in caso di crash Python
    }
  } else {
    // Demo mode
    setTimeout(() => {
      setAlbumCard('ICEMAN', 'Drake', '', 'FLAC · 3 tracks');
      const demo = [
        { index:0, id:'abc1', title:'Make Them Cry', artist:'Drake', duration_ms:307000, explicit:true,  cover:'', isrc:'USRC12345678', external_url:'https://open.spotify.com/track/abc1', preview_url:'https://p.scdn.co/mp3-preview/abc1', playcount:'1234567' }
      ];
      renderTracks(demo, 1);
      setStatus('Found: ICEMAN (1 track) — demo mode', false);
      logMessage('Demo data loaded (Python not connected)', 'warn');
      setFetchingState('success'); // Sblocca demo con successo
    }, 1500);
  }
}

// ── Build config ──────────────────────────────────────────────────────────────
function buildConfig() {
  return {
    services:               getChecked('services-list').length ? getChecked('services-list') : ['tidal'],
    quality:                $('config-quality').value,
    allow_fallback:         $('config-fallback').checked,
    lyrics:                 $('config-lyrics').checked,
    lyrics_providers:       getChecked('lyrics-list'),
    apple_lyrics_word_by_word: $('config-apple-wbw') ? $('config-apple-wbw').checked : true,
    enrich_metadata:        $('config-enrich').checked,
    enrich_providers:       getChecked('enrich-list'),
    filename_format:         $('config-filename').value.trim() || '{title} - {artist}',
    use_track_numbers:      $('config-track-numbers').checked,
    use_album_track_numbers:$('config-album-track-numbers').checked,
    use_artist_subfolders:  $('config-artist-sub').checked,
    use_album_subfolders:   $('config-album-sub').checked,
    create_playlist_subfolders: $('config-playlist-sub') ? $('config-playlist-sub').checked : true,
    first_artist_only:       $('config-first-artist').checked,
    artist_separator:        $('config-artist-separator')?.value.trim() || null,
    transcode_to:           $('config-transcode')?.value || 'none',
    transcode_bitrate:      $('config-transcode-bitrate')?.value || '320k',
    transcode_keep_original: $('config-transcode-keep')?.checked || false,
    track_max_retries:      parseInt($('config-retries').value) || 0,
    post_download_action:   $('config-post-action').value,
    post_download_command:  $('config-post-cmd')?.value?.trim() || '',
    qobuz_local_api_url:    $('config-qobuz-local-api').value.trim() || null,
    tidal_custom_api:       $('config-tidal-api').value.trim()  || null,
    acoustid_api_key:       $('config-acoustid-key')?.value.trim() || '',
    loop:                   parseInt($('config-loop').value) || null,
    log_level:              $('config-loglevel').value,
  };
}

let apiConfigTarget = null;

function openApiConfigPopup(target) {
  apiConfigTarget = target;
  const title = target === 'qobuz' ? 'Qobuz local API' : 'Custom Tidal API';
  const description = target === 'qobuz'
    ? 'Enter your local Qobuz stream API URL and verify reachability.'
    : 'Enter your self-hosted hifi-api instance URL and verify reachability.';
  const existingValue = target === 'qobuz'
    ? $('config-qobuz-local-api').value.trim()
    : $('config-tidal-api').value.trim();

  const status = $('api-config-status');
  $('api-config-title').textContent = title;
  $('api-config-desc').textContent = description;
  $('api-config-value').value = existingValue || '';
  status.textContent = 'Enter a URL and press Check.';
  status.style.color = '';
  const helpLink = $('api-config-help');
  if (helpLink) {
    if (target === 'qobuz') {
      helpLink.href = 'https://github.com/BartolomeoRusso9/qobuz-api';
      helpLink.textContent = 'How to create your own instance';
    } else {
      helpLink.href = 'https://github.com/binimum/hifi-api';
      helpLink.textContent = 'How to create your own instance';
    }
  }
  $('api-config-modal').classList.remove('hidden');
  setTimeout(() => $('api-config-value').focus(), 0);
}

function closeApiConfigPopup() {
  apiConfigTarget = null;
  $('api-config-modal').classList.add('hidden');
}

function normalizeApiInput(raw) {
  const trimmed = raw.trim();
  if (!trimmed) return '';
  const first = trimmed.split(/\s+/)[0];
  return first;
}

async function checkApiConfig() {
  const rawValue = $('api-config-value').value;
  const url = normalizeApiInput(rawValue);
  const status = $('api-config-status');
  const button = $('api-config-check-btn');
  if (!url) {
    status.textContent = 'Enter a URL first.';
    status.style.color = 'var(--red)';
    return;
  }
  if (rawValue.trim() !== url) {
    status.textContent = 'Multiple URLs detected; only the first will be tested.';
    status.style.color = 'var(--yellow)';
    $('api-config-value').value = url;
  } else {
    status.textContent = 'Checking…';
    status.style.color = '';
  }
  button.disabled = true;
  try {
    if (!window.pywebview?.api) {
      status.textContent = 'API check unavailable in this environment.';
      status.style.color = 'var(--red)';
      return;
    }
    let result = null;
    if (apiConfigTarget === 'qobuz') {
      result = await window.pywebview.api.check_qobuz_api(url);
    } else if (apiConfigTarget === 'tidal') {
      result = await window.pywebview.api.check_tidal_api(url);
    }
    if (result?.ok) {
      status.textContent = 'Reachable ✓';
      status.style.color = 'var(--green)';
    } else {
      status.textContent = `Check failed: ${result?.error || 'invalid response'}`;
      status.style.color = 'var(--red)';
    }
  } catch (e) {
    status.textContent = `Check failed: ${e?.message || e}`;
    status.style.color = 'var(--red)';
  } finally {
    button.disabled = false;
  }
}

function clearApiConfigValue() {
  const input = $('api-config-value');
  const status = $('api-config-status');
  const current = normalizeApiInput(input.value);
  if (!current) {
    status.textContent = 'No API configured to clear.';
    status.style.color = 'var(--red)';
    return;
  }
  input.value = '';
  status.textContent = 'API cleared from the field. Save to remove it from settings.';
  status.style.color = 'var(--green)';
}

function saveApiConfig() {
  if (!apiConfigTarget) return;
  const rawValue = $('api-config-value').value;
  const value = normalizeApiInput(rawValue);
  if (apiConfigTarget === 'qobuz') {
    $('config-qobuz-local-api').value = value;
  } else if (apiConfigTarget === 'tidal') {
    $('config-tidal-api').value = value;
  }
  if (rawValue.trim() !== value) {
    const status = $('api-config-status');
    status.textContent = 'Only the first URL was saved.';
    status.style.color = 'var(--yellow)';
  }
  updateAllApiConfigDisplays();
  closeApiConfigPopup();
  isDirty = true;
  updateSaveButtonVisual();
}

function updateApiConfigDisplay(target) {
  const value = target === 'qobuz'
    ? $('config-qobuz-local-api').value.trim()
    : $('config-tidal-api').value.trim();
  const display = $(target === 'qobuz' ? 'config-qobuz-local-api-display' : 'config-tidal-api-display');
  if (!display) return;
  display.textContent = value ? 'Configured' : 'Not set';
  display.classList.toggle('configured', !!value);
}

function updateAllApiConfigDisplays() {
  updateApiConfigDisplay('qobuz');
  updateApiConfigDisplay('tidal');
}

// ── Profiles ──────────────────────────────────────────────────────────────────
async function saveProfile() {
  const name = $('profile-name').value.trim();
  if (!name) { logMessage('Enter a profile name', 'error'); return; }
  if (window.pywebview?.api) {
    await window.pywebview.api.save_profile_data(name, buildConfig());
    logMessage(`Profile '${name}' saved.`, 'ok');
    loadHistoryAndProfiles();
  }
}
async function deleteProfile() {
  const name = $('profile-select').value;
  if (!name) {
    logMessage('Select a profile to delete.', 'error');
    return;
  }
  if (!confirm(`Delete profile '${name}'? This cannot be undone.`)) return;
  if (window.pywebview?.api) {
    const result = await window.pywebview.api.delete_profile_data(name);
    if (result) {
      logMessage(`Profile '${name}' deleted.`, 'ok');
      loadHistoryAndProfiles();
    } else {
      logMessage(`Unable to delete profile '${name}'.`, 'error');
    }
  }
}
async function loadProfile() {
  const name = $('profile-select').value;
  if (!name || !window.pywebview?.api) return;
  const data = await window.pywebview.api.load_profile_data(name);
  if (!data) return;
  if (data.quality)                $('config-quality').value            = data.quality;
  if (data.filename_format)         $('config-filename').value           = data.filename_format;
  $('config-qobuz-local-api').value = data.qobuz_local_api_url || '';
  $('config-tidal-api').value       = data.tidal_custom_api || '';
  $('config-track-numbers').checked = !!data.use_track_numbers; onTNChange();
  $('config-lyrics').checked = data.lyrics !== false;
  if ($('config-apple-wbw')) $('config-apple-wbw').checked = data.apple_lyrics_word_by_word !== false;
  $('config-enrich').checked        = data.enrich_metadata !== false; onEnrichChange();
  updateAllApiConfigDisplays();
  isDirty = true;
  updateSaveButtonVisual();
  logMessage(`Profile '${name}' loaded.`, 'ok');
}

/**
 * Renders health-check results for providers and extensions.
 * @param {Array<Object>} data - Health-check result rows containing provider identifiers and endpoint status.
 */
function renderHealthResults(data) {
  // Extension service rows are shown separately; they are not
  // a music provider and should not count toward the provider total.
  const extRows  = data.filter(r => r.provider === 'extensions');
  const provRows = data.filter(r => r.provider !== 'extensions');

  // Group by provider first
  const provMap = {};
  provRows.forEach(r => { if (!provMap[r.provider]) provMap[r.provider] = []; provMap[r.provider].push(r); });

  // Calcoliamo i provider totali e quelli con almeno un endpoint funzionante (ok)
  const totalProviders = Object.keys(provMap).length;
  const okProviders = Object.values(provMap).filter(rows => rows.some(r => r.ok)).length;

  updateStatusSummary(`${okProviders}/${totalProviders} providers OK`);
  updateOverallStatus(okProviders, totalProviders);
  
  const container = $('hc-results'); container.innerHTML = '';
  Object.entries(provMap).forEach(([prov, rows]) => {
    const anyOk = rows.some(r => r.ok);
    const okCount = rows.filter(r => r.ok).length;
    const totalCount = rows.length;
    const group  = document.createElement('div');
    group.className = 'hc-prov-group s-section';
    group.style.padding = '10px 12px';
    group.innerHTML = `
      <div class="hc-prov-name">
        <span class="hc-dot ${anyOk ? 'ok' : 'err'}"></span>
        ${prov}
        <span style="font-size:10px;font-weight:400;color:var(--muted)">${okCount}/${totalCount} endpoints OK</span>
      </div>
    `;
    container.appendChild(group);
  });

  // Sezione a parte per le estensioni
  if (extRows.length) {
    const anyOk = extRows.some(r => r.ok);
    const okCount = extRows.filter(r => r.ok).length;
    const totalCount = extRows.length;
    const group = document.createElement('div');
    group.className = 'hc-prov-group hc-ext-group s-section';
    group.style.padding = '10px 12px';
    group.style.marginTop = '10px';
    group.innerHTML = `
      <div class="hc-prov-name">
        <span class="hc-dot ${anyOk ? 'ok' : 'err'}"></span>
        Extensions
        <span style="font-size:10px;font-weight:400;color:var(--muted)">${okCount}/${totalCount} OK</span>
      </div>
    `;
    container.appendChild(group);
  }
}

// ── Window controls (no-drag safe wrappers) ───────────────────────────────────
function pyWin(method, arg) {
  if (arg !== undefined) window.pywebview?.api?.[method]?.(arg);
  else window.pywebview?.api?.[method]?.();
}

// ── Extension Registries ───────────────────────────────────────────────────
const REGISTRY_SOURCE_LABELS = {
  environment: 'Terminal export',
  env_file: '.env file',
  custom: 'Added in app',
};

function regEscapeHtml(str) {
  const d = document.createElement('div');
  d.textContent = str ?? '';
  return d.innerHTML
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

async function loadRegistries() {
  const list = $('registry-list');
  if (!list) return;
  if (!window.pywebview?.api?.get_registries) {
    list.innerHTML = '<div class="s-label" style="font-size:11.5px;">Registry management is unavailable in this build.</div>';
    return;
  }
  try {
    const registries = await window.pywebview.api.get_registries();
    renderRegistries(registries);
  } catch (e) {
    list.innerHTML = '<div class="s-label" style="font-size:11.5px;color:var(--red);">Unable to load registries.</div>';
  }
}

function renderRegistries(registries) {
  const list = $('registry-list');
  if (!list) return;

  if (registries?.error) {
    list.innerHTML = '<div class="s-label" style="font-size:11.5px;color:var(--red);">Failed to load registries: ' + regEscapeHtml(registries.message || 'unknown error') + '</div>';
    return;
  }

  if (!registries || !registries.length) {
    list.innerHTML = '<div class="s-label" style="font-size:11.5px;">No registry links configured yet.</div>';
    return;
  }

  list.innerHTML = registries.map((r) => {
    const badges = (r.sources || []).map((s) =>
      `<span class="reg-badge reg-badge-${regEscapeHtml(s)}">${regEscapeHtml(REGISTRY_SOURCE_LABELS[s] || s)}</span>`
    ).join('');
    const disabledCls = r.enabled ? '' : ' reg-item-disabled';
    return `
      <div class="sort-item reg-item${disabledCls}">
        <div class="reg-item-main">
          <span class="reg-url" title="${regEscapeHtml(r.url)}">${regEscapeHtml(r.url)}</span>
          <div class="reg-badges">${badges}${r.enabled ? '' : '<span class="reg-badge reg-badge-off">Removed</span>'}</div>
        </div>
        <button class="act-btn secondary reg-remove-btn" type="button" onclick="removeRegistryLink('${encodeURIComponent(r.url)}')" title="Remove this registry link">
          Remove
        </button>
      </div>`;
  }).join('');
}

async function addRegistryLink() {
  const input = $('registry-url-input');
  const url = (input?.value || '').trim();
  if (!url) return;
  if (!window.pywebview?.api?.add_registry) {
    showToast('Registry management is unavailable in this build.', 'error');
    return;
  }
  try {
    const result = await window.pywebview.api.add_registry(url);
    if (result?.ok) {
      input.value = '';
      renderRegistries(result.registries || []);
      logMessage(`Registry added: ${url}`, 'ok');
      showToast('Registry link added.');
    } else {
      showToast(result?.error || 'Unable to add registry link.', 'error');
    }
  } catch (e) {
    showToast('Unable to add registry link.', 'error');
  }
}

async function removeRegistryLink(encodedUrl) {
  const url = decodeURIComponent(encodedUrl);
  if (!window.pywebview?.api?.remove_registry) return;
  try {
    const result = await window.pywebview.api.remove_registry(url);
    if (result?.ok) {
      renderRegistries(result.registries || []);
      logMessage(`Registry removed: ${url}`, 'ok');
      showToast('Registry link removed.');
    } else {
      showToast(result?.error || 'Unable to remove registry link.', 'error');
    }
  } catch (e) {
    showToast('Unable to remove registry link.', 'error');
  }
}

// ── Registry Discovery (Directories) ─────────────────────────────────────────
// A directory lists *registries* (for review), not extensions directly —
// see extensions/directories.py. Same shape/conventions as the registry
// functions just above, on purpose.
async function loadDirectories() {
  const list = $('directory-list');
  if (!list) return;
  if (!window.pywebview?.api?.get_registry_directories) {
    list.innerHTML = '<div class="s-label" style="font-size:11.5px;">Directory management is unavailable in this build.</div>';
    return;
  }
  try {
    const directories = await window.pywebview.api.get_registry_directories();
    renderDirectories(directories);
  } catch (e) {
    list.innerHTML = '<div class="s-label" style="font-size:11.5px;color:var(--red);">Unable to load directories.</div>';
  }
}

function renderDirectories(directories) {
  const list = $('directory-list');
  if (!list) return;

  if (directories?.error) {
    list.innerHTML = '<div class="s-label" style="font-size:11.5px;color:var(--red);">Failed to load directories: ' + regEscapeHtml(directories.error) + '</div>';
    return;
  }
  if (!directories || !directories.length) {
    list.innerHTML = '<div class="s-label" style="font-size:11.5px;">No directory links configured yet.</div>';
    return;
  }

  list.innerHTML = directories.map((d) => {
    const badges = (d.sources || []).map((s) =>
      `<span class="reg-badge reg-badge-${regEscapeHtml(s)}">${regEscapeHtml(REGISTRY_SOURCE_LABELS[s] || s)}</span>`
    ).join('');
    const disabledCls = d.enabled ? '' : ' reg-item-disabled';
    return `
      <div class="sort-item reg-item${disabledCls}">
        <div class="reg-item-main">
          <span class="reg-url" title="${regEscapeHtml(d.url)}">${regEscapeHtml(d.url)}</span>
          <div class="reg-badges">${badges}${d.enabled ? '' : '<span class="reg-badge reg-badge-off">Removed</span>'}</div>
        </div>
        <button class="act-btn secondary reg-remove-btn" type="button" onclick="removeDirectoryLink('${encodeURIComponent(d.url)}')" title="Remove this directory link">
          Remove
        </button>
      </div>`;
  }).join('');
}

async function addDirectoryLink() {
  const input = $('directory-url-input');
  const url = (input?.value || '').trim();
  if (!url) return;
  if (!window.pywebview?.api?.add_registry_directory) {
    showToast('Directory management is unavailable in this build.', 'error');
    return;
  }
  try {
    const result = await window.pywebview.api.add_registry_directory(url);
    if (result?.ok) {
      input.value = '';
      renderDirectories(result.directories || []);
      logMessage(`Directory added: ${url}`, 'ok');
      showToast('Directory link added.');
    } else {
      showToast(result?.error || 'Unable to add directory link.', 'error');
    }
  } catch (e) {
    showToast('Unable to add directory link.', 'error');
  }
}

async function removeDirectoryLink(encodedUrl) {
  const url = decodeURIComponent(encodedUrl);
  if (!window.pywebview?.api?.remove_registry_directory) return;
  try {
    const result = await window.pywebview.api.remove_registry_directory(url);
    if (result?.ok) {
      renderDirectories(result.directories || []);
      logMessage(`Directory removed: ${url}`, 'ok');
      showToast('Directory link removed.');
    } else {
      showToast(result?.error || 'Unable to remove directory link.', 'error');
    }
  } catch (e) {
    showToast('Unable to remove directory link.', 'error');
  }
}

async function runDiscovery() {
  const btn = $('btn-run-discovery');
  const results = $('discovery-results');
  if (!results) return;
  if (!window.pywebview?.api?.discover_registries) {
    showToast('Discovery is unavailable in this build.', 'error');
    return;
  }
  if (btn) { btn.disabled = true; btn.textContent = 'Discovering…'; }
  results.innerHTML = '<div class="s-label" style="font-size:11.5px;">Probing registries…</div>';
  try {
    const byDirectory = await window.pywebview.api.discover_registries();
    renderDiscoveryResults(byDirectory);
  } catch (e) {
    results.innerHTML = '<div class="s-label" style="font-size:11.5px;color:var(--red);">Discovery failed.</div>';
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Discover Registries'; }
  }
}

function renderDiscoveryResults(byDirectory) {
  const results = $('discovery-results');
  if (!results) return;

  if (byDirectory?.error) {
    results.innerHTML = '<div class="s-label" style="font-size:11.5px;color:var(--red);">' + regEscapeHtml(byDirectory.error) + '</div>';
    return;
  }

  const directoryUrls = Object.keys(byDirectory || {});
  if (!directoryUrls.length) {
    results.innerHTML = '<div class="s-label" style="font-size:11.5px;">No directories configured, or none reachable.</div>';
    return;
  }

  results.innerHTML = directoryUrls.map((dirUrl) => {
    const rows = (byDirectory[dirUrl] || []).map((r) => {
      const health = r.health || {};
      const badge = health.reachable
        ? `<span class="reg-badge reg-badge-reachable">Reachable · ${health.extension_count} ext.</span>`
        : `<span class="reg-badge reg-badge-unreachable">Unreachable</span>`;
      return `
        <div class="sort-item reg-item">
          <div class="reg-item-main">
            <span class="reg-url" title="${regEscapeHtml(r.url)}">${regEscapeHtml(r.name)} — ${regEscapeHtml(r.url)}</span>
            <div class="reg-badges">${badge}</div>
          </div>
          <button class="act-btn secondary reg-remove-btn" type="button" onclick="addDiscoveredAsRegistry('${encodeURIComponent(r.url)}')">
            Add as Registry
          </button>
        </div>`;
    }).join('');
    return `<div class="s-label" style="font-size:11px;margin:8px 0 4px;">${regEscapeHtml(dirUrl)}</div>${rows}`;
  }).join('');
}

async function addDiscoveredAsRegistry(encodedUrl) {
  const url = decodeURIComponent(encodedUrl);
  if (!window.pywebview?.api?.add_registry) return;
  try {
    const result = await window.pywebview.api.add_registry(url);
    if (result?.ok) {
      renderRegistries(result.registries || []);
      showToast('Registry added.');
    } else {
      showToast(result?.error || 'Unable to add registry.', 'error');
    }
  } catch (e) {
    showToast('Unable to add registry.', 'error');
  }
}

// ── Trusted Signing Keys ──────────────────────────────────────────────────────
// See extensions/trust.py: an Ed25519 signature check layered on top of the
// sha256 checksum ExtensionManager already enforces. Nothing trusted by
// default — an unsigned entry is exactly as trusted as it was before.
async function loadTrustedKeys() {
  const list = $('trust-key-list');
  if (!list) return;
  if (!window.pywebview?.api?.get_trusted_keys) {
    list.innerHTML = '<div class="s-label" style="font-size:11.5px;">Trust management is unavailable in this build.</div>';
    return;
  }
  try {
    const keys = await window.pywebview.api.get_trusted_keys();
    renderTrustedKeys(keys);
  } catch (e) {
    list.innerHTML = '<div class="s-label" style="font-size:11.5px;color:var(--red);">Unable to load trusted keys.</div>';
  }
}

function renderTrustedKeys(keys) {
  const list = $('trust-key-list');
  if (!list) return;

  if (keys?.error) {
    list.innerHTML = '<div class="s-label" style="font-size:11.5px;color:var(--red);">Failed to load trusted keys: ' + regEscapeHtml(keys.error) + '</div>';
    return;
  }
  if (!keys || !keys.length) {
    list.innerHTML = '<div class="s-label" style="font-size:11.5px;">No trusted keys yet.</div>';
    return;
  }

  list.innerHTML = keys.map((k) => `
    <div class="sort-item reg-item">
      <div class="reg-item-main">
        <span class="reg-url">${regEscapeHtml(k.name)}</span>
        <div class="reg-badges"><span class="reg-badge">${regEscapeHtml((k.public_key_b64 || '').slice(0, 16))}…</span></div>
      </div>
      <button class="act-btn secondary reg-remove-btn" type="button" onclick="removeTrustedKeyLink('${encodeURIComponent(k.name)}')">
        Remove
      </button>
    </div>`).join('');
}

async function addTrustedKeyLink() {
  const nameInput = $('trust-key-name-input');
  const keyInput = $('trust-key-value-input');
  const name = (nameInput?.value || '').trim();
  const key = (keyInput?.value || '').trim();
  if (!name || !key) {
    showToast('Enter both a name and a public key.', 'error');
    return;
  }
  if (!window.pywebview?.api?.add_trusted_key) {
    showToast('Trust management is unavailable in this build.', 'error');
    return;
  }
  try {
    const result = await window.pywebview.api.add_trusted_key(name, key);
    if (result?.ok) {
      nameInput.value = '';
      keyInput.value = '';
      renderTrustedKeys(result.keys || []);
      showToast('Trusted key added.');
    } else {
      showToast(result?.error || 'Unable to add trusted key.', 'error');
    }
  } catch (e) {
    showToast('Unable to add trusted key.', 'error');
  }
}

async function removeTrustedKeyLink(encodedName) {
  const name = decodeURIComponent(encodedName);
  if (!window.pywebview?.api?.remove_trusted_key) return;
  try {
    const result = await window.pywebview.api.remove_trusted_key(name);
    if (result?.ok) {
      loadTrustedKeys();
      showToast('Trusted key removed.');
    } else {
      showToast(result?.error || 'Unable to remove trusted key.', 'error');
    }
  } catch (e) {
    showToast('Unable to remove trusted key.', 'error');
  }
}

// ── Duplicate Detection (acoustic fingerprint) ───────────────────────────────
// See core/audio_fingerprint.py. Off by default on the backend (needs the
// optional pyacoustid + fpcalc) — get_dedup_status() lets us say so
// up front instead of just failing after the user waits for a scan.
async function startDedupScan() {
  const path = $('local-path-input').value.trim();
  if (!path) {
    toastMgr.error('Please enter a valid folder or file path.');
    return;
  }

  if (window.pywebview?.api?.get_dedup_status) {
    try {
      const status = await window.pywebview.api.get_dedup_status();
      if (status && status.available === false) {
        toastMgr.error(status.install_hint || 'Duplicate detection is not available on this machine.');
        return;
      }
    } catch (e) {
      // Fall through and let scan_for_duplicates report the real error.
    }
  }

  setTaBtnState($('btn-scan-dedup'), 'loading');
  $('dedup-results-wrap')?.classList.add('hidden');

  try {
    if (window.pywebview?.api && typeof window.pywebview.api.scan_for_duplicates === 'function') {
      const result = await window.pywebview.api.scan_for_duplicates(path);
      if (result && result.status === 'error') {
        throw new Error(result.error || 'Scan failed');
      }
    }
    toastMgr.info('Fingerprinting files for duplicates... this can take a while for a large library.');
  } catch (err) {
    console.error('[Dedup] start failed:', err);
    setTaBtnState($('btn-scan-dedup'), 'error');
    setTimeout(() => setTaBtnState($('btn-scan-dedup'), 'default'), 2500);
    toastMgr.error(err.message || 'Failed to start duplicate scan');
  }
}

// Called by backend when the fingerprint scan finishes
window.app_dedup_results = function (payload) {
  setTaBtnState($('btn-scan-dedup'), 'default');
  renderDuplicateGroups(payload.groups || []);
};

// Called by backend on scan error
window.app_dedup_error = function (err) {
  setTaBtnState($('btn-scan-dedup'), 'error');
  setTimeout(() => setTaBtnState($('btn-scan-dedup'), 'default'), 2500);
  toastMgr.error('Duplicate scan failed: ' + err);
};

function renderDuplicateGroups(groups) {
  const wrap = $('dedup-results-wrap');
  const container = $('dedup-groups');
  if (!wrap || !container) return;

  if (!groups.length) {
    container.innerHTML = '<div class="s-label" style="font-size:11.5px;">No duplicates found.</div>';
    wrap.classList.remove('hidden');
    toastMgr.success('No duplicates found.');
    return;
  }

  container.innerHTML = groups.map((group, i) => `
    <div class="sort-item" style="flex-direction:column;align-items:stretch;gap:6px;cursor:default;">
      <div class="s-label" style="font-size:11px;">Group ${i + 1} (${group.length} files)</div>
      ${group.map((path) => `<span class="reg-url" title="${regEscapeHtml(path)}">${regEscapeHtml(path)}</span>`).join('')}
    </div>`).join('');
  wrap.classList.remove('hidden');
  toastMgr.success(`Found ${groups.length} duplicate group(s).`);
}

// ── Library duplicates (core/library_dedup.py) ───────────────────────────────
// The other duplicate finder: metadata first, so it scales to a real library,
// and it can act on what it found. The report lives on the backend instance
// between the scan and the resolve — this keeps only what the UI needs to
// render it and to say which files the user picked.
let libDedupGroups = [];
let libDedupManifest = '';

// The seconds two durations may differ by and still count as the same track.
// 0 is a meaningful setting — "the durations must match exactly" — so an empty
// or non-numeric box is what falls back to the default, not every falsy parse.
function libDedupTolerance() {
  const parsed = parseFloat($('libdedup-tolerance')?.value ?? '');
  return Number.isFinite(parsed) ? parsed : 4;
}

async function startLibraryDedupScan() {
  const path = $('local-path-input').value.trim();
  if (!path) {
    toastMgr.error('Please enter a valid folder or file path.');
    return;
  }

  if (typeof window.pywebview?.api?.scan_library_duplicates !== 'function') {
    toastMgr.error('The backend is not ready yet — try again in a moment.');
    return;
  }

  const verify = $('libdedup-verify')?.checked || false;
  if (verify && window.pywebview?.api?.get_dedup_status) {
    // Same courtesy as the fingerprint scan: say the optional dependency is
    // missing now, rather than after the user has waited for a walk.
    try {
      const status = await window.pywebview.api.get_dedup_status();
      if (status && status.available === false) {
        toastMgr.error(status.install_hint || 'Audio confirmation is not available on this machine.');
        return;
      }
    } catch (e) {
      // Fall through; the scan reports its own note if it cannot verify.
    }
  }

  setTaBtnState($('btn-libdedup-scan'), 'loading');
  libDedupGroups = [];
  renderLibraryDedup(null);
  $('libdedup-progress')?.classList.remove('hidden');
  if ($('libdedup-progress')) $('libdedup-progress').textContent = 'Scanning…';

  try {
    const result = await window.pywebview.api.scan_library_duplicates(
      path,
      true,
      $('libdedup-match')?.value || 'both',
      libDedupTolerance(),
      verify,
      0.95,
      $('libdedup-db')?.checked || false,
    );
    if (result && result.status === 'error') throw new Error(result.error || 'Scan failed');
  } catch (err) {
    console.error('[LibDedup] start failed:', err);
    setTaBtnState($('btn-libdedup-scan'), 'error');
    setTimeout(() => setTaBtnState($('btn-libdedup-scan'), 'default'), 2500);
    $('libdedup-progress')?.classList.add('hidden');
    toastMgr.error(err.message || 'Failed to start the library scan');
  }
}

window.app_library_dedup_progress = function (payload) {
  const el = $('libdedup-progress');
  if (!el) return;
  el.classList.remove('hidden');
  el.textContent = `Scanning ${payload.done} / ${payload.total} file(s)…`;
};

window.app_library_dedup_results = function (report) {
  setTaBtnState($('btn-libdedup-scan'), 'default');
  $('libdedup-progress')?.classList.add('hidden');
  renderLibraryDedup(report);
};

window.app_library_dedup_error = function (err) {
  setTaBtnState($('btn-libdedup-scan'), 'error');
  setTimeout(() => setTaBtnState($('btn-libdedup-scan'), 'default'), 2500);
  $('libdedup-progress')?.classList.add('hidden');
  toastMgr.error('Library scan failed: ' + err);
};

function renderLibraryDedup(report) {
  const summary = $('libdedup-summary');
  const container = $('libdedup-groups');
  const actions = $('libdedup-actions');
  if (!container) return;

  if (!report) {
    container.innerHTML = '';
    summary?.classList.add('hidden');
    actions?.classList.add('hidden');
    return;
  }

  const lib = report.library || {};
  const lines = [
    `${lib.files || 0} file(s) scanned · ${lib.total_size || '0 B'}`,
    `${report.groups || 0} duplicate group(s) · ${report.duplicate_files || 0} redundant copies · ${report.reclaimable || '0 B'} reclaimable`,
    `${lib.missing_isrc || 0} without ISRC · ${lib.missing_tags || 0} without artist/title${lib.unreadable ? ` · ${lib.unreadable} unreadable` : ''}`,
  ];
  if (report.database) lines.push(`index written to ${report.database}`);
  (report.notes || []).forEach((n) => lines.push(`note: ${n}`));
  if (summary) {
    summary.textContent = lines.join('\n');
    summary.classList.remove('hidden');
  }

  // Each group keeps its own chosen keeper and its own selection, so the
  // user can disagree with the ranking on one group without disturbing the
  // rest. The backend is told both when they resolve.
  libDedupGroups = (report.duplicate_groups || []).map((group) => {
    const files = [group.keep, ...(group.duplicates || [])];
    return {
      key: group.key,
      label: group.label,
      matchedBy: group.matched_by,
      reclaimable: group.reclaimable_bytes || 0,
      files,
      keepPath: group.keep?.path || '',
      selected: new Set((group.duplicates || []).map((f) => f.path)),
    };
  });

  if (!libDedupGroups.length) {
    container.innerHTML = '<div class="s-label" style="font-size:11.5px;">No duplicates found.</div>';
    actions?.classList.add('hidden');
    toastMgr.success('No duplicates found.');
    return;
  }

  container.innerHTML = libDedupGroups.map((group, i) => `
    <div class="sort-item" style="flex-direction:column;align-items:stretch;gap:6px;cursor:default;">
      <div class="s-label" style="font-size:11px;">
        ${regEscapeHtml(group.label)} — ${group.files.length} copies, ${formatLibDedupSize(group.reclaimable)} reclaimable, matched by ${regEscapeHtml(group.matchedBy)}
      </div>
      ${group.files.map((file, j) => {
        const isKeeper = file.path === group.keepPath;
        return `
        <div style="display:flex;align-items:center;gap:8px;">
          <input type="radio" name="libdedup-keep-${i}" ${isKeeper ? 'checked' : ''}
                 onchange="setLibDedupKeeper(${i}, ${j})" title="Keep this copy">
          <input type="checkbox" ${group.selected.has(file.path) ? 'checked' : ''}
                 ${isKeeper ? 'disabled' : ''}
                 onchange="toggleLibDedupFile(${i}, ${j}, this.checked)"
                 title="${isKeeper ? 'The kept copy is never removed' : 'Remove this copy'}">
          <span class="reg-url" style="flex:1;min-width:0;" title="${regEscapeHtml(file.path)}">${regEscapeHtml(file.path)}</span>
          <span class="s-label" style="font-size:10.5px;white-space:nowrap;">${regEscapeHtml(file.quality || '')}</span>
        </div>`;
      }).join('')}
    </div>`).join('');

  if (report.shown_groups !== undefined && report.shown_groups < report.groups) {
    container.innerHTML += `<div class="s-label" style="font-size:11px;">Showing ${report.shown_groups} of ${report.groups} groups — resolve these, then scan again for the rest.</div>`;
  }
  actions?.classList.remove('hidden');
  updateLibDedupCount();
  toastMgr.success(`Found ${report.groups} duplicate group(s), ${report.reclaimable} reclaimable.`);
}

function formatLibDedupSize(bytes) {
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = bytes || 0;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
  return `${unit === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
}

function setLibDedupKeeper(groupIndex, fileIndex) {
  const group = libDedupGroups[groupIndex];
  if (!group) return;
  const chosen = group.files[fileIndex];
  if (!chosen) return;
  // The copy being kept is never also a copy being removed; the one it
  // replaces goes back to being selectable (and selected, since the point of
  // the group is that it is redundant).
  group.selected.delete(chosen.path);
  group.selected.add(group.keepPath);
  group.keepPath = chosen.path;
  renderLibraryDedupGroup(groupIndex);
  updateLibDedupCount();
}

function toggleLibDedupFile(groupIndex, fileIndex, checked) {
  const group = libDedupGroups[groupIndex];
  const file = group?.files[fileIndex];
  if (!file || file.path === group.keepPath) return;
  if (checked) group.selected.add(file.path); else group.selected.delete(file.path);
  updateLibDedupCount();
}

function renderLibraryDedupGroup(groupIndex) {
  // Only the radios and checkboxes of one group change when its keeper does,
  // and re-rendering the whole list would scroll a long report back to the
  // top under the user's cursor.
  const group = libDedupGroups[groupIndex];
  const container = $('libdedup-groups');
  const item = container?.children[groupIndex];
  if (!group || !item) return;
  const rows = item.querySelectorAll('div[style*="display:flex"]');
  group.files.forEach((file, j) => {
    const row = rows[j];
    if (!row) return;
    const [radio, box] = row.querySelectorAll('input');
    const isKeeper = file.path === group.keepPath;
    if (radio) radio.checked = isKeeper;
    if (box) {
      box.disabled = isKeeper;
      box.checked = group.selected.has(file.path);
    }
  });
}

function updateLibDedupCount() {
  const total = libDedupGroups.reduce((sum, g) => sum + g.selected.size, 0);
  const el = $('libdedup-selected');
  if (el) el.textContent = `${total} selected`;
  ['btn-libdedup-trash', 'btn-libdedup-delete'].forEach((id) => {
    const btn = $(id);
    if (btn) btn.disabled = total === 0;
  });
}

async function resolveLibraryDuplicates(action) {
  const paths = [];
  const keepPaths = [];
  libDedupGroups.forEach((group) => {
    if (!group.selected.size) return;
    keepPaths.push(group.keepPath);
    group.selected.forEach((path) => paths.push(path));
  });
  if (!paths.length) {
    toastMgr.error('Nothing selected.');
    return;
  }

  const question = action === 'delete'
    ? `Delete ${paths.length} file(s) permanently? This cannot be undone.`
    : `Move ${paths.length} file(s) into the quarantine folder? You can undo this afterwards.`;
  if (!confirm(question)) return;

  const btn = $(action === 'delete' ? 'btn-libdedup-delete' : 'btn-libdedup-trash');
  setTaBtnState(btn, 'loading');
  try {
    const result = await window.pywebview.api.resolve_library_duplicates(
      paths, keepPaths, action, false,
    );
    if (!result || result.status === 'error') throw new Error(result?.error || 'Failed');

    setTaBtnState(btn, 'default');
    toastMgr.success(`${result.resolved} file(s) resolved, ${result.freed} reclaimed.`);
    (result.actions || [])
      .filter((a) => a.action === 'skip')
      .slice(0, 10)
      .forEach((a) => toastMgr.info(`Left alone: ${a.path} — ${a.error}`));

    libDedupManifest = result.manifest || '';
    const undo = $('libdedup-undo');
    if (libDedupManifest && action !== 'delete') {
      if ($('libdedup-manifest')) $('libdedup-manifest').textContent = libDedupManifest;
      undo?.classList.remove('hidden');
    } else {
      undo?.classList.add('hidden');
    }

    // The scan describes a library that no longer exists; the backend has
    // dropped the report for the same reason, so offering the stale list
    // again would only produce "gone since the scan" for every row.
    libDedupGroups = [];
    renderLibraryDedup(null);
  } catch (err) {
    console.error('[LibDedup] resolve failed:', err);
    setTaBtnState(btn, 'error');
    setTimeout(() => setTaBtnState(btn, 'default'), 2500);
    toastMgr.error(err.message || 'Could not resolve the duplicates');
  }
}

async function undoLibraryDedup() {
  if (!libDedupManifest) return;
  setTaBtnState($('btn-libdedup-undo'), 'loading');
  try {
    const result = await window.pywebview.api.restore_library_duplicates(libDedupManifest);
    if (!result || result.status === 'error') throw new Error(result?.error || 'Failed');
    setTaBtnState($('btn-libdedup-undo'), 'default');
    toastMgr.success(`${result.resolved} file(s) put back.`);
    (result.actions || [])
      .filter((a) => a.action === 'skip')
      .slice(0, 10)
      .forEach((a) => toastMgr.info(`Not restored: ${a.path} — ${a.error}`));
    libDedupManifest = '';
    $('libdedup-undo')?.classList.add('hidden');
  } catch (err) {
    setTaBtnState($('btn-libdedup-undo'), 'error');
    setTimeout(() => setTaBtnState($('btn-libdedup-undo'), 'default'), 2500);
    toastMgr.error(err.message || 'Could not restore the files');
  }
}

// ── Multi-user auth (--web-multiuser; web mode only) ─────────────────────────
// Desktop/pywebview mode has no concept of accounts, so all of this is a
// no-op there — gated on __SPOTIFLAC_WEB_MODE__, set only by webapp.py's
// index() route (see web-shim.js / sw.js for the same gate elsewhere).
async function checkAuthStatus() {
  if (!window.__SPOTIFLAC_WEB_MODE__) return;
  try {
    const res = await fetch('/api/auth/status');
    const status = await res.json();
    $('account-signout-row')?.classList.toggle('hidden', !status.multiuser);
    if (status.multiuser && !status.logged_in) {
      $('login-modal')?.classList.remove('hidden');
    }
  } catch (e) {
    // Can't reach the server at all — the rest of the app will surface
    // its own connection errors; nothing useful to add here.
  }
}

async function doLogin() {
  const username = ($('login-username')?.value || '').trim();
  const password = $('login-password')?.value || '';
  const errorEl = $('login-error');
  if (errorEl) errorEl.textContent = '';
  if (!username || !password) {
    if (errorEl) errorEl.textContent = 'Enter a username and password.';
    return;
  }
  const btn = $('login-submit-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Signing in…'; }
  try {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      if (errorEl) errorEl.textContent = data.error || 'Invalid username or password.';
      return;
    }
    $('login-modal')?.classList.add('hidden');
    $('login-password').value = '';
    $('account-signout-row')?.classList.remove('hidden');
  } catch (e) {
    if (errorEl) errorEl.textContent = 'Could not reach the server.';
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Sign in'; }
  }
}

async function doLogout() {
  try {
    await fetch('/api/auth/logout', { method: 'POST' });
  } catch (e) {
    // Best-effort — reload regardless so the login screen reappears.
  }
  location.reload();
}

// --- EXPLORE LOGIC ---
async function loadExploreData() {
  const sectionsContainer = $('explore-sections');
  const greetingEl = $('explore-greeting');
  
  if (!sectionsContainer) return;
  
  sectionsContainer.innerHTML = '<div style="text-align:center; padding: 40px; color: var(--muted);">Loading feed...</div>';
  if (window.pywebview?.api?.get_spotify_home_feed) {
    try {
      const homeData = await window.pywebview.api.get_spotify_home_feed();
      
      if (homeData && homeData.success) {
        if (greetingEl) greetingEl.textContent = homeData.greeting || 'Explore';
        renderHomeSections(homeData.sections);
      } else {
        sectionsContainer.innerHTML = '<div style="color:var(--red);">Unable to load feed. Check your connection.</div>';
      }
    } catch (e) {
      logMessage('Failed to load explore feed: ' + e, 'error');
      sectionsContainer.innerHTML = '<div style="color:var(--red);">Network error.</div>';
    }
  } else {
    // Demo Mode
    if (greetingEl) greetingEl.textContent = 'Explore (Demo)';
    sectionsContainer.innerHTML = '<div style="color:var(--muted);">Python backend not connected. Unable to load recommendations.</div>';
  }
}

function renderHomeSections(sections) {
  const container = $('explore-sections');
  container.innerHTML = '';

  sections.forEach(section => {
    if (!section.items || section.items.length === 0) return;

    const sectionEl = document.createElement('div');
    const titleEl = document.createElement('h3');
    titleEl.className = 'explore-section-title';
    titleEl.textContent = section.title;
    sectionEl.appendChild(titleEl);

    const gridEl = document.createElement('div');
    gridEl.className = 'explore-grid';

    section.items.forEach(item => {
      const card = document.createElement('div');
      card.className = 'explore-card';
      
      const imgUrl = item.cover_url || 'assets/icons/spotify.svg';
      const subText = item.description || item.artists || item.type;

      card.innerHTML = `
        <img src="${escHtml(imgUrl)}" loading="lazy" onerror="this.src='assets/icons/spotify.svg'">
        <div class="explore-card-title" title="${escHtml(item.name)}">${escHtml(item.name)}</div>
        <div class="explore-card-subtitle" title="${escHtml(subText)}">${escHtml(subText)}</div>
      `;

      card.onclick = () => {
        // Return to the home page
        switchView('home');
        
        // Switch to Fetch (Link) mode
        const mode = $('searchMode');
        if (mode && mode.value === 'search') {
          toggleSearchMode(); // Simulate click to set it back to "link"
        }
        
        // Insert the URI
        const input = $('urlInput');
        if (input) {
          input.value = item.uri || `spotify:${item.type}:${item.id}`;
          // Scatena la ricerca
          onFetch(); 
        }
      };

      gridEl.appendChild(card);
    });

    sectionEl.appendChild(gridEl);
    container.appendChild(sectionEl);
  });
}

// ── Boot ──────────────────────────────────────────────────────────────────────
window.addEventListener('pywebviewready', async () => {
  // No "backend connected" line here: the backend logs its own as soon as it
  // is ready (_on_loaded() in app.py, and webapp.py in --web mode), so
  // announcing it from this side too printed the same event twice, in two
  // different capitalisations.
  loadHistoryAndProfiles();
  checkAuthStatus();

  await loadSettingsFromStorage();
  // After the settings, so the saved order is known and can be restored on
  // top of the narrowed list — and outside them, because a machine with no
  // saved settings yet never calls applySettings() at all, which is exactly
  // the fresh install that most needs to be told what it can download from.
  await refreshInstalledServices();
  initSettingsTracking();
  updateSearchMode();
  initPasteButton();
});

window.matchMedia('(prefers-color-scheme: dark)').addEventListener?.('change', syncSystemTheme);

window.addEventListener('beforeunload', function (e) {
    if (isDirty) {
        e.preventDefault();
        e.returnValue = '';
    }
});

// ── Real-time search on keystroke ────────────────────────────────────────────
let _searchDebounceTimer = null;
let _lastSearchQuery = '';

$('urlInput').addEventListener('input', function() {
  const mode = $('searchMode').value;
  if (mode !== 'search') return;

  const query = this.value.trim();

  // Clear results if query is empty
  if (!query) {
    clearSearchUI();
    _lastSearchQuery = '';
    clearTimeout(_searchDebounceTimer);
    const container = $('text-search-results');
    if (container) container.innerHTML = '';
    $('text-search-container')?.classList.add('hidden');
    // Was .remove('hidden') — unhid the track table without ever clearing
    // #track-rows, so backspacing a search query back to empty didn't show
    // "no query" at all: it showed whatever track/album was still sitting
    // in the table from before search mode was even entered (e.g. the
    // track you'd just fetched), looking like a stale result for a search
    // that was never run.
    $('track-table-wrap')?.classList.add('hidden');
    $('track-controls')?.classList.add('hidden');
    if ($('recent-wrap')) $('recent-wrap').style.display = ''; // showSkeletonTracks() hides this once a real query starts
    renderRecentSearches();
    return;
  }

  // Skip if same query
  if (query === _lastSearchQuery) return;

  clearTimeout(_searchDebounceTimer);
  _searchDebounceTimer = setTimeout(() => {
    _lastSearchQuery = query;

    // BEGIN CHANGE: Instead of old loading text, show skeletons!
    // Call the function you just created
    showSkeletonTracks(6); // Show 6 pulsing placeholder rows
    
    // Ensure the table container is visible
    $('track-table-wrap')?.classList.remove('hidden');
    $('text-search-container')?.classList.add('hidden');
    // END CHANGE

    if (window.pywebview?.api) {
      window.pywebview.api.search_provider_async(query, 50).catch(e => {
        logMessage('Real-time search error: ' + e, 'error');
      });
    }
  }, 350);
});

setTimeout(() => {
  if (!window.pywebview) {
    renderRecent([
      { title:'ICEMAN', label:'ICEMAN', url:'https://open.spotify.com/album/0OAv7DCME2AV4q1KPO95HY' },
      { title:'Certified Lover Boy', label:'CLB', url:'https://open.spotify.com/album/3SpBlxme9WbeUDTbAcVsBN' },
    ]);
  }
}, 500);

// ── ffmpeg warning banner ─────────────────────────────────────────────────
// Informational only — SpotiFLAC will still try to install ffmpeg itself
// the first time MP3 transcoding is actually used (see
// core/ffmpeg_check.py's ensure_ffmpeg_installed()); this banner just means
// that hasn't happened/worked yet, not that nothing will be attempted. Tidal
// FLAC muxing and Amazon decryption have no such auto-install and will keep
// failing until ffmpeg is available one way or another.
window.showFfmpegWarning = function(result) {
  // Avoid duplicate banners
  if ($('ffmpeg-warning-banner')) return;

  const banner = document.createElement('div');
  banner.id = 'ffmpeg-warning-banner';
  banner.className = 'ffmpeg-banner';
  banner.innerHTML = `
    <span class="ffmpeg-banner-icon">⚠</span>
    <div class="ffmpeg-banner-body">
      <strong>ffmpeg not found</strong>
      <span>Tidal FLAC muxing and Amazon decryption will be unavailable. MP3 transcoding will try to install ffmpeg automatically the first time you use it.</span>
      <a href="#" class="ffmpeg-banner-link"
        onclick="event.preventDefault(); pyWin('open_url', 'https://ffmpeg.org/download.html')">
        Download ffmpeg
      </a>
    </div>
    <button class="ffmpeg-banner-close" onclick="this.closest('.ffmpeg-banner').remove()" title="Dismiss">✕</button>
  `;

  // Insert right after the search bar
  const searchBar = $('search-bar');
  if (searchBar && searchBar.parentNode) {
    searchBar.parentNode.insertBefore(banner, searchBar.nextSibling);
  }
};

// ── Node.js warning banner ───────────────────────────────────────────────────
// Informational only, like the ffmpeg one above — SpotiFLAC still tries to
// install Node itself the first time a JS extension actually runs (see
// core/node_check.py); this banner just means that hasn't happened/worked
// yet, not that nothing will be attempted.
window.showNodeWarning = function(result) {
  if ($('node-warning-banner')) return;

  const banner = document.createElement('div');
  banner.id = 'node-warning-banner';
  banner.className = 'ffmpeg-banner';
  banner.innerHTML = `
    <span class="ffmpeg-banner-icon">⚠</span>
    <div class="ffmpeg-banner-body">
      <strong>Node.js not found</strong>
      <span>JavaScript extensions won't work until it's installed — SpotiFLAC will try to install it automatically the first time you use one.</span>
      <a href="#" class="ffmpeg-banner-link"
        onclick="event.preventDefault(); pyWin('open_url', 'https://nodejs.org/en/download')">
        Download Node.js
      </a>
    </div>
    <button class="ffmpeg-banner-close" onclick="this.closest('.ffmpeg-banner').remove()" title="Dismiss">✕</button>
  `;

  const searchBar = $('search-bar');
  if (searchBar && searchBar.parentNode) {
    searchBar.parentNode.insertBefore(banner, searchBar.nextSibling);
  }
};
// Sets the two version-label DOM elements. Called from Python via the
// generic _push() bridge (desktop: evaluate_js, web: WebSocket dispatch) —
// see SpotiFLAC/app.py's _push() and frontend/web-shim.js.
// Playcounts that arrived after the table was drawn — a CSV import shows its
// tracklist immediately and fills this column in behind it (see
// _start_csv_playcounts() in api_mixins/csv_import.py), because an arbitrary
// list of tracks costs one Spotify lookup each and there is no album-wide
// query to read them all from at once. Patches the cells in place rather than
// re-rendering, so the user's checkboxes, scroll position and page stay put.
window.app_update_playcounts = function (byId) {
  if (!byId || typeof byId !== 'object') return;
  let patched = 0;
  currentTracks.forEach((t, i) => {
    const count = byId[t.id];
    if (!count) return;
    t.playcount = count;
    patched++;
    // Only the rows on the current page exist in the DOM; the rest pick the
    // value up from currentTracks the next time they are rendered.
    const cb = document.querySelector(`.track-cb[value="${i}"]`);
    const cell = cb && cb.closest('.track-row')?.querySelector('.tr-playcount');
    // A playlist view puts the album name in this column instead — leave it be.
    if (cell && cell.textContent.trim() === '—') {
      cell.textContent = String(count).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    }
  });
  if (patched) logMessage(`Playcount filled in for ${patched} track(s).`, 'debug');
};

window.__set_version_label = function (version) {
  const tb = document.getElementById('tb-version');
  if (tb) tb.innerText = version;
  const hero = document.getElementById('hero-version');
  if (hero) hero.innerText = 'v' + version;
};
// ══════════ LOCAL AUTO-TAGGER LOGIC ══════════

let localScanData = [];

// ── Drag & drop for the "Fix Local Files" target directory ─────────────────
//
// A dropped item only carries a real, absolute filesystem path when the app
// is running as a native pywebview window (pywebview's embedded webview
// exposes File.path for dropped files/folders, unlike a normal browser
// sandbox). In plain browser/web mode there is no way for JS to learn the
// server-side path of something dragged from the user's OS file manager, so
// we fall back to asking the user to type/paste it instead.
function onLocalDragOver(e) {
    e.preventDefault();
    e.stopPropagation();
    $('local-drop-zone').classList.add('drag-over');
}

function onLocalDragLeave(e) {
    e.preventDefault();
    e.stopPropagation();
    $('local-drop-zone').classList.remove('drag-over');
}

function onLocalDrop(e) {
    e.preventDefault();
    e.stopPropagation();
    $('local-drop-zone').classList.remove('drag-over');

    const files = e.dataTransfer?.files;
    if (!files || !files.length) return;

    const dropped = files[0];
    const realPath = dropped.path; // populated by pywebview's desktop webview

    if (realPath) {
        $('local-path-input').value = realPath;
        if (files.length > 1) {
            toastMgr.info(`Using "${realPath.split(/[\\/]/).pop()}" — drop one folder/file at a time.`);
        }
        startLocalScan();
        return;
    }

    toastMgr.warning(
        "Browsers can't reveal the real folder path of a dropped item — "
        + "paste the absolute path into the field above instead."
    );
}

// ── Folder Browser for Local Auto-Tagger ──────────────────────────────────

let currentFolderBrowserPath = null;
let currentFolderBrowserParent = null;

// Wrapper for the "← Back" button: navigateFolderBrowser() needs the real
// parent path from the server (stored after each browse), not a literal
// '..' — the server resolves paths itself and has no notion of a relative
// '..' relative to nothing.
function goFolderBrowserBack() {
    if (currentFolderBrowserParent) {
        navigateFolderBrowser(currentFolderBrowserParent);
    }
}

async function openFolderBrowser() {
    const modal = $('folder-browser-modal');
    modal.classList.remove('hidden');
    modal.focus();

    // Escape key handler
    const escapeHandler = (e) => {
        if (e.key === 'Escape') {
            closeFolderBrowser();
        }
    };
    modal.addEventListener('keydown', escapeHandler);
    modal.dataset.escapeAttached = 'true';

    currentFolderBrowserParent = null;
    const currentPath = $('local-path-input').value.trim() || null;

    if (window.pywebview?.api) {
        const api = window.pywebview.api;
        try {
            const homePath = typeof api.get_home_dir === 'function' ? await api.get_home_dir() : '/';
            await navigateFolderBrowser(homePath || currentPath || '/');
            return;
        } catch (err) {
            console.warn('pywebview folder browse fallback failed:', err);
        }
    }

    if (currentPath) {
        await navigateFolderBrowser(currentPath);
        return;
    }

    try {
        const response = await fetch('/api/get-home-dir');
        if (!response.ok) {
            console.warn('get-home-dir endpoint not available, using /');
            await navigateFolderBrowser('/');
            return;
        }
        const data = await response.json();
        const homePath = data.home_dir || '/';
        await navigateFolderBrowser(homePath);
    } catch (err) {
        console.warn('Failed to get home dir, falling back to root:', err);
        await navigateFolderBrowser('/');
    }
}

function closeFolderBrowser() {
    $('folder-browser-modal').classList.add('hidden');
}

async function navigateFolderBrowser(path) {
    if (!path) return;

    const modal = $('folder-browser-modal');
    if (modal.classList.contains('hidden')) return;

    try {
        let data;

        if (window.pywebview?.api && typeof window.pywebview.api.browse_folder === 'function') {
            data = await window.pywebview.api.browse_folder(path);
        } else {
            console.log('[FolderBrowser] Navigating to:', path);
            const encodedPath = encodeURIComponent(path);
            const url = `/api/browse-folder?path=${encodedPath}`;
            const response = await fetch(url);
            if (!response.ok) {
                const errorData = await response.json().catch(() => ({ error: response.statusText }));
                throw new Error(errorData.error || `HTTP ${response.status}`);
            }
            data = await response.json();
        }

        if (!data || data.error) {
            toastMgr.error(`Browse error: ${data?.error || 'Unknown folder browse error'}`);
            console.error('[FolderBrowser] Backend error:', data?.error || data);
            return;
        }

        currentFolderBrowserPath = data.path;
        currentFolderBrowserParent = data.parent || null;
        $('fb-path').value = data.path;

        const entriesDiv = $('fb-entries');
        entriesDiv.innerHTML = '';

        const items = [];
        (data.directories || []).forEach(dirName => {
            items.push({
                type: 'dir',
                name: dirName,
                path: (data.path || '') + '/' + dirName,
            });
        });
        (data.files || []).forEach(fileName => {
            items.push({
                type: 'file',
                name: fileName,
                path: (data.path || '') + '/' + fileName,
            });
        });

        if (items.length > 0) {
            items.forEach(item => {
                const div = document.createElement('div');
                div.className = 'fb-entry';
                const icon = item.type === 'dir'
                    ? '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>'
                    : '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H7a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M9 13h6M9 17h6"/></svg>';
                div.innerHTML = icon + ' ';
                div.appendChild(document.createTextNode(item.name));

                div.onclick = async () => {
                    if (item.type === 'dir') {
                        await navigateFolderBrowser(item.path);
                        return;
                    }

                    $('local-path-input').value = item.path;
                    closeFolderBrowser();
                    toastMgr.success(`Selected: ${item.path}`);
                    startLocalScan();
                };

                entriesDiv.appendChild(div);
            });
        } else {
            entriesDiv.innerHTML = '<div class="fb-empty">No files or subdirectories found.</div>';
        }

        $('fb-back').disabled = !data.parent;
        $('fb-back').classList.toggle('is-root', !data.parent);

    } catch (err) {
        console.error('[FolderBrowser] Navigation error:', err);
        toastMgr.error(`Failed to browse: ${err.message}`);
    }
}

function setFolderPath() {
    if (currentFolderBrowserPath) {
        $('local-path-input').value = currentFolderBrowserPath;
        closeFolderBrowser();
        toastMgr.success(`Selected: ${currentFolderBrowserPath}`);
        startLocalScan();
    }
}

async function startLocalScan() {
    const path = $('local-path-input').value.trim();
    if (!path) {
        toastMgr.error("Please enter a valid folder or file path.");
        return;
    }

    setTaBtnState($('btn-scan-local'), 'loading');
    $('local-results-wrap').classList.add('hidden');
    $('local-footer').classList.add('hidden');

    try {
        if (window.pywebview?.api && typeof window.pywebview.api.scan_local === 'function') {
            const result = await window.pywebview.api.scan_local(path);
            if (result && result.status === 'error') {
                throw new Error(result.error || 'Scan failed');
            }
        } else {
            const response = await fetch('/api/scan_local', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify([path]),
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                throw new Error(data?.error || 'Failed to start local scan');
            }
            if (data?.result?.status === 'error') {
                throw new Error(data.result.error || 'Scan failed');
            }
        }
        toastMgr.info("Scanning local files... this may take a moment.");
    } catch (err) {
        console.error('[LocalScan] start failed:', err);
        setTaBtnState($('btn-scan-local'), 'error');
        setTimeout(() => setTaBtnState($('btn-scan-local'), 'default'), 2500);
        toastMgr.error(err.message || 'Failed to start local scan');
    }
}

// Called by backend when scanning finishes
window.app_local_scan_results = function(payload) {
    setTaBtnState($('btn-scan-local'), 'default');
    localScanData = payload.files || [];
    renderLocalTracks();
    $('local-results-wrap').classList.remove('hidden');
    $('local-footer').classList.remove('hidden');
    updateLocalSelection();
    toastMgr.success(`Scan complete: found ${localScanData.length} files.`);
};

// Called by backend on scan error
window.app_local_scan_error = function(err) {
    setTaBtnState($('btn-scan-local'), 'error');
    setTimeout(() => setTaBtnState($('btn-scan-local'), 'default'), 2500);
    toastMgr.error("Scan failed: " + err);
};

function renderLocalTracks() {
    const container = $('local-track-rows');
    container.innerHTML = '';
    
    if (!localScanData.length) {
        container.innerHTML = '<div style="padding:40px 20px;text-align:center;color:var(--muted);font-size:14px;">No supported audio files found in this folder (FLAC, MP3, M4A/AAC, OGG, Opus, WAV, AIFF, WMA, WavPack, APE and more).</div>';
        return;
    }
    
    localScanData.forEach((item, i) => {
        const best = item.candidates && item.candidates[0];
        const hasMatch = !!best;
        const isSafe = hasMatch && best.is_safe;
        
        const oldCover = item.old_cover_base64 
            ? `<img src="${item.old_cover_base64}" alt="cover">` 
            : `🎵`;
            
        const fileName = item.file_path.split(/[\\/]/).pop();
        const oldTitle = item.old_title || item.guessed_title || fileName;
        const oldArtist = item.old_artist || item.guessed_artist || "Unknown Artist";
        
        let newCol = `<div style="color:var(--muted); font-size:12.5px; font-style:italic;">No match found</div>`;
        let scoreCol = `<span class="local-badge err">No Match</span>`;
        let checkbox = `<input type="checkbox" class="local-cb" value="${i}" data-file-path="${escHtml(item.file_path)}" disabled>`;

        if (hasMatch) {
            const newCover = best.metadata.cover_url || best.metadata.cover || '';
            const newCoverHtml = newCover ? `<img src="${newCover}">` : `🎵`;
            const newTitle = best.metadata.title || '';
            const newArtist = best.metadata.first_artist || '';

            // Diffing logic: mark as different if texts don't match (case insensitive)
            const hlTitle = oldTitle.toLowerCase() !== newTitle.toLowerCase() ? 'diff' : '';
            const hlArtist = oldArtist.toLowerCase() !== newArtist.toLowerCase() ? 'diff' : '';

            newCol = `
                <div class="local-cell-content">
                    <div class="local-thumb">${newCoverHtml}</div>
                    <div class="local-info">
                        <div class="local-title ${hlTitle}" title="New: ${escHtml(newTitle)}">${escHtml(newTitle)}</div>
                        <div class="local-artist ${hlArtist}">${escHtml(newArtist)}</div>
                    </div>
                </div>
            `;

            // An ISRC hit is an identity check, not a similarity score, so it
            // says so instead of showing a meaningless 100%. A row that scored
            // well but is held back — the title disagrees, or it looks like a
            // live/remix version with no duration to confirm it — explains why
            // it is not pre-ticked, rather than leaving the user to wonder.
            if (best.how === 'isrc') {
                scoreCol = `<span class="local-badge ok" title="Matched by ISRC — the file's own recording ID, not a guess">ISRC</span>`;
            } else {
                let why = 'Confidence score';
                if (!isSafe && best.confidence >= 90) {
                    if (!best.artist_known) {
                        why = 'This file has no artist — in tags or in its name — so only the title was compared. Check before applying';
                    } else if (best.variant_unconfirmed) {
                        why = 'Looks like a different version (live/remix/instrumental) and there is no duration to confirm it — check before applying';
                    } else {
                        why = 'The titles do not agree closely enough to apply this unattended — check before applying';
                    }
                }
                scoreCol = `<span class="local-badge ${isSafe ? 'ok' : 'warn'}" title="${escHtml(why)}">${best.confidence}%</span>`;
            }
            checkbox = `<input type="checkbox" class="local-cb" value="${i}" data-file-path="${escHtml(item.file_path)}" ${isSafe ? 'checked' : ''} onchange="updateLocalSelection()">`;
        } else if (item.error) {
            scoreCol = `<span class="local-badge err">Error</span>`;
            newCol = `<div style="color:var(--red); font-size:11px;">${escHtml(item.error)}</div>`;
        }

        const row = document.createElement('div');
        row.className = 'track-row local-row';
        row.style.gridTemplateColumns = "24px 1fr 80px 1fr";
        row.style.cursor = "default";
        
        row.innerHTML = `
            <div class="tr-check">${checkbox}</div>
            <div class="local-cell-content">
                <div class="local-thumb">${oldCover}</div>
                <div class="local-info">
                    <div class="local-title" title="Current: ${escHtml(oldTitle)}">${escHtml(oldTitle)}</div>
                    <div class="local-artist">${escHtml(oldArtist)}</div>
                    <div style="font-family:'JetBrains Mono', monospace; font-size:9.5px; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; margin-top:3px;" title="${escHtml(item.file_path)}">${escHtml(fileName)}</div>
                </div>
            </div>
            <div style="text-align:center;">${scoreCol}</div>
            ${newCol}
        `;
        container.appendChild(row);
    });
}

function toggleAllLocal(cb) {
    document.querySelectorAll('.local-cb:not([disabled])').forEach(c => c.checked = cb.checked);
    updateLocalSelection();
}

function updateLocalSelection() {
    const checked = document.querySelectorAll('.local-cb:checked').length;
    const total = document.querySelectorAll('.local-cb:not([disabled])').length;
    
    const checkAll = $('check-all-local');
    if (checkAll) {
        checkAll.checked = total > 0 && checked === total;
        checkAll.indeterminate = checked > 0 && checked < total;
    }
    
    $('local-selected-count').textContent = `${checked} file(s) selected`;
    $('btn-apply-local').disabled = checked === 0;
}

function applyLocalTags() {
    const selectedIdx = Array.from(document.querySelectorAll('.local-cb:checked')).map(cb => parseInt(cb.value));
    if (!selectedIdx.length) return;
    
    const itemsToApply = selectedIdx.map(i => {
        const entry = localScanData[i];
        return {
            file_path: entry.file_path,
            metadata: entry.candidates[0].metadata,
            backup: true // Always create .bak for safety
        };
    });
    
    setTaBtnState($('btn-apply-local'), 'loading');
    $('btn-apply-local').innerHTML = `Applying 0/${itemsToApply.length}...`;
    
    if (window.pywebview?.api) {
        window.pywebview.api.apply_local_tags(itemsToApply);
    } else {
        setTimeout(() => window.app_local_apply_finished({results: itemsToApply.map(i => ({success: true}))}), 2000);
    }
}

// Called per file during apply
window.app_local_apply_progress = function(payload) {
    const { done, total, last } = payload;
    $('btn-apply-local').innerHTML = `Applying ${done}/${total}...`;
    
    if (!last.success) {
        const name = last.file_path.split(/[\\/]/).pop();
        toastMgr.error(`Failed to tag ${name}: ${last.error}`);
    }
};

// Called when all applies are done
window.app_local_apply_finished = function(payload) {
    setTaBtnState($('btn-apply-local'), 'success');
    setTimeout(() => {
        setTaBtnState($('btn-apply-local'), 'default');
        $('btn-apply-local').innerHTML = `
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>
          Apply Selected Tags`;
    }, 2000);
    
    const successes = payload.results.filter(r => r.success).length;
    const errors = payload.results.length - successes;
    
    if (errors === 0) {
        toastMgr.success(`Done! ${successes} files successfully tagged and backed up.`);
    } else {
        toastMgr.warning(`Finished: ${successes} tagged, ${errors} failed.`);
    }
    
    // Automatically deselect checkboxes for successful ones so the user knows they are done
    payload.results.forEach((res) => {
        if (res.success && res.file_path) {
            const cb = document.querySelector(`.local-cb[data-file-path="${CSS.escape(res.file_path)}"]`);
            if (cb) {
                cb.checked = false;
                cb.disabled = true;
            }
        }
    });
    updateLocalSelection();
};

// Helper function to update artist separator field and row state
function updateArtistSeparatorState(firstArtistOnly) {
    const sepField = $('config-artist-separator');
    const row = $('config-artist-sep-row');
    const disabled = firstArtistOnly;

    if (sepField) {
        sepField.disabled = disabled;
    }
    if (row) {
        row.style.opacity = disabled ? '0.4' : '1';
        row.style.pointerEvents = disabled ? 'none' : 'auto';
    }
}

if ($('config-first-artist')) {
    $('config-first-artist').addEventListener('change', function() {
        updateArtistSeparatorState(this.checked);
    });
}
/* ──────────────────────────────────────────────────────────────────────────
   Following (subscriptions) — see core/subscriptions.py

   A subscription is a followed artist plus the set of releases already seen.
   The backend never downloads on its own: "Check for new" reports, and
   "Check & download" is the explicit second step.
   ────────────────────────────────────────────────────────────────────────── */

function subFormatDate(ts) {
  if (!ts) return 'never';
  try {
    return new Date(ts * 1000).toLocaleString();
  } catch (e) {
    return 'unknown';
  }
}

async function loadSubscriptions() {
  const list = $('subscription-list');
  if (!list) return;
  if (!window.pywebview?.api?.get_subscriptions) {
    list.innerHTML = '<div class="s-label" style="font-size:11.5px;">Following is unavailable in this build.</div>';
    return;
  }
  try {
    renderSubscriptions(await window.pywebview.api.get_subscriptions());
  } catch (e) {
    list.innerHTML = '<div class="s-label" style="font-size:11.5px;color:var(--red);">Unable to load subscriptions.</div>';
  }
}

function renderSubscriptions(subs) {
  const list = $('subscription-list');
  if (!list) return;

  if (subs?.error) {
    list.innerHTML = '<div class="s-label" style="font-size:11.5px;color:var(--red);">Failed to load: ' + regEscapeHtml(subs.error) + '</div>';
    return;
  }
  if (!subs || !subs.length) {
    list.innerHTML = emptyState(
      'user',
      'Not following anyone yet',
      'Paste an artist link above to be told when they release something new.',
    );
    return;
  }

  list.innerHTML = subs.map((s) => {
    const name = regEscapeHtml(s.name || s.url);
    const err = s.last_error
      ? `<div class="s-label" style="font-size:11px;color:var(--red);">Last check failed: ${regEscapeHtml(s.last_error)}</div>`
      : '';
    return `
      <div class="sort-item reg-item${s.enabled ? '' : ' reg-item-disabled'}">
        <div class="reg-item-main">
          <span class="reg-url" title="${regEscapeHtml(s.url)}">${name}</span>
          <div class="s-label" style="font-size:11px;">
            ${regEscapeHtml(s.include_groups)} · ${s.seen_count} release(s) seen · checked ${regEscapeHtml(subFormatDate(s.last_checked_at))}
          </div>
          ${err}
        </div>
        <button class="act-btn secondary reg-remove-btn" type="button"
                onclick="toggleSubscription('${regEscapeHtml(s.id)}', ${s.enabled ? 'false' : 'true'})"
                title="${s.enabled ? 'Stop checking this artist without forgetting what has been seen' : 'Resume checking this artist'}">
          ${s.enabled ? 'Pause' : 'Resume'}
        </button>
        <button class="act-btn secondary reg-remove-btn" type="button"
                onclick="resetSubscription('${regEscapeHtml(s.id)}')"
                title="Forget what this subscription has seen, so the whole back catalogue counts as new again">
          Reset
        </button>
        <button class="act-btn secondary reg-remove-btn" type="button"
                onclick="removeSubscription('${regEscapeHtml(s.id)}')">
          Unfollow
        </button>
      </div>`;
  }).join('');
}

async function addSubscription() {
  const input = $('subscription-url-input');
  const url = (input?.value || '').trim();
  if (!url) {
    showToast('Paste an artist link first.', 'error');
    return;
  }
  try {
    const res = await window.pywebview.api.add_subscription(
      url, '', $('subscription-groups')?.value || 'album,single', ''
    );
    if (res?.ok) {
      input.value = '';
      showToast('Now following ' + (res.subscription?.name || 'that artist') + '.');
      loadSubscriptions();
    } else {
      showToast(res?.error || 'Could not follow that link.', 'error');
    }
  } catch (e) {
    showToast('Could not follow that link.', 'error');
  }
}

async function removeSubscription(id) {
  try {
    await window.pywebview.api.remove_subscription(id);
    loadSubscriptions();
  } catch (e) {
    showToast('Could not unfollow.', 'error');
  }
}

async function toggleSubscription(id, enabled) {
  try {
    await window.pywebview.api.set_subscription_enabled(id, enabled);
    loadSubscriptions();
  } catch (e) {
    showToast('Could not update that subscription.', 'error');
  }
}

async function resetSubscription(id) {
  if (!confirm('Forget what this subscription has seen? The next check with downloading on will treat the whole back catalogue as new.')) return;
  try {
    await window.pywebview.api.reset_subscription(id);
    showToast('Reset. The next check treats every release as new.');
    loadSubscriptions();
  } catch (e) {
    showToast('Could not reset that subscription.', 'error');
  }
}

async function checkSubscriptions(download) {
  const results = $('subscription-results');
  if (results) {
    results.innerHTML = '<div class="s-label" style="font-size:11.5px;">Checking…</div>';
    $('subscription-results-section').style.display = '';
  }
  try {
    await window.pywebview.api.check_subscriptions(!!download);
  } catch (e) {
    showToast('Could not start the check.', 'error');
  }
}

/* Pushed by SubscriptionsMixin._check_subscriptions_thread when it finishes. */
function subscriptionsChecked(payload) {
  const results = $('subscription-results');
  const section = $('subscription-results-section');
  if (!results || !section) return;
  section.style.display = '';

  if (payload?.error) {
    results.innerHTML = '<div class="s-label" style="font-size:11.5px;color:var(--red);">' + regEscapeHtml(payload.error) + '</div>';
    return;
  }

  const rows = payload?.results || [];
  if (!rows.length) {
    results.innerHTML = '<div class="s-label" style="font-size:11.5px;">Nothing is being followed yet.</div>';
    return;
  }

  results.innerHTML = rows.map((r) => {
    const artist = regEscapeHtml(r.artist || r.url);
    if (r.error) {
      return `<div class="sort-item reg-item"><div class="reg-item-main">
        <span class="reg-url">${artist}</span>
        <div class="s-label" style="font-size:11px;color:var(--red);">${regEscapeHtml(r.error)}</div>
      </div></div>`;
    }
    if (r.watermarked) {
      return `<div class="sort-item reg-item"><div class="reg-item-main">
        <span class="reg-url">${artist}</span>
        <div class="s-label" style="font-size:11px;">First check — ${r.total_releases} release(s) recorded as seen. Only later releases will be fetched.</div>
      </div></div>`;
    }
    const releases = (r.new_releases || []).map((rel) =>
      `<div class="s-label" style="font-size:11px;">· ${regEscapeHtml(rel.title)}${rel.year ? ' (' + regEscapeHtml(rel.year) + ')' : ''} [${regEscapeHtml(rel.type)}]</div>`
    ).join('');
    return `<div class="sort-item reg-item"><div class="reg-item-main">
      <span class="reg-url">${artist}</span>
      <div class="s-label" style="font-size:11px;">${(r.new_releases || []).length} new release(s)</div>
      ${releases}
    </div></div>`;
  }).join('');

  loadSubscriptions();
}

/* ──────────────────────────────────────────────────────────────────────────
   Extension health — see core/provider_stats.py and api_mixins/extension_health.py
   ────────────────────────────────────────────────────────────────────────── */

function healthRate(rate) {
  return rate === null || rate === undefined ? '—' : Math.round(rate * 100) + '%';
}

function healthColour(row) {
  if (!row.attempts) return 'var(--muted)';
  if (row.success_rate >= 0.9) return 'var(--green, #1ed760)';
  if (row.success_rate >= 0.5) return 'var(--yellow, #f0c674)';
  return 'var(--red)';
}

async function loadExtensionHealth() {
  const list = $('ext-health-list');
  if (!list) return;
  if (!window.pywebview?.api?.get_extension_health) {
    list.innerHTML = '<div class="s-label" style="font-size:11.5px;">Extension health is unavailable in this build.</div>';
    return;
  }
  try {
    renderExtensionHealth(await window.pywebview.api.get_extension_health());
  } catch (e) {
    list.innerHTML = '<div class="s-label" style="font-size:11.5px;color:var(--red);">Unable to load extension health.</div>';
  }
}

function renderExtensionHealth(data) {
  const list = $('ext-health-list');
  if (!list) return;

  if (data?.error) {
    list.innerHTML = '<div class="s-label" style="font-size:11.5px;color:var(--red);">' + regEscapeHtml(data.error) + '</div>';
    return;
  }
  const rows = data?.providers || [];
  if (!rows.length) {
    list.innerHTML = '<div class="s-label" style="font-size:11.5px;">No extensions installed, and nothing has been downloaded yet.</div>';
    return;
  }

  list.innerHTML = rows.map((r) => {
    const version = r.version ? ' · v' + regEscapeHtml(r.version) : '';
    const detail = r.attempts
      ? `${r.successes}/${r.attempts} succeeded · ~${r.avg_duration_s}s each`
      : 'never tried';
    const err = r.last_error
      ? `<div class="s-label" style="font-size:11px;color:var(--red);">Last error: ${regEscapeHtml(r.last_error)}</div>`
      : '';
    return `
      <div class="sort-item reg-item">
        <div class="reg-item-main">
          <span class="reg-url">${regEscapeHtml(r.provider)}${version}</span>
          <div class="s-label" style="font-size:11px;">${regEscapeHtml(detail)}</div>
          ${err}
        </div>
        <span style="font-weight:600;font-size:13px;color:${healthColour(r)};">${healthRate(r.success_rate)}</span>
      </div>`;
  }).join('');
}

async function resetExtensionHealth() {
  if (!confirm('Clear the recorded provider statistics?')) return;
  try {
    await window.pywebview.api.reset_extension_health();
    showToast('Extension statistics cleared.');
    loadExtensionHealth();
  } catch (e) {
    showToast('Could not clear the statistics.', 'error');
  }
}

// ── Your library, in numbers (core/stats.py) ──────────────────────────────
//
// The download log read back as a dashboard. Everything here is derived from
// downloads this install actually made, so the view is honest about what it
// cannot know: genre, release year and duration are only recorded for
// downloads made since the feature landed, and each section reports its own
// coverage rather than presenting a fifth of the library as all of it.

function statsPeriodArgs() {
  const value = $('stats-period')?.value || 'all';
  if (value === 'year') return [new Date().getFullYear(), null];
  if (value === '365') return [null, 365];
  if (value === '30') return [null, 30];
  return [null, null];
}

function statsBytes(value) {
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let size = Number(value) || 0;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return (unit === 0 ? size.toFixed(0) : size.toFixed(1)) + ' ' + units[unit];
}

function statsDuration(ms) {
  const seconds = Math.floor((Number(ms) || 0) / 1000);
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function statsTile(label, value, hint) {
  return `
    <div class="stat-tile">
      <div class="stat-tile-value">${regEscapeHtml(value)}</div>
      <div class="stat-tile-label">${regEscapeHtml(label)}</div>
      ${hint ? `<div class="stat-tile-hint">${regEscapeHtml(hint)}</div>` : ''}
    </div>`;
}

// One ranking as labelled bars. `rows` is [{label, value, caption}], drawn
// relative to the largest value so the shape of the list is readable at a
// glance rather than needing the numbers to be compared by eye.
function statsBars(rows, formatValue) {
  if (!rows.length) return '<div class="s-label" style="font-size:11.5px;">Nothing here yet.</div>';
  const peak = Math.max(...rows.map((row) => row.value)) || 1;
  return rows.map((row) => `
    <div class="stat-bar-row">
      <div class="stat-bar-label" title="${regEscapeHtml(row.label)}">${regEscapeHtml(row.label)}</div>
      <div class="stat-bar-track"><div class="stat-bar-fill" style="width:${Math.max(2, (row.value / peak) * 100)}%"></div></div>
      <div class="stat-bar-value">${regEscapeHtml((formatValue || String)(row.value))}</div>
    </div>`).join('');
}

function statsSection(title, inner, note) {
  return `
    <div class="s-section">
      <div class="s-title">${regEscapeHtml(title)}</div>
      ${note ? `<div class="s-label" style="font-size:11px;margin:-2px 0 10px;">${regEscapeHtml(note)}</div>` : ''}
      ${inner}
    </div>`;
}

async function loadStats() {
  const body = $('stats-body');
  if (!body) return;
  if (!window.pywebview?.api?.get_stats) {
    body.innerHTML = '<div class="s-label" style="font-size:11.5px;">Statistics are unavailable in this build.</div>';
    return;
  }
  body.innerHTML = '<div class="s-label" style="font-size:11.5px;">Loading…</div>';
  try {
    const [year, days] = statsPeriodArgs();
    renderStats(await window.pywebview.api.get_stats(year, days, 10));
  } catch (e) {
    body.innerHTML = '<div class="s-label" style="font-size:11.5px;color:var(--red);">Could not read the download log.</div>';
  }
}

function renderStats(doc) {
  const body = $('stats-body');
  if (!body) return;
  if (!doc || doc.error) {
    body.innerHTML = `<div class="s-label" style="font-size:11.5px;color:var(--red);">${regEscapeHtml(doc?.error || 'No data.')}</div>`;
    return;
  }

  const totals = doc.totals || {};
  if (!totals.tracks) {
    body.innerHTML = `<div class="s-section">${emptyState(
      'chart',
      'Nothing to count yet',
      'This is built from the download log, which fills up as you fetch tracks. '
      + 'Come back after a download or two.',
    )}</div>`;
    return;
  }

  const sections = [];

  sections.push(`
    <div class="stat-tiles">
      ${statsTile('tracks', totals.tracks)}
      ${statsTile('artists', totals.artists)}
      ${statsTile('albums', totals.albums)}
      ${statsTile('on disk', statsBytes(totals.bytes))}
      ${totals.listening_known
        ? statsTile('of music', statsDuration(totals.listening_ms),
            totals.listening_known === totals.tracks ? '' : `timed for ${totals.listening_known} of them`)
        : ''}
      ${totals.failed ? statsTile('failed', totals.failed, `${Math.round((totals.success_rate || 0) * 100)}% succeeded`) : ''}
    </div>`);

  sections.push(statsSection('Top artists', statsBars(
    (doc.top_artists || []).map((e) => ({ label: e.name, value: e.tracks })),
  )));

  const albums = (doc.top_albums || []).map((e) => ({
    label: e.artist ? `${e.name} — ${e.artist}` : e.name,
    value: e.tracks,
  }));
  sections.push(statsSection('Top albums', statsBars(albums)));

  const genres = doc.top_genres || { entries: [] };
  sections.push(statsSection(
    'Top genres',
    statsBars((genres.entries || []).map((e) => ({ label: e.name, value: e.tracks }))),
    genres.unknown
      ? `Known for ${genres.known} of ${totals.tracks} tracks — a genre needs metadata enrichment to have been on when the track was downloaded.`
      : '',
  ));

  const decades = doc.decades || { entries: [] };
  if ((decades.entries || []).length) {
    sections.push(statsSection(
      'By decade',
      statsBars(decades.entries.map((e) => ({ label: e.name, value: e.tracks }))),
      decades.unknown ? `Release year known for ${decades.known} of ${totals.tracks} tracks.` : '',
    ));
  }

  const repeats = doc.top_tracks || [];
  if (repeats.length) {
    sections.push(statsSection(
      'Fetched more than once',
      statsBars(repeats.map((e) => ({ label: `${e.name} — ${e.artist}`, value: e.tracks }))),
      'A re-download after a quality upgrade, or the same song from two playlists.',
    ));
  }

  sections.push(statsSection('Providers', statsBars(
    (doc.providers || []).map((e) => ({ label: e.name, value: e.tracks })),
  )));

  const formats = doc.formats || [];
  if (formats.length) {
    sections.push(statsSection('Formats', statsBars(
      formats.map((e) => ({ label: (e.name || '').toUpperCase(), value: e.tracks })),
    )));
  }

  const timeline = (doc.timeline || []).slice(-12);
  if (timeline.length) {
    sections.push(statsSection('Last months', statsBars(
      timeline.map((e) => ({ label: e.month, value: e.tracks })),
    )));
  }

  const activity = doc.activity || {};
  const weekdays = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
  if ((activity.by_weekday || []).some((n) => n)) {
    sections.push(statsSection('By weekday', statsBars(
      activity.by_weekday.map((count, index) => ({ label: weekdays[index], value: count })),
    )));
  }

  const facts = [];
  if (activity.busiest_day) {
    facts.push(`Busiest day: ${activity.busiest_day.date} (${activity.busiest_day.tracks} tracks)`);
  }
  if (activity.active_days) {
    facts.push(`Downloaded on ${activity.active_days} day(s) · longest streak ${activity.longest_streak}`);
  }
  if (doc.first) {
    const when = new Date((doc.first.downloaded_at || 0) * 1000).toLocaleDateString();
    facts.push(`First in this period: ${doc.first.title} — ${doc.first.artist} (${when})`);
  }
  if (facts.length) {
    sections.push(statsSection(
      'Highlights',
      facts.map((fact) => `<div class="s-label" style="font-size:12px;line-height:1.9;">${regEscapeHtml(fact)}</div>`).join(''),
    ));
  }

  body.innerHTML = sections.join('');
}

// ── CSV import (core/csv_source.py) ───────────────────────────────────────
//
// The file is read here, in the browser, and only its text crosses to Python
// — so this works identically in the desktop window and over `--web`, and a
// server never has to be able to see the user's disk.

// Blob.text() everywhere it exists, FileReader where it doesn't — the
// desktop window runs whatever webview the operating system ships, which on
// an older install predates it.
function readFileAsText(file) {
  if (typeof file.text === 'function') return file.text();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(reader.error || new Error('read failed'));
    reader.readAsText(file);
  });
}

function openCsvPicker() {
  $('csvFileInput')?.click();
}

async function onCsvFileChosen(input) {
  const file = input?.files?.[0];
  // Cleared straight away so choosing the same file twice in a row still
  // fires a change event.
  if (input) input.value = '';
  if (!file) return;

  if (file.size > 2000000) {
    showToast('That file is too large to import (2 MB max).', 'error');
    return;
  }
  if (!window.pywebview?.api?.fetch_csv) {
    showToast('CSV import is unavailable in this build.', 'error');
    return;
  }

  let text;
  try {
    text = await readFileAsText(file);
  } catch (e) {
    showToast('Could not read that file.', 'error');
    return;
  }

  try {
    const started = await window.pywebview.api.fetch_csv(text, file.name);
    if (started?.status === 'error') {
      showToast(started.error || 'Could not read that file.', 'error');
      return;
    }
    showToast(`Reading ${file.name}… rows without a link are matched against the catalogue.`, 'info');
  } catch (e) {
    showToast(e.message || 'CSV import failed.', 'error');
  }
}

// Pushed by api_mixins/csv_import.py through both long phases of an import:
// matching the rows against the catalogue, then fetching the metadata of
// every link that matched. A large file is minutes of work in each, and
// without this the import said nothing between "Reading the file…" and the
// finished track list — a file whose columns were mapped wrong looked
// exactly like one that was working, and a stalled fetch like a slow one.
// Three numbers, because they answer different questions: how far along it
// is, how much of it is coming back, and how much is being lost.
window.app_csv_progress = function (payload) {
  const { phase = 'matching', done = 0, total = 0, found = 0, missing = 0 } = payload || {};
  const matching = phase === 'matching';
  const label =
    `${matching ? 'Matching' : 'Fetching metadata'} ${done}/${total} · ` +
    `${found} ${matching ? 'found' : 'ready'}` +
    (missing ? ` · ${missing} ${matching ? 'not found' : 'without metadata'}` : '');
  // The line itself is already on screen: the same counter arrives as an
  // app_set_progress label. This puts it on the button that started the
  // import too, since that is where the pointer is, and marks the button
  // busy — without touching its innerHTML, which is the icon.
  const btn = $('csvBtn');
  if (!btn) return;
  btn.title = label;
  btn.setAttribute('aria-label', label);
  // Only the metadata phase reaching its total ends the import; matching
  // hits done === total and is immediately followed by the second phase.
  btn.classList.toggle('is-busy', matching || done < total);
};

// Pushed by api_mixins/csv_import.py once the track list is ready. The table
// itself is filled by the ordinary showTracklist event, so this only reports
// on the rows that did not make it.
window.app_csv_loaded = function (payload) {
  const missed = (payload?.unresolved || []).length;
  const failed = payload?.failed || 0;
  const btn = $('csvBtn');
  if (btn) btn.classList.remove('is-busy');
  if (missed || failed) {
    const parts = [];
    if (missed) parts.push(`${missed} row(s) could not be matched`);
    // A row can match a link whose metadata then fails to fetch, which is
    // why this is a separate number from the unmatched rows: together they
    // account for the gap between the file's line count and the table's.
    if (failed) parts.push(`${failed} link(s) returned no metadata`);
    showToast(`${payload.tracks} track(s) loaded · ${parts.join(' · ')} (see the log).`, 'info');
  } else {
    showToast(`${payload.tracks} track(s) loaded from ${payload.file}.`, 'success');
  }
};

window.app_csv_error = function (payload) {
  const btn = $('csvBtn');
  if (btn) btn.classList.remove('is-busy');
  showToast(payload?.error || 'CSV import failed.', 'error');
};
