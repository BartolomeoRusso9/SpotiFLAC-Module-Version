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
const ts = () => new Date().toLocaleTimeString('it-IT');
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
  }
}

// ── Appearance ───────────────────────────────────────────────────────────────
function applyTheme(mode) {
  if (mode === 'light') {
    document.body.classList.remove('dark-theme');
    document.body.classList.add('light-theme');
  } else if (mode === 'dark') {
    document.body.classList.remove('light-theme');
    document.body.classList.add('dark-theme');
  } else {
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    if (prefersDark) {
      document.body.classList.remove('light-theme');
      document.body.classList.add('dark-theme');
    } else {
      document.body.classList.remove('dark-theme');
      document.body.classList.add('light-theme');
    }
  }
}

function changeTheme() {
  const val = $('config-theme').value;
  applyTheme(val);
  try { localStorage.setItem('spotiflac-theme-mode', val); } catch (e) {}
}

function syncSystemTheme(e) {
  const val = $('config-theme')?.value || 'auto';
  if (val === 'auto') applyTheme('auto');
}

function loadThemeFromStorage() {
  const stored = (() => {
    try { return localStorage.getItem('spotiflac-theme-mode'); } catch (e) { return null; }
  })() || 'auto';
  if ($('config-theme')) $('config-theme').value = stored;
  applyTheme(stored);
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
  if ($('config-theme')) $('config-theme').value = cfg.theme;
  if ($('config-font')) $('config-font').value = cfg.font;
  changeFont();
  changeTheme();
  if ($('config-lyrics')) { $('config-lyrics').checked = cfg.lyrics; onLyricsChange(); }
  if ($('config-enrich')) { $('config-enrich').checked = cfg.enrich_metadata; onEnrichChange(); }
  if ($('config-filename')) $('config-filename').value = cfg.filename_format;
  if ($('config-track-numbers')) { $('config-track-numbers').checked = cfg.use_track_numbers; onTNChange(); }
  if ($('config-album-track-numbers')) $('config-album-track-numbers').checked = cfg.use_album_track_numbers;
  if ($('config-artist-sub')) $('config-artist-sub').checked = cfg.use_artist_subfolders;
  if ($('config-album-sub')) $('config-album-sub').checked = cfg.use_album_subfolders;
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
  if ($('config-loop')) $('config-loop').value = cfg.loop;
  if ($('config-loglevel')) $('config-loglevel').value = cfg.log_level;
  applyListState('services-list', cfg.services);
  applyListState('lyrics-list', cfg.lyrics_providers);
  applyListState('enrich-list', cfg.enrich_providers);
  updateAllApiConfigDisplays();
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
    else loadThemeFromStorage();
  } catch(e) {
    loadThemeFromStorage();
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
    cfg.theme = $('config-theme')?.value || DEFAULT_SETTINGS.theme;
    cfg.font  = $('config-font')?.value  || DEFAULT_SETTINGS.font;
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
function onTranscodeChange() {
  const on = $('config-transcode') && $('config-transcode').value !== 'none';
  document.querySelectorAll('.transcode-opt').forEach(row => {
    row.style.display = on ? 'flex' : 'none';
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
  loop: 0,
  log_level: 'INFO',
  services: ['tidal','qobuz','deezer','amazon','joox','netease','migu','kuwo','apple','soundcloud','youtube','pandora'],
  lyrics_providers: ['apple', 'lrclib'],
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
  const iconFile = iconMap[type] || `${type}.svg`;
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
let previewAudio = null;
let previewPlayingIndex = -1;
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
  // Write to the log UI panel
  const area = $('logArea');
  if (area) {
    const line = document.createElement('div');
    line.className = 'log-line';
    line.innerHTML = `<span class="log-ts">${ts()}</span><span class="log-msg ${type}">${escHtml(msg)}</span>`;
    area.appendChild(line);
    area.scrollTop = area.scrollHeight;
  }

  // Also generate a visual Toast based on the event type!
  if (type === 'ok') toastMgr.success(msg);
  else if (type === 'error') toastMgr.error(msg);
  else if (type === 'warn') toastMgr.warning(msg);
  // If there is no type or it is routine info ("info"), show info only when relevant
  else if (type === 'info') toastMgr.info(msg, { duration: 2500 });
}

function clearLog() { $('logArea').innerHTML = ''; }

window.app_log = (msg, type = '') => logMessage(msg, type);
window.app_set_progress = (label) => { if (label) setStatus(label); };
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
      d.track_count
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
        if (Array.isArray(data.queue)) {
          data.queue.forEach(stat => {
            let qi = queue.findIndex(q => q.id && stat.id && q.id === stat.id);
            if (qi < 0 && stat.spotify_id) {
              qi = queue.findIndex(q => q.spotify_id && q.spotify_id === stat.spotify_id);
            }
            if (qi < 0 && stat.track_name) {
              qi = queue.findIndex(q => q.title === stat.track_name && q.artist === stat.artist_name);
            }
            if (qi < 0) return;
            const item = queue[qi];
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
  if (statusText) statusText.textContent = msg;
  const spinner = $('spinner');
  if (spinner) spinner.style.display = loading ? 'block' : 'none';
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

function setAlbumCard(title, artist, coverUrl, quality, description, followers, owner, ownerAvatar, source, artistListeners, artistRank, artistVerified, artistBiography, releaseDate, trackCount) {
  g_albumReleaseDate = releaseDate || '';
  g_albumTrackCount = trackCount || 0;
  
  const metaSection = $('track-meta-section');
  if (metaSection) {
    metaSection.innerHTML = '';
    metaSection.style.display = 'none';
  }
  $('album-cover').querySelector('.cover-duration')?.remove();
  $('album-subtitle').style.display = '';

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
  $('album-artist').textContent  = artist || '';
  const subtitle = $('album-subtitle');
  
  // For artists, show rank or listeners; for playlists, show quality
  const isArtistCard = !!(artistRank || artistListeners || artistVerified || artistBiography);

  if (isArtistCard) {
    const bio = artistBiography || description || '';
    subtitle.innerHTML = bio;
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
    if (followers)       parts.push(`${Number(followers).toLocaleString('it-IT')} followers`);
    if (artistListeners) parts.push(`${Number(artistListeners).toLocaleString('it-IT')} listeners`);
    
    artistStatsRow.innerHTML = parts.map(p => `<span>${escHtml(p)}</span>`).join('<span class="dot-sep"> · </span>');
    artistStatsRow.style.display = 'flex';
    if (ownerRow) ownerRow.style.display = 'none'; // Hide the original row while keeping it intact
    
    metaDetails.classList.remove('hidden');
    if (avatarEl) avatarEl.classList.add('hidden');
  } else {
    artistStatsRow.style.display = 'none';
    if (ownerRow) ownerRow.style.display = 'flex';
    
    if (ownerEl) ownerEl.textContent = owner || '';
    const followerCount = Number(followers);
    if (followersEl) followersEl.textContent = !Number.isNaN(followerCount) ? `${followerCount.toLocaleString()} followers` : '';
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
  const artistEl = $('album-artist');
  if (owner) {
    artistEl.textContent = "";
  } else {
    artistEl.textContent = artist || '';
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
  if (coverUrl) {
    const displayArtist = artist || title || 'Unknown';
    coverEl.innerHTML = `<img src="${coverUrl}" alt="cover" onerror="this.parentElement.innerHTML='🎵'">
    <button id="cover-download-btn" class="cover-download-btn" onclick="downloadAlbumCover(this, '${coverUrl}', '${escHtml(title || 'album')}', '${escHtml(displayArtist)}', '${escHtml(owner || '')}')" title="Download cover" style="left: 50%; top: 50%; transform: translate(-50%, -50%);">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
    </button>`;
  } else {
    coverEl.innerHTML = '🎵';
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
    
    let subtitleParts = [];
    if (artist) subtitleParts.push(artist);
    if (g_albumReleaseDate) {
      const dateStr = String(g_albumReleaseDate).split('T')[0];
      if (dateStr) subtitleParts.push(dateStr);
    }
    if (trackCount > 0) {
      subtitleParts.push(`${trackCount} track${trackCount !== 1 ? 's' : ''}`);
    }
    
    const subtitleText = subtitleParts.join(' · ');
    subtitleEl.textContent = subtitleText;
    subtitleEl.style.display = subtitleText ? '' : 'none';
  }

  
  
  const artistEl = $('album-artist');
  const hasArtist = Boolean(artistEl.textContent && artistEl.textContent.trim());
  $('album-meta').classList.toggle('no-artist', !hasArtist);
  artistEl.style.display = hasArtist ? '' : 'none';
  const trackCountEl = $('album-tracks-count');
  if (trackCountEl) {
    trackCountEl.textContent = `${trackCount} track${trackCount !== 1 ? 's' : ''}`;
  }
  $('album-meta').style.display = '';
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

  // Hide the subtitle (quality) — already shown elsewhere
  $('album-subtitle').style.display = 'none';

  // Populate the meta grid
  const section = $('track-meta-section');
  const playcountRaw = t.plays ?? t.playcount ?? t.playCount ?? t.plays_count;
  const playcountVal = playcountRaw != null
    ? Number(playcountRaw).toLocaleString('it-IT')
    : null;

  const metas = [
    { label: 'Album',        value: t.album || t.album_name || t.release || null },
    { label: 'Release Date', value: t.release_date ? String(t.release_date).split('T')[0] : (t.year || null) },
    { label: 'Total Plays',  value: playcountVal },
    { label: 'Copyright',    value: t.copyright || null },
  ].filter(m => m.value);

  if (metas.length) {
    const grid = document.createElement('div');
    grid.className = 'track-meta-grid';
    metas.forEach(m => {
      const item = document.createElement('div');
      item.className = 'track-meta-item';
      item.innerHTML = `
        <div class="track-meta-label">${escHtml(m.label)}</div>
        <div class="track-meta-value" title="${escHtml(String(m.value))}">${escHtml(String(m.value))}</div>
      `;
      grid.appendChild(item);
    });
    section.innerHTML = '';
    section.appendChild(grid);
    section.style.display = '';
  } else {
    section.style.display = 'none';
  }

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

function formatDuration(ms) {
  if (!ms) return '—';
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60); const sec = s % 60;
  return `${m}:${sec.toString().padStart(2, '0')}`;
}

function injectArtistTabs(tracks) {
  document.getElementById('artist-tabs-section')?.remove();

  // Raggruppa per album
  const albumMap = new Map();
  tracks.forEach((t, idx) => {
    const key = t.album || t.album_name || t.release || '—';
    if (!albumMap.has(key)) {
      albumMap.set(key, {
        name: key,
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
      <div class="aac-cover">${coverHtml}</div>
      <div class="aac-body">
        <div class="aac-name" title="${escHtml(album.name)}">${escHtml(album.name)}</div>
        <div class="aac-meta">${album.year ? album.year + ' · ' : ''}${album.indices.length} track${album.indices.length !== 1 ? 's' : ''}</div>
      </div>`;
    card.onclick = () => { addToQueue(album.indices); startDownloadQueue(); $('queue-drawer').classList.add('open'); };
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
            <div class="tr-artist">${escHtml(t.artists || t.artist || '')}</div>
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
    const label = $('recent-wrap').querySelector('.recent-label');
    if (label) label.textContent = 'RECENT SEARCHES';
    
    searches.forEach(q => {
        const card = document.createElement('div');
        card.className = 'recent-card';
        card.style.padding = '12px 14px';
        card.style.display = 'flex';
        card.style.alignItems = 'center';
        card.style.gap = '10px';
        card.innerHTML = `<span style="font-size:16px;">🔎</span><span class="rc-title" style="font-size:13px; color:var(--text);">${escHtml(q)}</span>`;
        card.onclick = () => {
            $('urlInput').value = q;
            $('urlInput').dispatchEvent(new Event('input'));
        };
        grid.appendChild(card);
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
    const icon = $('searchModeIcon');
    const label = $('searchModeText');
    const fetchBtn = $('fetchBtn');

    if (mode.value === 'link') {
        mode.value = 'search';
        toggle.classList.add('active');
        icon.textContent = '🔎';
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
        icon.textContent = '🔗';
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
  const icon = $('searchModeIcon');
  const label = $('searchModeText');
  
  if (mode === 'search') {
    // Text mode: stop the animation and set the fixed text
    clearTimeout(phTimeout);
    input.placeholder = 'Search Spotify with keywords, artist or track name…';
    toggle.classList.add('active');
    icon.textContent = '🔎';
    label.textContent = 'Search';
    toggle.title = 'Switch to Fetch Mode';
    $('track-table-wrap')?.classList.add('hidden');
    $('track-controls')?.classList.add('hidden');
    $('album-card')?.classList.add('hidden');
  } else {
    // Link mode: reset and restart the animation
    toggle.classList.remove('active');
    icon.textContent = '🔗';
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

function renderRecent(hist) {
  const grid = $('recent-grid'); grid.innerHTML = '';
  if (!hist || !hist.length) {
    grid.innerHTML = '<div style="grid-column:1/-1;font-size:12px;color:var(--muted);padding:10px 0;">No recent fetches yet.</div>';
    return;
  }
  const BADGE_CFG = {
    playlist: { label:'Playlist', color:'#a855f7', bg:'rgba(168,85,247,.15)', icon:'☰' },
    artist:   { label:'Artist',  color:'#f97316', bg:'rgba(249,115,22,.15)',  icon:'♪' },
    album:    { label:'Album',   color:'#22c55e', bg:'rgba(34,197,94,.15)',   icon:'◎' },
    track:    { label:'Track',   color:'#3b82f6', bg:'rgba(59,130,246,.15)',  icon:'♩' },
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
      ? `<span class="rc-badge" style="color:${badge.color};background:${badge.bg};">${badge.icon} ${badge.label}</span>`
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
  indices.forEach(i => {
      const t = currentTracks[i];
      if (!t) {
        console.warn('Skipped invalid track index', i);
        return;
      }

      // Usa l'indice originale per evitare che Python scarichi la track sbagliata
      const realIndex = t._originalIndex !== undefined ? t._originalIndex : i;

      if (queue.find(q => q.index === realIndex)) {
        console.warn('Track already in queue', realIndex);
        return;
      }
      const itemId = t.id || t.external_url || `queue-${realIndex}-${Math.random().toString(16).slice(2)}`;
      const spotifyId = t.id || t.external_url || itemId;
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

function renderQueue() {
  const list = $('queue-list'); list.innerHTML = '';
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

  const dock = $('queue-dock');
  if (queue.length === 0) {
    queueStats = { downloaded:'0.00 MB', speed:'0.00 MB/s' };
    if (dock) dock.classList.remove('visible');
    $('queue-drawer')?.classList.remove('open');

    empty.style.display = 'flex';
    list.appendChild(empty);
    $('q-count').textContent = '0 tracks'; $('q-done').textContent = '';
    const downloaded = $('qd-downloaded');
    const speed = $('qd-speed');
    if (downloaded) downloaded.textContent = queueStats.downloaded;
    if (speed) speed.textContent = queueStats.speed;
    resetQueueDuration();
    return;
  }

  empty.style.display = 'none';
  if (dock) dock.classList.add('visible');
  queue.forEach((item, qi) => {
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
    const pillClass = `qi-pill ${item.status}`;

    // Define the bottom section HTML (size and path for completed tracks, status text for others)
    let bottomHtml;
    if (item.status === 'done') {
      const sizeHtml = item.file_size_mb > 0
        ? `<span>${item.file_size_mb.toFixed(2)} MB</span>`
        : '';
      const pathHtml = item.file_path
        ? `<span class="qi-bm-path" title="${escHtml(item.file_path)}">${escHtml(item.file_path)}</span>`
        : '';
      bottomHtml = (sizeHtml || pathHtml) ? `<div class="qi-bottom-meta">${sizeHtml}${pathHtml}</div>` : '';
    } else {
      bottomHtml = `<div class="qi-bottom">${statusText}</div>`;
    }

    const el = document.createElement('div');
    el.className = 'queue-item'; el.id = `qi-${qi}`;
    
    // Combine artist and album with a middle dot (•) if the album metadata exists
    const artistAlbumText = item.album
      ? `${escHtml(item.artist)} • ${escHtml(item.album)}`
      : escHtml(item.artist);
    
    el.innerHTML = `
      <div class="qi-top">
        <div class="qi-meta">
          <div class="qi-title">${escHtml(item.title)}</div>
          <div class="qi-artist">${artistAlbumText}</div>
        </div>
        <div class="${pillClass}">${statusLabel}</div>
      </div>
      ${bottomHtml}
    `;
    list.appendChild(el);
  });
  
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
  if (speed) speed.textContent = queueStats.speed;
  
  updateQueueDuration();
}

function toggleQueueDrawer() {
  const drawer = $('queue-drawer');
  if (!drawer) return;
  drawer.classList.toggle('open');
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
    
    currentFetchToastId = toastMgr.loading(
      `<div class="ft-desc loading" style="font-size:12px; margin-top:2px; color:var(--text2);">please wait...</div>`, 
      { title: title, position: 'bottom-left' } // Lo teniamo a sinistra come l'originale
    );
  } 
  else if (state === 'success') {
    if (currentFetchToastId) toastMgr.dismiss(currentFetchToastId);
    toastMgr.success('success', { title: 'Completato', position: 'bottom-left', duration: 2500 });
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
    logMessage(`Text search: ${url}`, 'info');
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
  logMessage(`Fetching: ${url}`, 'info');
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
    enrich_metadata:        $('config-enrich').checked,
    enrich_providers:       getChecked('enrich-list'),
    filename_format:         $('config-filename').value.trim() || '{title} - {artist}',
    use_track_numbers:      $('config-track-numbers').checked,
    use_album_track_numbers:$('config-album-track-numbers').checked,
    use_artist_subfolders:  $('config-artist-sub').checked,
    use_album_subfolders:   $('config-album-sub').checked,
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
        if (greetingEl) greetingEl.textContent = homeData.greeting || 'Esplora';
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
  logMessage('Python backend connected.', 'ok');
  loadHistoryAndProfiles();
  checkAuthStatus();

  await loadSettingsFromStorage();
  initSettingsTracking();
  updateSearchMode();
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
    $('track-table-wrap')?.classList.remove('hidden');
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
                div.style.cssText = 'padding:8px 12px; cursor:pointer; border-radius:6px; display:flex; align-items:center; gap:8px; color:var(--text); font-size:13px; border:1px solid transparent;';
                const icon = item.type === 'dir'
                    ? '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>'
                    : '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H7a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M9 13h6M9 17h6"/></svg>';
                div.innerHTML = icon + ' ';
                div.appendChild(document.createTextNode(item.name));

                div.onmouseover = () => div.style.backgroundColor = 'var(--surface2)';
                div.onmouseout = () => div.style.backgroundColor = 'transparent';

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
            entriesDiv.innerHTML = '<div style="padding:20px; text-align:center; color:var(--muted); font-size:12px;">No files or subdirectories found.</div>';
        }

        $('fb-back').disabled = !data.parent;
        $('fb-back').style.opacity = data.parent ? '1' : '0.5';

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

            scoreCol = `<span class="local-badge ${isSafe ? 'ok' : 'warn'}" title="Confidence Score">${best.confidence}%</span>`;
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