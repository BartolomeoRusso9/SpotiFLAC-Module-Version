# SpotiFLAC — Extension System

The extension system allows you to load JS providers from the registry of
[SpotiFLAC-Extension](https://github.com/zarzet/SpotiFLAC-Extension) and use them
directly in the Python module, without rewriting anything in Python.

---

## Requirements

- **Node.js ≥ 16** in PATH (`node --version`)
- Python ≥ 3.11

---

## Quick Setup

```python
from SpotiFLAC.extensions import ExtensionManager

em = ExtensionManager()                   # default: ~/.spotiflac/extensions/

# See available extensions online
for entry in em.fetch_registry():
    print(entry.id, entry.version, entry.description)

# Install
em.install("soundcloud")
em.install("pandora")
em.install("ytmusic-spotiflac")

# List installed
for ext in em.list_installed():
    print(ext.name, ext.version, "→", ext.types)

# Update all
status = em.update_all()
print(status)
```

---

## Usage with SpotiFLAC

Pass `"ext:<name>"` in the `services` list:

```python
from SpotiFLAC import SpotiFLAC

sf = SpotiFLAC(
    services=["ext:soundcloud", "tidal", "qobuz"],   # try SC first, then Tidal
)
sf.download("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT")
```

Or use the provider directly:

```python
from SpotiFLAC.extensions import JSExtensionProvider

with JSExtensionProvider("soundcloud") as p:
    result = p.download_track(metadata, output_dir="/tmp/music")
    print(result.file_path)
```

---

## Extension Settings

Each extension has optional settings defined in its `manifest.json`.

```python
em = ExtensionManager()

# Save permanent settings to disk
em.save_settings("pandora", {
    "apiBaseUrl": "https://api.example.com",
})

# Or pass settings at runtime
provider = JSExtensionProvider("pandora", settings={"apiBaseUrl": "https://..."})
```

---

## Install from local file or custom URL

```python
# From local file
em.install_from_file("/path/to/my-ext.spotiflac-ext")

# From direct URL
em.install_from_url("https://example.com/my-ext.spotiflac-ext")
```

---

## API pubblica del runtime JS

You can also call extension methods directly:

```python
from SpotiFLAC.extensions import JSExtensionProvider

with JSExtensionProvider("soundcloud") as p:
    # Check availability for ISRC
    avail = p.check_availability(
        isrc="GBUM71029604",
        track_name="Bohemian Rhapsody",
        artist_name="Queen",
    )
    print(avail)  # {'available': True, 'track_id': '12345678'}

    # Resolve a direct URL
    info = p.handle_url("https://soundcloud.com/artist/track")
    print(info)   # {'type': 'track', 'metadata': {...}}

    # Download by native ID
    result = p.download_track(metadata, "/tmp/music")
```

---

## `.spotiflac-ext` Format

A `.spotiflac-ext` file is a ZIP containing:

```
my-ext.spotiflac-ext (ZIP)
├── manifest.json
├── index.js
└── icon.jpg         (optional)
```

### manifest.json

```json
{
  "name": "my-provider",
  "displayName": "My Provider",
  "version": "1.0.0",
  "description": "...",
  "type": ["metadata_provider", "download_provider"],
  "minAppVersion": "4.2.3",
  "permissions": {
    "network": ["api.myprovider.com"],
    "storage": true,
    "file": true
  },
  "urlHandler": {
    "enabled": true,
    "patterns": ["myprovider.com"]
  },
  "settings": [
    {
      "key": "apiKey",
      "label": "API Key",
      "type": "string",
      "default": "",
      "description": "Your provider API key.",
      "secret": true
    }
  ]
}
```

### index.js — Available Global APIs

```javascript
// HTTP
var resp = http.get(url, headersObj);
// resp → { statusCode, body, headers, url, error }

var resp = http.post(url, bodyString, headersObj);

// Storage (persistent in runtime session)
storage.set("key", JSON.stringify(data));
var raw = storage.get("key");  // string | null

// File download
var result = file.download(url, outputPath, { headers: {...} });
// result → { success, path, size } | { success: false, error }

// Logging
log.info("message");
log.warn("warning");
log.error("error");

// Utils
var ua = utils.randomUserAgent();
var appUA = utils.appUserAgent();  // "SpotiFLAC-Python/1.2"

// Register extension (required)
registerExtension({
  initialize:        function(settings) { /* returns true */ },
  cleanup:           function() {},
  handleURL:         function(url) { /* returns {type, ...} */ },
  handleUrl:         function(url) { /* alias */ },
  checkAvailability: function(isrc, name, artist, opts) { /* {available, track_id} */ },
  download:          function(trackId, quality, outputPath, onProgress) { /* {success, file_path, ...} */ },
  getTrack:          function(id) {},
  getAlbum:          function(id) {},
  getArtist:         function(id) {},
  getPlaylist:       function(id) {},
});
```

---

## Internal Architecture

```
Python                          Node.js (_bridge.js)
  │                                │
  │  stdin: {"id":1,"call":"download","args":[...]}
  │ ─────────────────────────────► │
  │                                │   Worker Thread
  │                                │   ┌──────────────────────┐
  │                                │   │  extension index.js  │
  │                                │   │                      │
  │                                │   │  http.get(url)       │
  │                                │   │  ┌──SharedArrayBuf──┐│
  │                                │   │  │ Atomics.wait()   ││
  │                                │──►│  └──────────────────┘│
  │                                │   └──────────────────────┘
  │                                │   Main Thread: executes HTTP
  │                                │   Atomics.notify() → worker resumes
  │                                │
  │  stdout: {"id":1,"result":{...}}
  │ ◄─────────────────────────────│
```

The `JSRuntime` Python manages the Node.js process, sends commands via stdin and
receives responses via stdout on a dedicated background thread.
