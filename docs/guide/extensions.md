<!-- Extracted verbatim from README.md. The README had grown to 76 KB
     and 87 headings, which is past the point where either GitHub or
     PyPI renders it usefully. Nothing here was reworded in the split. -->

[← Back to the README](../../README.md)

# Extensions

## Extensions

SpotiFLAC has **no built-in download provider and no default extension source**. Every provider — Tidal, Qobuz, Amazon Music, Deezer, or anything else — is supplied entirely by extensions that you find, review, and choose to install yourself. Two extension runtimes are supported:

- **JavaScript** — sharing the same extension format used by [SpotiFLAC Mobile](https://github.com/zarzet/SpotiFLAC-Mobile), executed via a Node.js bridge.
- **Python** — packaged as `.spotiflac-ext` / `.sflx` files (a ZIP containing a manifest and a Python entry point), loaded directly in-process.

Extensions are never fetched or installed automatically. You must explicitly configure a registry before SpotiFLAC will contact anything. There are several equivalent ways to do it — pick whichever fits your workflow:

**Environment variable:**

```bash
# Comma-separated list of registry JSON URLs — none is set by default
export SPOTIFLAC_REGISTRIES="https://example.com/my-registry.json"
```

**`.env` file** (see `.env.example`):

```env
SPOTIFLAC_REGISTRIES=https://example.com/my-registry.json
```

**CLI flag** (`--registries`, repeat once per URL) — persisted to `~/.spotiflac/registry_settings.json`, so you only need to pass it once and it's picked up on every future run, exactly like a registry added from the GUI/Interactive wizard:

```bash
spotiflac --registries https://example.com/my-registry.json URL ./out
```

**Python API** (`registries` parameter on `SpotiFLAC()` / `AsyncSpotiFLAC()`) — persisted the same way as the CLI flag:

```python
from SpotiFLAC import SpotiFLAC

SpotiFLAC(
    url="https://open.spotify.com/track/...",
    output_dir="./downloads",
    registries=["https://example.com/my-registry.json"],
)
```

```python
from SpotiFLAC import AsyncSpotiFLAC

async with AsyncSpotiFLAC(
    output_dir="./downloads",
    registries=["https://example.com/my-registry.json"],
) as client:
    await client.download_track("https://open.spotify.com/track/...")
```

**Interactive wizard / GUI** — both expose a registry manager (add, remove, list, and see where each URL came from) without touching environment variables or files by hand.

All of these feed into the same merged, deduplicated list (`extensions.registry_config.effective_urls()`), regardless of which entry point you use — the CLI, the GUI, and the Python API all end up contacting the same registries.

Once configured, you can also install and manage extensions directly:

```python
from SpotiFLAC.extensions import ExtensionManager

em = ExtensionManager()
em.install("some-extension-id", registry_url="https://example.com/my-registry.json")
```

Extensions use the `ext:` prefix and are referenced like any other provider:

```bash
spotiflac URL ./out \
  --service ext:tidal-web ext:qobuz-web
```

> **Note:** If Node.js is not installed, SpotiFLAC automatically attempts to install it the first time a JavaScript extension is used, printing progress as it goes (`core/node_check.py`) — it never escalates privileges itself (no `sudo`/`runas` is ever added on your behalf), so on Linux it works out of the box only when already running as root, and otherwise tells you the exact command to run yourself. (The Docker image needs neither: Node.js is installed at build time, and the container runs as a non-root user.) A startup check (same idea as the ffmpeg one) also warns upfront if Node.js is missing, independent of whether the auto-install ends up working.
>
> Supported package managers:
>
> - **Linux:** apt-get, dnf, yum, pacman
> - **macOS:** brew
> - **Windows:** winget, choco

**A note on legacy names:** for backwards compatibility, short names like `tidal`, `qobuz`, `amazon`, `deezer`, `apple`, `soundcloud`, `youtube`, `pandora` are still accepted in `services`/`--service`, and are resolved to an installed extension with a matching ID (e.g. `tidal` → `ext:tidal-web`) if — and only if — you have that extension installed. They are aliases, not built-in providers; nothing downloads without an extension behind it.

The maintainer does not review, endorse, or take responsibility for the content or behavior of any third-party registry or extension. Choose your sources with the same care you would apply to installing any other third-party code.

### Extension Discovery (Directories)

Finding a registry in the first place is still on you — a **directory** is just a JSON file that lists *registries* (name, URL, description), for you to review and add yourself the normal way. Nothing is bundled here either: no default directory ships with SpotiFLAC.

```bash
export SPOTIFLAC_REGISTRY_DIRECTORIES="https://example.com/my-directory.json"
# or: spotiflac --registry-directories https://example.com/my-directory.json URL ./out
```

A directory JSON looks like:

```json
{
  "registries": [
    {
      "name": "Example Community Registry",
      "url": "https://example.com/registry.json",
      "description": "A few extra extensions",
      "maintainer": "someone"
    }
  ]
}
```

Once you've added one, Settings → Extensions in the GUI (or `get_registry_directories()` / `add_registry_directory()` / `remove_registry_directory()` / `discover_registries()` in the Python/web API) fetches it and probes each listed registry for reachability, so you see a "reachable, N extensions" badge *before* deciding to add it as a registry of your own via the normal `--registries` flow. Probing is read-only and never installs anything on its own.

### Registry Trust (Signed Extensions)

The sha256 checksum a registry provides (see above) proves a package wasn't corrupted or swapped *in transit* — it says nothing about who put it in the registry in the first place. Ed25519 signatures close that gap: a registry maintainer signs each entry with their own private key, and you decide whose public key you're willing to trust, once, up front. Nothing is trusted by default — an unsigned entry is exactly as trusted as it is today (checksum-only, if the registry provides one at all).

```bash
# Add a maintainer's public key you've decided to trust
spotiflac --trust-key-add "some-maintainer" "<base64 Ed25519 public key>"
spotiflac --trust-key-list
spotiflac --trust-key-remove "some-maintainer"
```

Or from Settings → Extensions in the GUI ("Trusted Signing Keys"), backed by `get_trusted_keys()` / `add_trusted_key()` / `remove_trusted_key()` in the Python/web API.

Once added, any installed extension's `RegistryEntry` gets a `.trust_tier` of `"signed"` (verified against a trusted key), `"checksum-only"`, or `"unverified"`.

**For registry maintainers** — generate a keypair and sign your own entries with the bundled tool:

```bash
python -m SpotiFLAC.tools.registry_signing_cli keygen
# publish the printed public key however you publish your registry;
# keep the private key secret

python -m SpotiFLAC.tools.registry_signing_cli sign \
  --private-key <base64> --id tidal-web --version 1.2.0 \
  --sha256 <hex> --download-url https://example.com/tidal-web.spotiflac-ext
# paste the printed "signature" into that entry in your registry.json
```

### Developing Extensions

- **JavaScript extensions** reuse the format built for [SpotiFLAC Mobile](https://github.com/zarzet/SpotiFLAC-Mobile). Its [Extension Development Guide](https://github.com/spotiflacapp/SpotiFLAC-Mobile/blob/main/docs/EXTENSION_DEVELOPMENT.md) is the closest available reference, but it was written for Mobile — some details (packaging, available runtime capabilities) may not match this project exactly. Verify against this repository's own loader (`SpotiFLAC/extensions/runtime.py`) before relying on it.
- **Python extensions** are ZIP packages (`.spotiflac-ext` / `.sflx`) containing a manifest and a Python module, loaded directly by `SpotiFLAC/extensions/python_provider.py`. There's no separate guide yet — reading that file, and an existing extension's manifest, is currently the best way to see the expected shape.

**Scaffolding a new extension** generates a starting point that already satisfies this repo's own loader, instead of reverse-engineering the shape from an existing extension:

```bash
spotiflac --ext-scaffold my-provider --runtime python      # or --runtime javascript
# writes ./my-provider/{manifest.json, my_provider.py, README.md}
```

**Validating it** — without installing into your real `~/.spotiflac/extensions` or contacting any registry — checks the manifest, confirms the entry point exists and imports/parses cleanly, and (Python) that it exposes exactly one `BaseProvider` subclass, or (JavaScript, if `node` is on `PATH`) that it's syntactically valid and calls `registerExtension(...)`:

```bash
spotiflac --ext-dry-run ./my-provider
# or against an already-packaged ZIP:
spotiflac --ext-dry-run ./my-provider.spotiflac-ext
```

If you build something reusable, consider publishing it to your own registry rather than asking the maintainer to bundle or endorse it — see [Extensions](#extensions) above for why nothing is bundled by design.

---

---

## Enforcing trust (`--min-trust-tier`)

Signatures used to be advisory: `trust_tier` was computed, shown as a badge,
and then ignored — an entry whose signature failed to verify installed
exactly like one that verified. A floor makes the verification mean
something.

```bash
# Refuse anything the registry didn't publish a checksum for
spotiflac --min-trust-tier checksum-only ...

# Refuse anything not signed by a key you added yourself
spotiflac --min-trust-tier signed ...
```

Also settable as `SPOTIFLAC_MIN_TRUST`. The three tiers, in increasing order:

| Tier | Means |
| --- | --- |
| `unverified` | The registry gave no sha256. The default, and today's behaviour — nothing is bundled, so a stricter default would leave a fresh install unable to install anything at all. |
| `checksum-only` | The registry published a sha256 and the package matches it. Proves the package wasn't altered in transit; says nothing about who put it there. |
| `signed` | The entry carries an Ed25519 signature that verifies against a key you added with `--trust-key-add`. |

A rejected entry is skipped with a warning and the rest of the registry is
still processed — the bootstrap doesn't abort. If an entry *claims* a
signature that doesn't verify, the message says so specifically: that is
either the wrong key in your trust store, or something you want to know
about.

The floor does not apply to `install_from_file()`. A local file has no
registry entry to be signed against and could only ever score `unverified`,
so gating it would turn "distrust the registry" into "you may no longer
install your own extension from disk".

---

## What runs where

JavaScript extensions run in their own `node` process. That process is
started with an allowlisted environment rather than a copy of the host's:
previously every third-party extension could read `SPOTIFLAC_WEB_TOKEN`,
`AWS_SECRET_ACCESS_KEY` and everything else out of `process.env`. What it
still gets is what a program making HTTPS requests needs — `PATH`, proxy
settings, CA bundles. On POSIX it also runs under address-space, CPU and
file-size limits.

If an extension genuinely needs a variable:

```bash
export SPOTIFLAC_EXT_ENV_PASSTHROUGH="MY_PROVIDER_KEY,OTHER_VAR"
```

`SPOTIFLAC_EXT_NO_SANDBOX=1` restores the old behaviour wholesale.

**This is not a security boundary.** The extension still runs as the same OS
user, can still open sockets and write files. It removes an accidental
handover of credentials and caps obvious resource exhaustion; it does not
contain a hostile extension. Real isolation needs a container or a separate
user account.

Python extensions get none of this: they are imported into the SpotiFLAC
process, so they share its memory and environment by construction.
