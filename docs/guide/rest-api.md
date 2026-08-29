# The REST API (`/api/v1`)

`spotiflac --web` serves two HTTP surfaces.

The older one, `POST /api/<method_name>`, is an RPC bridge onto the same
`SpotiFLAC_API` object the desktop window uses. It exists so the frontend and
the desktop GUI share one implementation, and it stays exactly as it was. It
is not a contract: the names are GUI internals, there is no schema, and
renaming a method to tidy the desktop code would silently break anything else
pointed at it.

`/api/v1` is the contract. Declared request and response models, an OpenAPI
document, and only the endpoints an integrator actually needs.

```bash
spotiflac --web --port 8000
open http://127.0.0.1:8000/docs        # interactive OpenAPI browser
curl http://127.0.0.1:8000/openapi.json
```

## Authentication

None of its own — it inherits whatever the instance is configured with, since
every path starts with `/api/`:

- **Nothing** (default): open, like the rest of `--web`.
- **`--web-token`**: pass `?token=…` once, or send the cookie it sets.
- **`--web-multiuser`**: log in at `POST /api/auth/login`; the session cookie
  covers `/api/v1` too.

```bash
curl -c jar -X POST localhost:8000/api/auth/login \
     -H 'content-type: application/json' \
     -d '{"username":"alice","password":"…"}'
curl -b jar localhost:8000/api/v1/info
```

## Endpoints

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/api/v1/info` | Version, whether multi-user and whether authenticated. |
| `POST` | `/api/v1/resolve` | A URL → its tracks. Downloads nothing. |
| `GET` | `/api/v1/search?q=` | Search Spotify for tracks. |
| `POST` | `/api/v1/downloads` | Queue a download. **202** with a job. |
| `GET` | `/api/v1/downloads` | The caller's jobs. |
| `GET` | `/api/v1/downloads/{id}` | One job. |
| `GET` | `/api/v1/history` | What has actually been downloaded. |
| `GET` | `/api/v1/stats` | The same log as a dashboard: totals, rankings, activity. |
| `POST` | `/api/v1/csv/resolve` | A CSV of tracks → links. Downloads nothing. |
| `GET` | `/api/v1/subscriptions` | Followed artists. |
| `POST` | `/api/v1/subscriptions` | Follow one. **201**. |
| `DELETE` | `/api/v1/subscriptions/{id}` | Unfollow. **204**. |
| `POST` | `/api/v1/subscriptions/check` | Check every subscription for new releases. |
| `GET` | `/api/v1/extensions` | Installed extensions and how reliable they have been. |
| `POST` | `/api/v1/library/scan` | Files below a target quality (read-only). |

### Queueing a download

```bash
curl -X POST localhost:8000/api/v1/downloads \
     -H 'content-type: application/json' \
     -d '{"url":"https://open.spotify.com/album/…","quality":"LOSSLESS"}'
# {"id":"a1b2…","status":"queued","created_at":…}
```

A download takes minutes, so this answers **202 Accepted** with a job to poll
rather than holding the connection open — otherwise every client's timeout
would become the real limit on album size.

```bash
curl localhost:8000/api/v1/downloads/a1b2…
```

In multi-user mode a job is visible only to the account that submitted it, and
an id belonging to someone else answers **404**, not 403 — whether it exists
is not something to let an account probe for.

### The dashboard

```bash
curl 'localhost:8000/api/v1/stats?year=2026&top=5'
```

Totals, top artists/albums/genres, providers, formats, a month-by-month
timeline and activity (weekday, hour, streaks) for one period — `year=` or
`days=`, and neither for all of it. In multi-user mode it covers the calling
account's own downloads, never the instance's.

Every ranking that depends on a column added later carries its own coverage:

```json
{"top_genres": {"known": 812, "unknown": 244, "entries": [{"name": "Rock", "tracks": 300, "share": 0.37}]}}
```

`known + unknown` is the period's track count. A client should say so rather
than presenting `entries` as the whole picture: genre, release year and
duration are only recorded for downloads made since they were added to the
log, and a genre also needs metadata enrichment to have been on.

### Importing a CSV

```bash
curl -X POST localhost:8000/api/v1/csv/resolve \
     -H 'content-type: application/json' \
     -d '{"content":"Track Name,Artist Name(s)\nEverlong,Foo Fighters\n","name":"wishlist.csv"}'
```

The file's *contents*, not a path — a path would be a path on the server, and
the caller has no business naming one. Rows carrying a link keep it; rows
carrying only text are matched against the catalogue and scored, and anything
below `min_score` (0.62 by default) comes back under `unresolved` rather than
being guessed at.

Resolving is not downloading: the response is a list of URLs for the caller
to review and then queue through `POST /downloads` like any other download,
so quotas, the queue limit and per-account isolation all apply unchanged.

### Errors

One shape, always:

```json
{"error": "Could not resolve that URL.",
 "detail": "It may be private, unsupported, or not a media link."}
```

Validation failures are **422** with FastAPI's own field-level detail.
Internal messages are logged server-side and never put in a response body.

### What is deliberately not exposed

`file_path` — on downloads and in history alike. It is a path on the server:
useless to a remote caller and a disclosure of the host's directory layout.
Library scans are confined to the calling account's own download folder for
the same reason `/api/browse-folder` is.

---

## Quotas and roles (`--web-multiuser`)

Accounts used to be flat. There are now two roles, and optional per-account
limits.

```bash
spotiflac --web-user-add alice hunter2 --role admin
spotiflac --web-user-add bob   hunter2 --daily-tracks 100 --daily-mb 5000
spotiflac --web-user-list
spotiflac --web-user-quota bob --daily-tracks 50
spotiflac --web-user-role bob admin
```

`0` means unlimited, which is the default — an instance that never sets a
quota behaves exactly as it did before quotas existed, and files written by an
older version load unchanged.

Usage is a **rolling 24 hours**, counted from the download log rather than
from a separate counter: the number someone is refused on is the same number
their history shows them. Failed downloads do not consume quota.

| Endpoint | Who |
| --- | --- |
| `GET /api/quota/mine` | Any logged-in account, about itself. |
| `GET /api/admin/users` | Admin. Every account, with usage. |
| `POST /api/admin/quota` | Admin. `{"username":…, "daily_track_quota":…, "daily_byte_quota":…}` |
| `POST /api/admin/role` | Admin. `{"username":…, "role":"admin"\|"user"}` |
| `GET /api/admin/queue` | Admin. Every account's jobs. |
| `GET /api/metrics` | Single-user: anyone. Multi-user: admins only. |

The admin endpoints answer **404** to a non-admin rather than 403: whether an
admin API exists on this instance is not something an ordinary account needs
to learn.

The last admin cannot be demoted — the alternative is an instance nobody can
administer, recoverable only by hand-editing `web_users.json`.

> **Scope.** This is household or small-team separation, not hostile-tenant
> isolation. Accounts still run in one process as one OS user, and anyone who
> can install an extension can affect everyone.

---

## Durability

The queue, the download log and subscriptions live in a SQLite database
(`~/.spotiflac/spotiflac.db`, overridable with `$SPOTIFLAC_DB_PATH`).

In `--web-multiuser` the queue is persistent: a job queued before a restart is
picked up after it. A job that was *running* when the process died is re-queued
rather than assumed finished, so a download handler must tolerate running twice
for the same input — the download path does, because every provider skips a
track whose file is already on disk.
