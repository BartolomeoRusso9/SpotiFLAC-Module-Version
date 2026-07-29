# SpotiFLAC Telegram Bot — User Guide

A Telegram bot that downloads lossless music (FLAC) via Tidal, Qobuz,
Deezer and Amazon Music, organizes it automatically into `Artist/Album`
folders, keeps a searchable library, and walks you through every download
option with an interactive button wizard.

This guide covers everything you need to **run** it with Docker.

---

## 1. Requirements

- Docker (and Docker Compose, recommended)
- A Telegram bot token (see [Section 2](#2-create-your-telegram-bot))

---

## 2. Create your Telegram bot

1. Open Telegram and message **[@BotFather](https://t.me/BotFather)**.
2. Send `/newbot` and follow the prompts (choose a name and a username).
3. BotFather replies with a token that looks like:
   ```
   123456789:AAFQcbdZmBgZGHXBtspiLAbem-yuFualgvw
   ```
   Keep this secret — anyone with it can control your bot.
4. (Optional) Find your own numeric Telegram user ID by messaging
   **[@userinfobot](https://t.me/userinfobot)** — you'll need it if you want
   to restrict who can use the bot (see `ALLOWED_USER_IDS` below).

---

## 3. Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | **Yes** | — | Token from BotFather. The container will crash immediately without it. |
| `ALLOWED_USER_IDS` | No | *(unset = open to everyone)* | Comma-separated Telegram user IDs allowed to use the bot, e.g. `123456789,987654321`. |
| `BOT_DATA_DIR` | No | `/app/data` | Where the library database, config, and chat list are stored. **Must be a persistent volume.** |
| `DOWNLOAD_DIR` | No | `/app/downloads` | Where downloaded/organized music ends up. **Must be a persistent volume.** |
| `QOBUZ_AUTH_TOKEN` | No | — | Optional Qobuz account token, improves metadata reliability. |
| `QOBUZ_LOCAL_API_URL` | No | — | Optional self-hosted Qobuz API URL, if you run one. |
| `TIDAL_CUSTOM_API` | No | — | Optional self-hosted Tidal API URL for guaranteed availability. |

> ⚠️ **Security note:** without `ALLOWED_USER_IDS`, anyone who finds your bot
> on Telegram can use it — it will consume your server's bandwidth/disk and
> your Qobuz/Tidal API access. Set it unless you deliberately want a public
> bot.

---

## 4. docker-compose.yml (recommended)

```yaml
services:
  spotiflac-bot:
    image: ghcr.io/<your-username>/<your-repo>:latest
    container_name: spotiflac-bot
    restart: unless-stopped
    command: ["bot"]
    shm_size: "1gb"
    environment:
      TELEGRAM_BOT_TOKEN: "123456789:AAFQcbdZmBgZGHXBtspiLAbem-yuFualgvw"
      ALLOWED_USER_IDS: "123456789"
      # QOBUZ_AUTH_TOKEN: "your_token_here"
    volumes:
      - ./data:/app/data
      - ./downloads:/app/downloads   # or point this at your Jellyfin/Navidrome music folder
```

Start it:
```bash
docker compose up -d
```

View logs:
```bash
docker compose logs -f spotiflac-bot
```

You should see:
```
🤖 SpotiFLAC Bot started and listening...
```

---

## 5. docker run (alternative, no compose)

```bash
docker run -d \
  --name spotiflac-bot \
  --restart unless-stopped \
  --shm-size=1g \
  -e TELEGRAM_BOT_TOKEN="123456789:AAFQcbdZmBgZGHXBtspiLAbem-yuFualgvw" \
  -e ALLOWED_USER_IDS="123456789" \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/downloads:/app/downloads \
  ghcr.io/<your-username>/<your-repo>:latest \
  bot
```

> In **Portainer**, look for **"Command & logging" → Command** and set it
> to `bot` (single argument), keep the volumes/env vars as above.

---

## 6. Why the volumes matter

The bot uses **polling**, not webhooks — it reaches out to Telegram itself,
so **no ports need to be published**. But it does need two folders to
persist across restarts/updates:

| Path | Contains | What you lose without it |
|---|---|---|
| `/app/data` | Library database, config, chat list | Your searchable library history (`/search` stops working) and saved preferences |
| `/app/downloads` | Organized `Artist/Album/track.flac` files | All previously downloaded music (re-downloading everything is wasteful and slow) |

If you're running Jellyfin, Navidrome, or Plex, point `/app/downloads` at
the same folder your media server scans, so new downloads show up
automatically.

---

## 7. Using the bot

1. Open a chat with your bot on Telegram and send `/start`.
2. Paste a music link (Spotify, Tidal, Apple Music, etc.).
3. Follow the wizard: providers → extensions fallback → quality → quality
   fallback → track numbering / folder options → lyrics → metadata
   enrichment → retries → timeout → filename format → confirm.
4. Or skip the wizard entirely with quick flags:
   ```
   https://open.spotify.com/track/xxxx --service qobuz --quality LOSSLESS --no-lyrics
   ```

Other commands:
- `/search <text>` — search your already-downloaded library
- `/queue` — see what's downloading / queued
- `/cancel <id>` — remove a queued job
- `/stop` — clear the queue (the currently running job still finishes)

---

## 8. Updating the bot

When a new image version is published:

```bash
docker compose pull && docker compose up -d
```

(in Portainer: **Images → Pull** the new tag, then **Recreate** the
container with "Pull latest image" checked)

Your data and downloads are untouched since they live in the mounted
volumes, not inside the container.

---

## 9. Troubleshooting

| Symptom | Likely cause |
|---|---|
| Container exits immediately, log shows `Missing required environment variable: TELEGRAM_BOT_TOKEN` | Env var name typo — must be exactly `TELEGRAM_BOT_TOKEN` |
| Bot doesn't respond at all | Wrong/revoked token, or no outbound internet access from the container |
| `/search` returns nothing after a fresh container recreate | `/app/data` wasn't mounted as a volume — library was reset |
| Downloaded files disappear after updating the image | `/app/downloads` wasn't mounted as a volume |
| `PermissionError` when moving files | Host folder permissions don't allow the container's user to write — `chmod -R 775` the host folder or match the UID |
| "Conflict: terminated by other getUpdates request" | The same bot token is running in polling mode somewhere else too (e.g. locally on your PC) — stop the other instance |