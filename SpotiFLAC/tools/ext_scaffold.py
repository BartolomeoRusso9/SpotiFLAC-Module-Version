"""Extension scaffolding — generates a working starting point for a new
SpotiFLAC extension, Python or JavaScript.

The README is upfront that Python extension docs don't exist yet beyond
reading extensions/python_provider.py, and that the JS guide is borrowed
from SpotiFLAC Mobile and may not match this loader exactly. This module
is the concrete alternative: run it, get a manifest + entry point that
already satisfy this repo's own loader (extensions/python_provider.py for
Python, extensions/_bridge.js's `registerExtension()` contract for JS), and
fill in the TODOs.

Used by `spotiflac --ext-scaffold NAME` (see launcher.py); also usable
directly:

    from SpotiFLAC.tools.ext_scaffold import scaffold_extension
    scaffold_extension("my-provider", runtime="python", output_dir=".")
"""

from __future__ import annotations

import json
from pathlib import Path

SUPPORTED_RUNTIMES = ("python", "javascript")


def _manifest(name: str, runtime: str, display_name: str) -> dict:
    entry = f"{name.replace('-', '_')}.py" if runtime == "python" else "index.js"
    return {
        "name": name,
        "displayName": display_name,
        "version": "0.1.0",
        "description": f"TODO: describe what {display_name} does.",
        "runtime": runtime,
        "entryPoint": entry,
        "type": ["download_provider"],
        "urlHandler": {"patterns": []},
        "settings": [],
    }


def _python_template(name: str, display_name: str) -> str:
    class_name = "".join(p.capitalize() for p in name.replace("-", "_").split("_"))
    return f'''"""{display_name} — SpotiFLAC Python extension.

Loaded by SpotiFLAC/extensions/python_provider.py, which requires this
module to expose EXACTLY ONE class subclassing BaseProvider — see
SpotiFLAC/core/base.py for the full contract (download_track_async() is
the one abstract method you must implement; _build_output_path(),
_file_exists(), and self._async_http are the shared helpers already done
for you).

Try it without touching your real ~/.spotiflac/extensions:
    spotiflac --ext-dry-run /path/to/this/folder
"""

from __future__ import annotations

from SpotiFLAC.core.base import BaseProvider
from SpotiFLAC.core.errors import TrackNotFoundError
from SpotiFLAC.core.models import DownloadResult, TrackMetadata


class {class_name}Provider(BaseProvider):
    name = "{name}"

    async def download_track_async(
        self,
        metadata: TrackMetadata,
        output_dir: str,
        *,
        filename_format="{{title}} - {{artist}}",
        position: int = 1,
        include_track_num: bool = False,
        use_album_track_num: bool = False,
        first_artist_only: bool = False,
        artist_separator: str | None = None,
        allow_fallback: bool = True,
        embed_lyrics: bool = False,
        lyrics_providers: list[str] | None = None,
        enrich_metadata: bool = False,
        enrich_providers: list[str] | None = None,
        is_album: bool = False,
        **kwargs,
    ) -> DownloadResult:
        # 1. TODO: search your service for `metadata` (title/artist/isrc/
        #    duration_ms are usually enough — see TrackMetadata in
        #    core/models.py for every field available) and resolve a
        #    matching track id / stream URL. Raise TrackNotFoundError if
        #    nothing matches, so the downloader moves on to the next
        #    configured provider instead of treating this as a hard error.
        raise TrackNotFoundError(
            self.name, f"{{metadata.first_artist}} - {{metadata.title}} (TODO: implement search)"
        )

        # 2. Once you have a stream URL, build the destination path with
        #    the shared helper (handles filename_format, subfolders, the
        #    {{platform}}/{{id}} placeholders, etc.) — extension omitted
        #    since BaseProvider figures it out from what you pass here:
        # path = self._build_output_path(
        #     metadata, output_dir, filename_format, position,
        #     include_track_num, use_album_track_num, first_artist_only,
        #     extension=".flac", native_id=resolved_track_id,
        # )
        # if self._file_exists(path):
        #     return DownloadResult(success=True, file_path=str(path), skipped=True)

        # 3. Download with the shared, rate-limited, retrying HTTP client
        #    (self._async_http, set up by BaseProvider.__init__ from what
        #    you pass to super().__init__() if you override it) — see any
        #    installed extension's provider module for a full streaming-
        #    download example (chunked writes + self._progress_cb calls).

        # 4. Return the result:
        # return DownloadResult(success=True, file_path=str(path))
'''


def _javascript_template(name: str, display_name: str) -> str:
    return f"""// {display_name} — SpotiFLAC JavaScript extension.
//
// Loaded by SpotiFLAC/extensions/_bridge.js inside a Node.js worker thread.
// Every function below runs SYNCHRONOUSLY (no async/await, no Promises —
// use Node's *Sync file/network calls) and is exposed to Python by calling
// the global registerExtension() at the bottom, once, with every method
// you implement.
//
// Try it without touching your real ~/.spotiflac/extensions:
//     spotiflac --ext-dry-run /path/to/this/folder

function initialize(settings) {{
  // Optional. `settings` matches this manifest's `settings` schema
  // (defaults merged with anything the user configured for this
  // extension). Nothing required here if you have no settings yet.
}}

function checkAvailability(isrc, title, artist, options) {{
  // TODO: look up (isrc, title, artist) against your service.
  // Return {{ available: false }} if nothing matches — the downloader
  // will fall back to the next configured extension, not treat it as
  // a hard failure.
  return {{ available: false }};
  // On a match: return {{ available: true, track_id: "..." }};
}}

function download(track_id, quality, output_path, onProgress) {{
  // TODO: resolve track_id to a stream URL and write it to output_path
  // (Node's fs.writeFileSync / a sync HTTP client — see storage.* helpers
  // exposed by the bridge for chunked file writes without loading an
  // entire track into memory). Call onProgress(percent) periodically if
  // you can report it; it's optional.
  //
  // Return shape on success:
  // return {{ success: true, file_path: output_path, title: "...", artist: "..." }};
  return {{ success: false, error: "not implemented" }};
}}

// Optional: only needed if users should be able to paste a URL from your
// service directly (not just get matched via Spotify metadata).
// function handleURL(url) {{
//   return {{ type: "track", id: "..." }};
// }}

registerExtension({{
  initialize,
  checkAvailability,
  download,
  // handleURL,
}});
"""


def _readme_template(name: str, runtime: str) -> str:
    dry_run_hint = "spotiflac --ext-dry-run ."
    return f"""# {name}

Scaffolded by `spotiflac --ext-scaffold {name} --runtime {runtime}`.

## Next steps

1. Fill in the TODOs in the entry point ({"index.js" if runtime == "javascript" else name.replace("-", "_") + ".py"}).
2. Validate the manifest and confirm the entry point loads, without
   installing it into your real extensions folder:

       {dry_run_hint}

3. Once it works, package it as a `.spotiflac-ext` (a ZIP of this folder's
   contents, manifest.json at the root) and either:
   - install it directly for yourself: `ExtensionManager().install_from_file(...)`
   - or publish it, and a `sha256` + `download_url` for it, in a registry
     JSON of your own (see the README's Extensions section) so others can
     install it via `--registries`.

Nothing here is reviewed, bundled, or endorsed by the SpotiFLAC maintainer
— see the README's Extensions section for why, and for what a registry
entry needs.
"""


def scaffold_extension(
    name: str,
    *,
    runtime: str = "python",
    output_dir: str | Path | None = None,
    display_name: str | None = None,
) -> Path:
    """Writes a new extension skeleton to `output_dir/name/` (default:
    `./name/`). Returns the created directory's Path.

    Raises ValueError for an unknown runtime, or FileExistsError if the
    target directory already exists (never silently overwrites someone's
    work in progress).
    """
    if runtime not in SUPPORTED_RUNTIMES:
        msg = (
            f"Unknown runtime '{runtime}'. Use one of: {', '.join(SUPPORTED_RUNTIMES)}"
        )
        raise ValueError(msg)

    safe_name = name.strip().lower()
    if not safe_name:
        raise ValueError("Extension name cannot be empty")

    display_name = display_name or name
    base = Path(output_dir) if output_dir else Path.cwd()
    target = base / safe_name
    if target.exists():
        raise FileExistsError(f"'{target}' already exists")

    target.mkdir(parents=True)
    (target / "manifest.json").write_text(
        json.dumps(_manifest(safe_name, runtime, display_name), indent=2) + "\n",
        encoding="utf-8",
    )

    if runtime == "python":
        entry_name = f"{safe_name.replace('-', '_')}.py"
        (target / entry_name).write_text(
            _python_template(safe_name, display_name), encoding="utf-8"
        )
    else:
        (target / "index.js").write_text(
            _javascript_template(safe_name, display_name), encoding="utf-8"
        )

    (target / "README.md").write_text(
        _readme_template(safe_name, runtime), encoding="utf-8"
    )

    return target
