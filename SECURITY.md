# Security Policy

## Reporting a vulnerability

Please report security issues **privately**, not as a public GitHub issue.

- Preferred: [GitHub private vulnerability reporting](https://github.com/BartolomeoRusso9/SpotiFLAC-Module-Version/security/advisories/new)
- Alternative: contact the maintainer through the
  [Telegram community](https://t.me/SpotiFLAC_Chat) and ask for a private channel.

A useful report includes the version (`spotiflac --version` or
`pip show SpotiFLAC`), how SpotiFLAC was running (module import, CLI, `--gui`,
`--web`, Docker), and the smallest set of steps that shows the problem.

You will get an acknowledgement within a few days. Fixes ship in the next
release; if one is urgent, it gets its own. Please give a fix a reasonable
window before disclosing publicly, and tell us if you would like credit in
the release notes.

## Supported versions

Only the latest release on PyPI receives security fixes. Older versions are
not backported.

## Scope

In scope — anything in this repository:

- The Python module and CLI.
- The `--gui` (pywebview) and `--web` (FastAPI) interfaces, including the
  frontend under `SpotiFLAC/frontend/`.
- The extension loading, checksum and signature machinery
  (`SpotiFLAC/extensions/`).
- The published Docker image.

Out of scope:

- **Third-party extensions and registries.** Nothing is bundled: extensions
  are installed only from a source you configure yourself, and they run with
  the same privileges as SpotiFLAC. The maintainer neither reviews nor
  controls them. A malicious extension doing malicious things is that
  extension's problem — a way for an extension to escape a limit SpotiFLAC
  claims to enforce is ours.
- The streaming services and metadata providers themselves.
- Anything requiring an attacker to already have local code execution or
  filesystem access as your user.

## Things that are by design, not vulnerabilities

Please don't report these as findings — but do report a way to trigger them
that the operator did not choose:

- **`--web` is unauthenticated by default** and binds to `127.0.0.1`.
  Exposing it further is opt-in (`--host`), and the server logs a warning
  when you do. `--web-token` and `--web-multiuser` exist for that case.
- **`--web-multiuser` does not isolate accounts from each other.** All
  sessions share one application instance, one download directory, and one
  set of search results; accounts identify *who submitted a job*, not
  separate tenants. See the note on `SESSION_COOKIE` in `webapp.py`.
- **`--post-action command` runs a shell command.** From the CLI this is the
  operator's own shell, so it grants nothing new. It is *not* accepted from
  the GUI/web bridge unless `SPOTIFLAC_ALLOW_POST_COMMAND=1` was set when the
  process started — a way to run one without that variable is a real finding.
- **Installing an extension executes its code.** Checksums and Ed25519
  signatures tell you the package is the one the registry described; they
  don't sandbox it.
- **Sessions live in memory** and are lost on restart.

## What the project does to reduce risk

- Extension archives are size-capped, rejected if they unpack beyond a limit,
  and refused if they contain absolute paths, `..` components, symlink
  entries, or a manifest name that would escape the extensions directory.
- Registry checksums are enforced; Ed25519 signature verification is
  available on top, against keys you add yourself.
- Account passwords use PBKDF2-HMAC-SHA256 (600k iterations) with per-user
  salts; failed logins are rate limited, and an unknown username costs the
  same time as a known one.
- Credential and trust files are written atomically and owner-only.
- Writing the trust store is not reachable from the HTTP API.
