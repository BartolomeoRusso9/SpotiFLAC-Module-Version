[← Back to the README](../../README.md) · [Guide index](README.md)

# Automation & Operations

Everything here is for SpotiFLAC running as part of something else: a
script, a cron job, a NAS container, a library pipeline.

---

## Machine-readable output (`--json`)

Human output already goes to stderr, so stdout carries only the document:

```bash
spotiflac "https://open.spotify.com/album/..." ~/Music --json | jq '.summary'
```

```json
{
  "schema_version": 1,
  "started_at": 1787913132.02,
  "finished_at": 1787913267.44,
  "summary": { "total": 12, "downloaded": 11, "skipped": 0, "failed": 1 },
  "tracks": [
    {
      "id": "4cOdK2wGLETKBW3PvgPWqT",
      "title": "Never Gonna Give You Up",
      "artists": "Rick Astley",
      "album": "Whenever You Need Somebody",
      "isrc": "GBARL9300135",
      "duration_ms": 213573,
      "status": "downloaded",
      "provider": "tidal",
      "format": "flac",
      "file_path": "/home/me/Music/Rick Astley - Never Gonna Give You Up.flac",
      "error": null
    }
  ]
}
```

`status` is one of `downloaded`, `skipped`, `failed`. A skipped track rides
on a *successful* result (the file was already there), so count `status`,
not success.

The document is emitted even when the run fails or is interrupted — a
script should never have to tell "no JSON" apart from "no tracks". Check
`schema_version` before relying on the shape.

---

## Post-download hooks (`--post-hook`)

Your own Python, called after every finished track, with typed objects
instead of a shell string:

```python
# mylib/hooks.py
from SpotiFLAC.core.models import DownloadResult, TrackMetadata

def on_track(result: DownloadResult, metadata: TrackMetadata) -> None:
    if result.success:
        print(f"{metadata.artists} — {metadata.title} → {result.file_path}")
```

```bash
spotiflac "<url>" ~/Music --post-hook mylib.hooks:on_track
```

Repeatable. `async def` hooks are awaited; synchronous ones run in a worker
thread, so slow work (a library scan, a beets import) doesn't stall the
other concurrent downloads.

Hooks fire for failures too — check `result.success`. One that raises is
logged and skipped: a broken notifier must not fail a download that already
succeeded. A typo in the hook name fails immediately, when the run is
configured, rather than silently doing nothing for two hours.

**Why not `--post-action=command`?** That hands a string to a shell, so
filenames with quotes or semicolons have to be escaped by whoever wrote the
template, and an album title is not a safe string. It also runs once per
batch, not per track. It still exists for the CLI, where you already had a
shell — but it is refused from the GUI and web API unless
`SPOTIFLAC_ALLOW_POST_COMMAND=1` was set when the process started.

---

## Notifications (`--notify`)

A headless instance finishing a run at 3am tells nobody. `library_notify`
asks a *media server* to rescan, which is one specific errand; this is the
general one.

```bash
# One message per run, to a Discord webhook (the default: --notify-on summary)
spotiflac <url> ~/Music --notify discord --notify-url "https://discord.com/api/webhooks/…"

# A message for every failed track, to a self-hosted ntfy
spotiflac <url> ~/Music --notify ntfy --notify-url "http://nas.local:8080/music" \
                        --notify-on failure

# Telegram takes a bot token and a chat id rather than a URL
spotiflac <url> ~/Music --notify telegram --notify-token "$BOT_TOKEN" \
                        --notify-chat-id 123456

# A plain webhook receives the structured JSON, not a sentence
spotiflac <url> ~/Music --notify webhook --notify-url https://example.com/hook
```

| Flag | Values |
| --- | --- |
| `--notify` | `webhook`, `discord`, `telegram`, `ntfy` |
| `--notify-url` | Destination. Falls back to `$SPOTIFLAC_NOTIFY_URL`. |
| `--notify-token` | Bearer token (ntfy, webhook) or bot token (Telegram). Falls back to `$SPOTIFLAC_NOTIFY_TOKEN`. |
| `--notify-chat-id` | Telegram only. |
| `--notify-on` | `summary` (default), `success`, `failure`, `both` |

`summary` sends one message for the whole run, listing only the failures —
which is what you want for a 300-track discography. The other three send one
message per track.

A misconfigured notifier fails **before** anything is downloaded, rather than
after a two-hour run finishes and says nothing.

### What is sent, and what isn't

Title, artist, album, provider, format, and success/failure. **Not the file
path** — it leaks the server's directory layout, and the receiving end can do
nothing with a path on another host.

Like the library token, the destination is read from the CLI or the
environment and **never** from the GUI/web config. This sends data off the
machine to a host of someone's choosing, and an HTTP request must not get to
pick which host that is.

A notifier that is down is logged and ignored: the files are on disk either
way, and a failed notification must not turn a completed run into a failed
one.

## Playlists and music libraries

Write an M3U of everything a run downloaded:

```bash
spotiflac "<url>" ~/Music --write-m3u ~/Music/latest.m3u8
```

Paths are relative to the playlist file, so the folder stays portable.

Tell a music server to rescan when the run finishes, instead of waiting for
its own schedule:

```bash
export SPOTIFLAC_LIBRARY_TOKEN="..."
spotiflac "<url>" /media/music \
  --library-rescan jellyfin --library-url http://nas.local:8096
```

| `--library-rescan` | Credential | Notes |
| --- | --- | --- |
| `plex` | X-Plex-Token | Refreshes every section |
| `jellyfin`, `emby` | API key | |
| `navidrome`, `subsonic` | Password + `--library-user` | Salted-hash auth; the password never travels in the URL |

The token comes from `--library-token` or `$SPOTIFLAC_LIBRARY_TOKEN` — never
from a GUI or web request, which must not get to choose which host a
credential is sent to. A server that is down is logged and ignored: the
files are on disk either way.

**A note on Navidrome/Subsonic.** Plex and Jellyfin take a revocable API
token. Subsonic's protocol instead authenticates with `md5(password + salt)`
computed from the account password — that construction is specified by the
API, not chosen here, and the server compares against the same digest. It is
the strongest of the three options Subsonic accepts (the other two send the
password in the query string), but it means the value on the wire is
derived from your password and can be attacked offline if captured. Use
`https://`, and give SpotiFLAC an account you use for nothing else.

SpotiFLAC warns whenever a library URL is plain `http://` and not loopback —
for every server type, because a Plex or Jellyfin token is equally readable
on the path and stays valid until you revoke it. It warns rather than
refuses: a NAS on a home LAN is the deployment this feature exists for, and
HTTPS there usually means a self-signed certificate.

---

## Resuming interrupted downloads

On by default. An interrupted transfer leaves its `.part` file, and the next
attempt sends a `Range` header and continues from there rather than starting
over. Servers that ignore `Range` are handled correctly — the partial is
discarded and the download restarts, rather than being appended to.

`--no-resume` restores the previous behaviour: every run starts each file
from zero, and `.part` files are cleaned up at the end.

---

## Cache maintenance

```bash
spotiflac --cache-stats                       # what's on disk
spotiflac --cache-prune --dry-run             # what would go
spotiflac --cache-prune --cache-max-age-days 30
spotiflac --cache-clear                       # everything disposable
```

`--cache-clear` keeps `profiles.json` and `gui-settings.json`: they live in
the same directory but hold things you typed, not things SpotiFLAC can
re-fetch. Add `--json` to any of these for parseable output.

---

## Running the web server

### Liveness and metrics

| Endpoint | Auth | For |
| --- | --- | --- |
| `GET /healthz` | none | Container health checks |
| `GET /api/metrics` | same as the rest of `/api/` | Provider success rates, queue depth, connected clients (single-user only — see below) |

`/healthz` is deliberately unauthenticated: an orchestrator has no token,
and a probe that 401s reports "unhealthy" for a reason unrelated to health.
It discloses only that the process is answering. `/metrics` does expose real
information, so it sits behind whatever auth is configured.

Under `--web-multiuser` it reports only the version and the auth
configuration. The rest of the counters are instance-wide — every account's
downloads, the shared queue, how many clients are connected — and accounts
are flat, with no administrator role to show them to, so they are omitted
rather than shown to whoever logs in first.

### Multi-user

`--web-multiuser` gives each account its own search results, its own
download folder under the shared root, and its own event stream. Sign in
through the browser — the login form appears automatically when the server
says one is needed.

```bash
spotiflac --web-user-add alice 'a-good-password'
spotiflac --web --host 0.0.0.0 --web-multiuser
```

Still shared, because it is genuinely machine-wide: installed extensions,
the registry configuration, the trust store, the HTTP connection pool.
Accounts run in one process as one OS user — this is household or
small-team separation, not hostile-tenant isolation.

Failed logins are rate limited per client address with exponential backoff,
and an unknown username costs the same time as a known one, so the form
can't be used to enumerate accounts.
