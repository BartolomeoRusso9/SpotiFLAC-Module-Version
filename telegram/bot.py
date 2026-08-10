"""SpotiFLAC Telegram Bot -- unified, English, universal version.

Combines:
  - A full step-by-step wizard (mirrors SpotiFLAC's CLI interactive mode:
    services, extensions fallback, quality, quality fallback, track
    numbering, artist/album subfolders, lyrics, metadata enrichment,
    retries, timeout, filename format) plus a quick text-flag shortcut
    (e.g. "URL --service qobuz --quality LOSSLESS --no-lyrics")
  - Optional user allow-list (ALLOWED_USER_IDS) -- if unset, the bot is
    open to anyone who can message it
  - A persistent SQLite library with automatic Artist/Album
    organization and duplicate detection
  - A download queue with /queue, /cancel, /stop
  - A /search command to search the already-downloaded library

Download engine: uses AsyncSpotiFLAC as a Python library (not a CLI
subprocess), matching SpotiFLAC/client.py and providers/__init__.py.

Only Tidal, Qobuz, Deezer and Amazon Music are exposed as selectable
download providers (the most reliable / highest quality sources). This
does not affect the accepted input URL (Spotify, Tidal, Apple Music
links, etc. are still accepted as sources to identify the track).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import sqlite3
import time
import unicodedata
import uuid
from contextlib import closing
from pathlib import Path

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from SpotiFLAC import AsyncSpotiFLAC

# ─── CONFIGURATION ───────────────────────────────────────────────────


# Timestamped lines, because the bot's log is read long after the fact —
# usually to find out when a download started and how long it took.
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# One line per polled update, per HTTP request, per browser command: true, and
# useless. Everything these emit that matters is a warning or worse.
for _noisy in ("aiogram.event", "aiogram.dispatcher", "httpx", "httpcore", "pydoll"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

log = logging.getLogger("spotiflac.bot")


def _require_env(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise OSError(f"Missing required environment variable: {key}")
    return val


def _env_bool(key: str, default: bool = False) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


BOT_TOKEN = _require_env("TELEGRAM_BOT_TOKEN")

DATA_DIR = Path(os.getenv("BOT_DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/app/downloads")
CONFIG_PATH = str(DATA_DIR / "config.json")
DB_PATH = str(DATA_DIR / "library.db")
CHATIDS_PATH = str(DATA_DIR / "chat_ids.json")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Only these four providers are exposed as selectable download sources.
# (SpotiFLAC itself supports more, but these are the most reliable /
# highest quality ones -- restrict the bot's menu to just these.)
VALID_SERVICES = ["tidal", "qobuz", "deezer", "amazon"]

VALID_QUALITIES = ["LOSSLESS", "HI_RES", "HIGH", "DOLBY_ATMOS"]

# These are NOT restricted to the 4 audio providers above: lyrics and
# metadata enrichment use a different, independent set of sources.
LYRICS_PROVIDERS = ["spotify", "apple", "musixmatch", "lrclib", "amazon"]
ENRICH_PROVIDERS = ["deezer", "apple", "qobuz", "tidal", "soundcloud"]

SUPPORTED_DOMAINS = [
    "spotify.com",
    "open.spotify.com",
    "tidal.com",
    "listen.tidal.com",
    "music.apple.com",
    "apple.com",
    "soundcloud.com",
    "on.soundcloud.com",
    "youtube.com",
    "youtu.be",
    "music.youtube.com",
]

FILENAME_FORMAT_PRESETS = {
    "default": "{title} - {artist}",
    "artist_first": "{artist} - {title}",
    "with_track": "{track}. {title} - {artist}",
}

RETRY_PRESETS = [0, 1, 2, 3, 5]
TIMEOUT_PRESETS = [0, 60, 120, 300]

MAX_TELEGRAM_UPLOAD_BYTES = 50 * 1024 * 1024

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ─── AUTHORIZATION ────────────────────────────────────────────────────
# If ALLOWED_USER_IDS is not set, the bot is open to anyone -- this is
# the "universal" default. Set it to restrict access to specific
# Telegram user IDs (comma-separated).


def _allowed_user_ids() -> set[int] | None:
    raw = os.getenv("ALLOWED_USER_IDS", "").strip()
    if not raw:
        return None
    return {int(x.strip()) for x in raw.split(",") if x.strip()}


ALLOWED_USER_IDS = _allowed_user_ids()


def is_authorized(user_id: int) -> bool:
    return ALLOWED_USER_IDS is None or user_id in ALLOWED_USER_IDS


# ─── UTILITIES ─────────────────────────────────────────────────────────


def sanitize(name: str) -> str:
    if not name:
        return "Unknown"
    n = name.replace("/", "_").replace("\\", "_")
    n = n.replace(":", " -").replace("?", "").replace("*", "")
    n = n.replace('"', "'").replace("<", "(").replace(">", ")")
    n = n.replace("|", "-")
    n = re.sub(r"[\x00-\x1f]", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    if not n or re.fullmatch(r"[\s\-_.]+", n):
        return "Unknown"
    return n


def normalize(name: str) -> str:
    return unicodedata.normalize("NFC", name).lower().strip()


def format_artists(s: str) -> str:
    if not s:
        return ""
    artists = [a.strip() for a in re.split(r"[;,]\s*", s)]
    seen, unique = set(), []
    for a in artists:
        if a.lower() not in seen and a:
            seen.add(a.lower())
            unique.append(a)
    return "; ".join(unique)


def first_artist(artist_string: str) -> str:
    if not artist_string:
        return "Unknown Artist"
    for sep in ["; ", ", ", " & ", " feat. ", " ft. "]:
        parts = artist_string.split(sep)
        if len(parts) > 1:
            return sanitize(parts[0].strip())
    return sanitize(artist_string.strip())


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def he(s) -> str:
    """Escape text for Telegram HTML parse mode."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def save_chat_id(chat_id: int) -> None:
    ids = set()
    if os.path.exists(CHATIDS_PATH):
        try:
            with open(CHATIDS_PATH, "r") as f:
                ids = set(json.load(f))
        except Exception:
            pass
    ids.add(chat_id)
    try:
        with open(CHATIDS_PATH, "w") as f:
            json.dump(list(ids), f)
    except Exception as e:
        log.warning("Could not persist chat id %s: %s", chat_id, e)


def snapshot_audio_files() -> set[str]:
    extensions = (".flac", ".mp3", ".m4a", ".aac")
    files = set()
    try:
        for root, _, filenames in os.walk(DOWNLOAD_DIR):
            if "Duplicates" in root:
                continue
            for f in filenames:
                if f.lower().endswith(extensions):
                    files.add(os.path.join(root, f))
    except Exception as e:
        log.warning("Could not scan %s for audio files: %s", DOWNLOAD_DIR, e)
    return files


def collect_new_files(snapshot_before: set[str]) -> set[str]:
    return snapshot_audio_files() - snapshot_before


# ─── DATABASE (sync, called via asyncio.to_thread) ─────────────────────


def db_connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    with closing(db_connect()) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tracks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT NOT NULL,
                artist      TEXT,
                album       TEXT,
                albumartist TEXT,
                genre       TEXT,
                date        TEXT,
                format      TEXT,
                bitrate     TEXT,
                filepath    TEXT UNIQUE NOT NULL
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_title  ON tracks(title)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_artist ON tracks(artist)")
        conn.commit()


def db_upsert_track(tags, fmt, bitrate, filepath):
    try:
        with closing(db_connect()) as conn:
            conn.execute(
                """
                INSERT INTO tracks
                (title, artist, album, albumartist, genre, date, format, bitrate, filepath)
                VALUES (:title,:artist,:album,:albumartist,:genre,:date,:format,:bitrate,:filepath)
                ON CONFLICT(filepath) DO UPDATE SET
                    title=excluded.title, artist=excluded.artist, album=excluded.album,
                    albumartist=excluded.albumartist, genre=excluded.genre, date=excluded.date,
                    format=excluded.format, bitrate=excluded.bitrate
            """,
                {
                    "title": tags.get("title", ""),
                    "artist": tags.get("artist", ""),
                    "album": tags.get("album", ""),
                    "albumartist": tags.get("albumartist", ""),
                    "genre": tags.get("genre", ""),
                    "date": tags.get("date", ""),
                    "format": fmt,
                    "bitrate": bitrate,
                    "filepath": filepath,
                },
            )
            conn.commit()
    except Exception as e:
        log.warning("Library insert failed for %s: %s", filepath, e)


def db_search(query: str, limit: int = 15):
    q = f"%{query.lower()}%"
    try:
        with closing(db_connect()) as conn:
            return conn.execute(
                """
                SELECT * FROM tracks
                WHERE lower(title) LIKE ? OR lower(artist) LIKE ? OR lower(album) LIKE ?
                LIMIT ?
            """,
                (q, q, q, limit),
            ).fetchall()
    except Exception as e:
        log.warning("Library search failed for %r: %s", query, e)
        return []


# ─── TAG READING / FILE ORGANIZATION (sync) ────────────────────────────


def get_file_info(filepath):
    ext = os.path.splitext(filepath)[1].lower()
    fmt = ext.replace(".", "").upper()
    bitrate = "N/A"
    try:
        if ext == ".flac":
            from mutagen.flac import FLAC

            a = FLAC(filepath)
            if a.info.bits_per_sample and a.info.sample_rate:
                bitrate = f"{a.info.bits_per_sample}bit/{a.info.sample_rate // 1000}kHz"
        elif ext in (".m4a", ".aac"):
            from mutagen.mp4 import MP4

            a = MP4(filepath)
            if a.info.bitrate:
                bitrate = f"{a.info.bitrate // 1000}kbps"
        elif ext == ".mp3":
            from mutagen.mp3 import MP3

            a = MP3(filepath)
            if a.info.bitrate:
                bitrate = f"{a.info.bitrate // 1000}kbps"
    except Exception as e:
        log.warning("Could not read audio properties of %s: %s", filepath, e)
    return fmt, bitrate


def read_tags(filepath):
    t = {
        "title": "",
        "artist": "",
        "album": "",
        "albumartist": "",
        "genre": "",
        "date": "",
        "track": "",
    }
    ext = os.path.splitext(filepath)[1].lower()
    try:
        if ext == ".flac":
            from mutagen.flac import FLAC

            a = FLAC(filepath)
            t["title"] = a.get("title", [""])[0]
            t["artist"] = a.get("artist", [""])[0]
            t["album"] = a.get("album", [""])[0]
            t["albumartist"] = a.get("albumartist", [""])[0]
            t["genre"] = a.get("genre", [""])[0]
            t["date"] = a.get("date", [""])[0]
            t["track"] = a.get("tracknumber", [""])[0]
        elif ext in (".m4a", ".aac"):
            from mutagen.mp4 import MP4

            a = MP4(filepath)
            t["title"] = str(a.get("\xa9nam", [""])[0]) if a.get("\xa9nam") else ""
            t["artist"] = str(a.get("\xa9ART", [""])[0]) if a.get("\xa9ART") else ""
            t["album"] = str(a.get("\xa9alb", [""])[0]) if a.get("\xa9alb") else ""
            t["albumartist"] = str(a.get("aART", [""])[0]) if a.get("aART") else ""
            t["genre"] = str(a.get("\xa9gen", [""])[0]) if a.get("\xa9gen") else ""
            t["date"] = str(a.get("\xa9day", [""])[0]) if a.get("\xa9day") else ""
            if a.get("trkn"):
                t["track"] = str(a.get("trkn")[0][0])
        elif ext == ".mp3":
            from mutagen.id3 import ID3

            try:
                a = ID3(filepath)
            except Exception:
                a = ID3()

            def g(frame):
                return (
                    str(frame.text[0])
                    if frame and hasattr(frame, "text") and frame.text
                    else ""
                )

            t["title"] = g(a.get("TIT2"))
            t["artist"] = g(a.get("TPE1"))
            t["album"] = g(a.get("TALB"))
            t["albumartist"] = g(a.get("TPE2"))
            t["genre"] = g(a.get("TCON"))
            t["date"] = g(a.get("TDRC"))
            t["track"] = g(a.get("TRCK")).split("/")[0]
    except Exception as e:
        log.warning("Could not read tags of %s: %s", filepath, e)
    return t


def find_existing_folder(base_dir, folder_name):
    # base_dir may not exist yet if this is the first track for that
    # artist/album -- that's normal, not an error, so we don't log it.
    if not os.path.isdir(base_dir):
        return None
    try:
        target = normalize(folder_name)
        for entry in os.scandir(base_dir):
            if entry.is_dir() and normalize(entry.name) == target:
                return entry.path
    except Exception as e:
        log.warning("Could not list %s: %s", base_dir, e)
    return None


def build_filename(tags: dict, job_cfg: dict, fallback_name: str) -> str:
    """Build the target filename (without extension) from tags, honoring
    the job's filename_format / track-numbering options."""
    title = sanitize(tags.get("title") or fallback_name)
    artist = format_artists(tags.get("artist", "")) or "Unknown Artist"
    album = sanitize(tags.get("album", "Unknown Album"))
    album_artist = format_artists(tags.get("albumartist", "")) or artist

    if job_cfg.get("first_artist_only"):
        artist = first_artist(artist)
        album_artist = first_artist(album_artist)

    track_num = ""
    raw_track = (tags.get("track") or "").strip()
    if raw_track.isdigit():
        track_num = raw_track.zfill(2)

    fmt = job_cfg.get("filename_format") or FILENAME_FORMAT_PRESETS["default"]
    name = fmt.format(
        title=title,
        artist=artist,
        album=album,
        album_artist=album_artist,
        track=track_num or "00",
    )

    if job_cfg.get("use_track_numbers") and track_num:
        prefix = track_num if job_cfg.get("use_album_track_numbers") else track_num
        if not name.startswith(prefix):
            name = f"{prefix}. {name}"

    return sanitize(name)


def organize_file(filepath: str, job_cfg: dict):
    if not os.path.exists(filepath):
        return None

    tags = read_tags(filepath)
    artist_raw = tags.get("artist", "")
    album = sanitize(tags.get("album", "Unknown Album"))
    raw_aa = tags.get("albumartist", "").strip()

    album_artist = format_artists(raw_aa) if raw_aa else ""
    artist = format_artists(artist_raw) if artist_raw else ""
    folder_artist = first_artist(album_artist or artist) or "Unknown Artist"

    base_dir = DOWNLOAD_DIR
    if job_cfg.get("use_artist_subfolders", True):
        artist_dir = find_existing_folder(base_dir, folder_artist) or os.path.join(
            base_dir, folder_artist
        )
    else:
        artist_dir = base_dir

    if job_cfg.get("use_album_subfolders", True):
        album_dir = find_existing_folder(artist_dir, album) or os.path.join(
            artist_dir, album
        )
    else:
        album_dir = artist_dir

    os.makedirs(album_dir, exist_ok=True)

    ext = os.path.splitext(filepath)[1].lower()
    original_name = os.path.splitext(os.path.basename(filepath))[0]
    if not ext:
        return None

    new_fname = build_filename(tags, job_cfg, original_name) + ext
    if new_fname in (f"Unknown{ext}", ext, f"- {ext}"):
        new_fname = sanitize(original_name) + ext or f"track_{int(time.time())}{ext}"

    dest = os.path.join(album_dir, new_fname)

    if os.path.exists(dest):
        if os.path.getsize(filepath) == os.path.getsize(dest):
            os.remove(filepath)
            return ("duplicate_exact", new_fname, dest, tags)
        dup_dir = os.path.join(DOWNLOAD_DIR, "Duplicates", folder_artist, album)
        os.makedirs(dup_dir, exist_ok=True)
        dup_dest = os.path.join(dup_dir, new_fname)
        if os.path.exists(dup_dest):
            base, e2 = os.path.splitext(dup_dest)
            dup_dest = f"{base}_{int(time.time())}{e2}"
        shutil.move(dest, dup_dest)
        shutil.move(filepath, dest)
        return ("duplicate_different", new_fname, dest, tags)

    shutil.move(filepath, dest)
    return ("ok", new_fname, dest, tags)


def cleanup_empty_dirs():
    for dirpath, _, _ in os.walk(DOWNLOAD_DIR, topdown=False):
        if dirpath == DOWNLOAD_DIR or "Duplicates" in dirpath:
            continue
        if not os.listdir(dirpath):
            try:
                os.rmdir(dirpath)
            except Exception:
                pass


def process_new_files(files_before: set[str], job_cfg: dict):
    """Organize new audio files and register them in the DB. Sync
    function, must be called via asyncio.to_thread."""
    new_files = collect_new_files(files_before)
    organized, duplicates, errors = [], [], []

    for f in new_files:
        try:
            res = organize_file(f, job_cfg)
        except Exception as e:
            errors.append(f"{os.path.basename(f)} ({e})")
            continue
        if res is None:
            errors.append(os.path.basename(f))
            continue
        status, name, dest, _old_tags = res
        fmt, bitrate = get_file_info(dest)
        fresh_tags = read_tags(dest)
        if status == "ok":
            organized.append((name, dest))
            db_upsert_track(fresh_tags, fmt, bitrate, dest)
        elif status == "duplicate_exact":
            duplicates.append(name)
        elif status == "duplicate_different":
            duplicates.append(f"{name} (previous file moved to Duplicates)")
            db_upsert_track(fresh_tags, fmt, bitrate, dest)

    cleanup_empty_dirs()
    return organized, duplicates, errors


# ─── DOWNLOAD ENGINE (AsyncSpotiFLAC as a library) ─────────────────────


async def run_spotiflac_once(url: str, job_cfg: dict) -> None:
    qobuz_token = os.getenv("QOBUZ_AUTH_TOKEN") or None
    qobuz_local_api = os.getenv("QOBUZ_LOCAL_API_URL") or None
    tidal_custom_api = os.getenv("TIDAL_CUSTOM_API") or None

    kwargs = dict(
        output_dir=DOWNLOAD_DIR,
        services=job_cfg["services"],
        quality=job_cfg["quality"],
        allow_fallback=job_cfg.get("allow_fallback", True),
        embed_lyrics=job_cfg.get("embed_lyrics", True),
        enrich_metadata=job_cfg.get("enrich_metadata", True),
        qobuz_token=qobuz_token,
    )
    if job_cfg.get("embed_lyrics") and job_cfg.get("lyrics_providers"):
        kwargs["lyrics_providers"] = job_cfg["lyrics_providers"]
    if job_cfg.get("enrich_metadata") and job_cfg.get("enrich_providers"):
        kwargs["enrich_providers"] = job_cfg["enrich_providers"]
    if "use_extensions_fallback" in job_cfg:
        kwargs["use_extensions_fallback"] = job_cfg["use_extensions_fallback"]
    if job_cfg.get("transcode_to"):
        kwargs["transcode_to"] = job_cfg["transcode_to"]
        kwargs["transcode_bitrate"] = job_cfg.get("transcode_bitrate", "320k")
        kwargs["transcode_keep_original"] = job_cfg.get(
            "transcode_keep_original", False
        )
    if qobuz_local_api:
        kwargs["qobuz_local_api_url"] = qobuz_local_api
    if tidal_custom_api:
        kwargs["tidal_custom_api"] = tidal_custom_api

    try:
        async with AsyncSpotiFLAC(**kwargs) as client:
            await client.download_track(url)
    except TypeError:
        # Older/newer SpotiFLAC versions may not accept every optional
        # kwarg above -- fall back to the minimal, always-supported set.
        core_kwargs = dict(
            output_dir=DOWNLOAD_DIR,
            services=job_cfg["services"],
            quality=job_cfg["quality"],
            allow_fallback=job_cfg.get("allow_fallback", True),
            embed_lyrics=job_cfg.get("embed_lyrics", True),
            enrich_metadata=job_cfg.get("enrich_metadata", True),
            qobuz_token=qobuz_token,
        )
        async with AsyncSpotiFLAC(**core_kwargs) as client:
            await client.download_track(url)


async def run_spotiflac(url: str, job_cfg: dict) -> None:
    """Run a download, applying the job's retry/timeout settings."""
    max_retries = job_cfg.get("track_max_retries", 0)
    timeout_s = job_cfg.get("timeout_s", 0)

    attempts = max_retries + 1
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            if timeout_s:
                await asyncio.wait_for(
                    run_spotiflac_once(url, job_cfg), timeout=timeout_s
                )
            else:
                await run_spotiflac_once(url, job_cfg)
            return
        except Exception as e:
            last_error = e
            if attempt < attempts:
                continue
    if last_error:
        raise last_error


# ─── DOWNLOAD QUEUE ─────────────────────────────────────────────────────

download_queue: asyncio.Queue = asyncio.Queue()
_queue_list: list[dict] = []
_queue_id_counter = 0
current_task: dict | None = None


def _next_queue_id() -> int:
    global _queue_id_counter
    _queue_id_counter += 1
    return _queue_id_counter


async def safe_edit(chat_id: int, message_id: int, text: str):
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            log.warning(
                "Could not edit message %s in chat %s: %s", message_id, chat_id, e
            )


async def send_result(chat_id: int, message_id: int, organized, duplicates, errors):
    lines = ["🎵 <b>Download complete!</b>\n"]
    if organized:
        lines.append(f"<b>✅ Downloaded ({len(organized)}):</b>")
        for name, _ in organized:
            lines.append(f"  • <code>{he(name)}</code>")
        lines.append("")
    if duplicates:
        lines.append(f"<b>♻️ Already in library ({len(duplicates)}):</b>")
        for name in duplicates:
            lines.append(f"  • <code>{he(name)}</code>")
        lines.append("")
    if errors:
        lines.append(f"<b>⚠️ Errors ({len(errors)}):</b>")
        for name in errors:
            lines.append(f"  • <code>{he(name)}</code>")

    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass
    await bot.send_message(chat_id, "\n".join(lines)[:4000])

    for name, path in organized:
        size = os.path.getsize(path)
        if size > MAX_TELEGRAM_UPLOAD_BYTES:
            await bot.send_message(
                chat_id,
                f"'{he(name)}' is {size / 1024 / 1024:.1f} MB, too large for Telegram (50 MB limit).",
            )
            continue
        try:
            await bot.send_audio(chat_id=chat_id, audio=FSInputFile(path))
        except Exception as e:
            await bot.send_message(chat_id, f"Could not send '{he(name)}': {he(e)}")


async def download_worker():
    global current_task
    while True:
        job = await download_queue.get()
        _queue_list[:] = [x for x in _queue_list if x["id"] != job["id"]]
        current_task = job

        started = time.monotonic()
        log.info(
            "Job %s started: %s (%s, %s, %s left in queue)",
            job["id"],
            job["url"],
            "/".join(job.get("services") or ["?"]),
            job.get("quality", "?"),
            download_queue.qsize(),
        )

        try:
            await safe_edit(
                job["chat_id"], job["message_id"], "⏳ <b>Starting download...</b>"
            )
            files_before = await asyncio.to_thread(snapshot_audio_files)

            await run_spotiflac(job["url"], job)

            organized, duplicates, errors = await asyncio.to_thread(
                process_new_files,
                files_before,
                job,
            )

            log.info(
                "Job %s finished in %.1fs: %s new, %s already in library, %s error(s)",
                job["id"],
                time.monotonic() - started,
                len(organized),
                len(duplicates),
                len(errors),
            )
            for _name, dest in organized:
                log.info("Job %s stored %s", job["id"], dest)
            for name in errors:
                log.warning("Job %s could not organize %s", job["id"], name)

            if not organized and not duplicates:
                log.warning(
                    "Job %s produced no file — check the provider logs above for %s",
                    job["id"],
                    job["url"],
                )
                await safe_edit(
                    job["chat_id"],
                    job["message_id"],
                    "❌ <b>Download failed:</b> no file was downloaded.",
                )
            else:
                await send_result(
                    job["chat_id"], job["message_id"], organized, duplicates, errors
                )

        except Exception as e:
            log.exception(
                "Job %s failed after %.1fs", job["id"], time.monotonic() - started
            )
            await safe_edit(
                job["chat_id"], job["message_id"], f"❌ Error: {he(str(e))}"
            )
        finally:
            current_task = None
            download_queue.task_done()
            if download_queue.empty():
                log.info("Queue empty, waiting for new jobs")


async def enqueue_job(chat_id: int, message_id: int, job_cfg: dict) -> int:
    qid = _next_queue_id()
    job = {"id": qid, "chat_id": chat_id, "message_id": message_id, **job_cfg}
    _queue_list.append(job)
    await download_queue.put(job)
    position = download_queue.qsize()
    log.info(
        "Job %s queued at position %s by chat %s: %s",
        qid,
        position,
        chat_id,
        job_cfg.get("url", "?"),
    )
    if current_task is not None or position > 1:
        await safe_edit(
            chat_id, message_id, f"⏳ Added to queue (position #{position}, ID {qid})."
        )
    return qid


# ─── QUICK TEXT FLAGS (shortcut, skips the wizard) ─────────────────────


def parse_quick_flags(args: list[str]) -> dict:
    """E.g. ['--service', 'qobuz', '--quality', 'LOSSLESS', '--no-lyrics']"""
    cfg = {}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--service" and i + 1 < len(args):
            val = args[i + 1].lower()
            if val in VALID_SERVICES:
                cfg["service"] = val
            i += 2
        elif a == "--quality" and i + 1 < len(args):
            val = args[i + 1].upper()
            if val in VALID_QUALITIES:
                cfg["quality"] = val
            i += 2
        elif a == "--no-lyrics":
            cfg["embed_lyrics"] = False
            i += 1
        elif a == "--mp3":
            cfg["transcode_to"] = "mp3"
            i += 1
        elif a == "--transcode-bitrate" and i + 1 < len(args):
            cfg["transcode_bitrate"] = args[i + 1].lower()
            i += 2
        elif a == "--keep-original":
            cfg["transcode_keep_original"] = True
            i += 1
        else:
            i += 1
    return cfg


def default_job_cfg(url: str) -> dict:
    """Default values for every wizard-configurable option -- used both
    as the wizard's starting point and as the quick-flag fallback."""
    return {
        "url": url,
        "services": ["tidal"],
        "use_extensions_fallback": True,
        "quality": "LOSSLESS",
        "allow_fallback": True,
        "use_track_numbers": False,
        "use_album_track_numbers": False,
        "use_artist_subfolders": True,
        "use_album_subfolders": True,
        "first_artist_only": False,
        "embed_lyrics": True,
        "lyrics_providers": ["lrclib", "apple", "amazon"],
        "enrich_metadata": True,
        "enrich_providers": ["deezer", "apple", "qobuz", "tidal", "soundcloud"],
        "track_max_retries": 0,
        "timeout_s": 0,
        "transcode_to": os.getenv("TRANSCODE_TO") or None,
        "transcode_bitrate": os.getenv("TRANSCODE_BITRATE") or "320k",
        "transcode_keep_original": _env_bool("TRANSCODE_KEEP_ORIGINAL"),
        "filename_format": FILENAME_FORMAT_PRESETS["default"],
    }


# ─── WIZARD KEYBOARDS ───────────────────────────────────────────────────

user_sessions: dict[str, dict] = {}


def yn_keyboard(prefix: str, task_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Yes", callback_data=f"{prefix}|{task_id}|yes"
                ),
                InlineKeyboardButton(
                    text="❌ No", callback_data=f"{prefix}|{task_id}|no"
                ),
            ]
        ]
    )


SERVICE_LABELS = {
    "tidal": "🌊 Tidal",
    "qobuz": "💽 Qobuz",
    "deezer": "🎵 Deezer",
    "amazon": "📦 Amazon",
}


def service_keyboard(task_id: str, selected: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for svc in VALID_SERVICES:
        mark = f" ({selected.index(svc) + 1})" if svc in selected else ""
        label = ("✅ " if svc in selected else "") + SERVICE_LABELS[svc] + mark
        rows.append(
            [
                InlineKeyboardButton(
                    text=label, callback_data=f"svc_toggle|{task_id}|{svc}"
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="🌐 Select all 4", callback_data=f"svc_all|{task_id}"
            )
        ]
    )
    rows.append(
        [InlineKeyboardButton(text="➡️ Done", callback_data=f"svc_done|{task_id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def quality_keyboard(task_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💎 LOSSLESS (FLAC 16-bit)",
                    callback_data=f"qual|{task_id}|LOSSLESS",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✨ HI-RES (FLAC 24-bit)",
                    callback_data=f"qual|{task_id}|HI_RES",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎧 HIGH (MP3 320 / AAC)", callback_data=f"qual|{task_id}|HIGH"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔊 DOLBY ATMOS (Tidal only)",
                    callback_data=f"qual|{task_id}|DOLBY_ATMOS",
                )
            ],
        ]
    )


def multi_provider_keyboard(
    prefix: str, task_id: str, options: list[str], selected: list[str]
) -> InlineKeyboardMarkup:
    rows = []
    for opt in options:
        mark = f" ({selected.index(opt) + 1})" if opt in selected else ""
        label = ("✅ " if opt in selected else "") + opt.capitalize() + mark
        rows.append(
            [
                InlineKeyboardButton(
                    text=label, callback_data=f"{prefix}_toggle|{task_id}|{opt}"
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="➡️ Done", callback_data=f"{prefix}_done|{task_id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def retries_keyboard(task_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=str(n), callback_data=f"retries|{task_id}|{n}"
                )
                for n in RETRY_PRESETS
            ],
        ]
    )


def timeout_keyboard(task_id: str) -> InlineKeyboardMarkup:
    labels = {0: "Disabled", 60: "60s", 120: "120s", 300: "300s"}
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=labels[n], callback_data=f"timeout|{task_id}|{n}"
                )
                for n in TIMEOUT_PRESETS
            ],
        ]
    )


def filename_keyboard(task_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="{title} - {artist}", callback_data=f"fmt|{task_id}|default"
                )
            ],
            [
                InlineKeyboardButton(
                    text="{artist} - {title}",
                    callback_data=f"fmt|{task_id}|artist_first",
                )
            ],
            [
                InlineKeyboardButton(
                    text="{track}. {title} - {artist}",
                    callback_data=f"fmt|{task_id}|with_track",
                )
            ],
        ]
    )


def services_label(services: list[str]) -> str:
    return " → ".join(s.capitalize() for s in services)


# ─── WIZARD STEP TRANSITIONS ────────────────────────────────────────────
# Each step edits the current message text + keyboard to move to the
# next question. Sessions are keyed by a short random task_id.


async def goto_extensions_fallback(call: types.CallbackQuery, task_id: str):
    session = user_sessions[task_id]
    await call.message.edit_text(
        f"🔗 <b>URL:</b> <code>{he(session['url'])}</code>\n"
        f"✅ <b>Services:</b> {services_label(session['services'])}\n\n"
        f"🧩 <b>2. Use installed extensions as automatic fallback?</b>",
        reply_markup=yn_keyboard("ext", task_id),
        disable_web_page_preview=True,
    )


async def goto_quality(call: types.CallbackQuery, task_id: str):
    session = user_sessions[task_id]
    await call.message.edit_text(
        f"🔗 <b>URL:</b> <code>{he(session['url'])}</code>\n"
        f"✅ <b>Services:</b> {services_label(session['services'])}\n"
        f"✅ <b>Extensions fallback:</b> {'Yes' if session['use_extensions_fallback'] else 'No'}\n\n"
        f"📈 <b>3. Select audio quality:</b>",
        reply_markup=quality_keyboard(task_id),
        disable_web_page_preview=True,
    )


async def goto_quality_fallback(call: types.CallbackQuery, task_id: str):
    session = user_sessions[task_id]
    await call.message.edit_text(
        f"✅ <b>Quality:</b> <code>{session['quality']}</code>\n\n"
        f"🔁 <b>4. Allow automatic quality fallback</b> (try a lower quality if the "
        f"selected one isn't available)?",
        reply_markup=yn_keyboard("qualfb", task_id),
    )


async def goto_track_numbers(call: types.CallbackQuery, task_id: str):
    await call.message.edit_text(
        "🔢 <b>5. Add track number to filename?</b>",
        reply_markup=yn_keyboard("tracknum", task_id),
    )


async def goto_album_track_numbers(call: types.CallbackQuery, task_id: str):
    await call.message.edit_text(
        "🔢 <b>5b. Use the track's original album track number?</b>",
        reply_markup=yn_keyboard("albumtracknum", task_id),
    )


async def goto_artist_subfolders(call: types.CallbackQuery, task_id: str):
    await call.message.edit_text(
        "📁 <b>5b. Create an artist subfolder?</b>",
        reply_markup=yn_keyboard("artistsub", task_id),
    )


async def goto_album_subfolders(call: types.CallbackQuery, task_id: str):
    await call.message.edit_text(
        "📁 <b>5c. Create an album subfolder (inside the artist folder)?</b>",
        reply_markup=yn_keyboard("albumsub", task_id),
    )


async def goto_first_artist_only(call: types.CallbackQuery, task_id: str):
    await call.message.edit_text(
        '👤 <b>5d. Use only the first artist</b> in tags and filename (e.g. "Artist A" '
        'instead of "Artist A, Artist B")?',
        reply_markup=yn_keyboard("firstartist", task_id),
    )


async def goto_lyrics(call: types.CallbackQuery, task_id: str):
    await call.message.edit_text(
        "📝 <b>6. Embed synchronized lyrics?</b>",
        reply_markup=yn_keyboard("lyr", task_id),
    )


async def goto_lyrics_providers(call: types.CallbackQuery, task_id: str):
    session = user_sessions[task_id]
    await call.message.edit_text(
        "📝 <b>6b. Lyrics providers</b> (tap in priority order, then Done):",
        reply_markup=multi_provider_keyboard(
            "lyrp", task_id, LYRICS_PROVIDERS, session["lyrics_providers"]
        ),
    )


async def goto_enrichment(call: types.CallbackQuery, task_id: str):
    await call.message.edit_text(
        "🧬 <b>7. Enable metadata enrichment</b> (high-res covers, BPM, labels, etc.)?",
        reply_markup=yn_keyboard("enr", task_id),
    )


async def goto_enrich_providers(call: types.CallbackQuery, task_id: str):
    session = user_sessions[task_id]
    await call.message.edit_text(
        "🧬 <b>7b. Enrichment providers</b> (tap in priority order, then Done):",
        reply_markup=multi_provider_keyboard(
            "enrp", task_id, ENRICH_PROVIDERS, session["enrich_providers"]
        ),
    )


async def goto_retries(call: types.CallbackQuery, task_id: str):
    await call.message.edit_text(
        "🔄 <b>8. Extra retries per track</b> if a download fails (0 = no retry):",
        reply_markup=retries_keyboard(task_id),
    )


async def goto_timeout(call: types.CallbackQuery, task_id: str):
    await call.message.edit_text(
        "⏱️ <b>9. Timeout per track</b> (0 = disabled):",
        reply_markup=timeout_keyboard(task_id),
    )


async def goto_filename_format(call: types.CallbackQuery, task_id: str):
    await call.message.edit_text(
        "🏷️ <b>10. Filename format:</b>",
        reply_markup=filename_keyboard(task_id),
    )


def summary_text(session: dict) -> str:
    lines = ["📋 <b>Ready to download</b>\n"]
    lines.append(f"🔗 URL: <code>{he(session['url'])}</code>")
    lines.append(f"Services: {services_label(session['services'])}")
    lines.append(
        f"Extensions fallback: {'Yes' if session['use_extensions_fallback'] else 'No'}"
    )
    lines.append(
        f"Quality: <code>{session['quality']}</code> (fallback: {'Yes' if session['allow_fallback'] else 'No'})"
    )
    if session["use_track_numbers"]:
        lines.append(
            f"Track numbers: Yes (album numbers: {'Yes' if session['use_album_track_numbers'] else 'No'})"
        )
    else:
        lines.append(
            f"Artist subfolder: {'Yes' if session['use_artist_subfolders'] else 'No'}"
        )
        lines.append(
            f"Album subfolder: {'Yes' if session['use_album_subfolders'] else 'No'}"
        )
        lines.append(
            f"First artist only: {'Yes' if session['first_artist_only'] else 'No'}"
        )
    lines.append(
        f"Lyrics: {'Yes (' + ', '.join(session['lyrics_providers']) + ')' if session['embed_lyrics'] else 'No'}"
    )
    lines.append(
        f"Enrichment: {'Yes (' + ', '.join(session['enrich_providers']) + ')' if session['enrich_metadata'] else 'No'}"
    )
    lines.append(
        f"Retries: {session['track_max_retries']} | Timeout: {session['timeout_s'] or 'disabled'}"
    )
    lines.append(f"Filename format: <code>{he(session['filename_format'])}</code>")
    return "\n".join(lines)


async def goto_confirm(call: types.CallbackQuery, task_id: str):
    session = user_sessions[task_id]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚀 Start download", callback_data=f"confirm|{task_id}|yes"
                ),
                InlineKeyboardButton(
                    text="❌ Cancel", callback_data=f"confirm|{task_id}|no"
                ),
            ]
        ]
    )
    await call.message.edit_text(summary_text(session), reply_markup=kb)


# ─── COMMANDS ────────────────────────────────────────────────────────────


@dp.message(Command("start", "help"))
async def cmd_start(message: types.Message):
    if not is_authorized(message.from_user.id):
        await message.answer("⛔ You are not authorized to use this bot.")
        return
    save_chat_id(message.chat.id)
    await message.answer(
        "🎧 <b>SpotiFLAC Bot</b>\n\n"
        "Send me a music link (Spotify, Tidal, Apple Music, etc.) and I'll walk you "
        "through a full menu to configure provider, quality, lyrics, organization and more.\n\n"
        "Or skip the wizard with quick flags, e.g.:\n"
        "<code>https://... --service qobuz --quality LOSSLESS --no-lyrics</code>\n\n"
        "<b>Commands:</b>\n"
        "• /search [text] — search the already-downloaded library\n"
        "• /queue — show the download queue\n"
        "• /cancel [id] — remove a job from the queue\n"
        "• /stop — clear the queue (the running job still finishes)"
    )


@dp.message(Command("search"))
async def cmd_search(message: types.Message):
    if not is_authorized(message.from_user.id):
        return
    query = message.text.replace("/search", "", 1).strip()
    if not query:
        await message.answer("🔍 Usage: <code>/search title, artist or album</code>")
        return
    rows = await asyncio.to_thread(db_search, query)
    if not rows:
        await message.answer(f"❌ No results for: <b>{he(query)}</b>")
        return
    lines = [f'🔍 <b>Results for "{he(query)}"</b> ({len(rows)}):\n']
    for i, row in enumerate(rows[:25], 1):
        title = row["title"] or "?"
        artist = (row["artist"] or "").split(",")[0].strip()
        album = row["album"] or "Single/Unknown"
        fmt = row["format"] or "?"
        br = row["bitrate"] or ""
        lines.append(
            f"{i}. <b>{he(title)}</b> — {he(artist)}\n   💿 <i>{he(album)}</i> [{fmt} {br}]"
        )
    await message.answer("\n\n".join(lines))


@dp.message(Command("queue"))
async def cmd_queue(message: types.Message):
    if not is_authorized(message.from_user.id):
        return
    if not _queue_list and current_task is None:
        await message.answer("✅ The queue is empty.")
        return
    lines = ["📋 <b>Download status</b>\n"]
    if current_task is not None:
        lines.append(f"▶️ Running: <code>{he(current_task['url'])}</code>\n")
    if _queue_list:
        lines.append("⏸️ Waiting:")
        for item in _queue_list:
            lines.append(f"   [ID {item['id']}] <code>{he(item['url'])}</code>")
    await message.answer("\n".join(lines), disable_web_page_preview=True)


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message):
    if not is_authorized(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("❌ Usage: <code>/cancel ID_NUMBER</code>")
        return
    target_id = int(parts[1])
    before = len(_queue_list)
    _queue_list[:] = [x for x in _queue_list if x["id"] != target_id]
    if len(_queue_list) < before:
        await message.answer(f"✅ ID {target_id} removed from the queue.")
    else:
        await message.answer(
            f"❌ No ID {target_id} in the queue (note: the internal asyncio.Queue isn't "
            f"cleared until processed, but the job will be discarded automatically)."
        )


@dp.message(Command("stop"))
async def cmd_stop(message: types.Message):
    if not is_authorized(message.from_user.id):
        return
    if current_task is None:
        await message.answer(
            "Nothing is downloading right now (note: a running job can't be interrupted mid-way, only queued ones)."
        )
        return
    _queue_list.clear()
    await message.answer(
        "🛑 Queue cleared. The current download will finish normally (it can't be interrupted mid-way)."
    )


# ─── MESSAGES (links + wizard) ──────────────────────────────────────────


@dp.message()
async def handle_message(message: types.Message):
    if not is_authorized(message.from_user.id):
        return
    save_chat_id(message.chat.id)
    text = (message.text or "").strip()
    if not text:
        return

    try:
        args = shlex.split(text)
    except ValueError as e:
        await message.answer(f"⚠️ Syntax error: {he(e)}")
        return
    if not args:
        return

    url = next((a for a in args if a.startswith("http")), "")
    if not url or not any(d in url.lower() for d in SUPPORTED_DOMAINS):
        await message.answer(
            "❌ Send me a valid music link (Spotify, Tidal, Apple Music, etc.)."
        )
        return

    flags = [a for a in args if a != url]
    quick_cfg = parse_quick_flags(flags)
    saved_cfg = load_config()

    if quick_cfg:
        job_cfg = default_job_cfg(url)
        job_cfg["services"] = [
            quick_cfg.get("service", saved_cfg.get("default_service", "tidal"))
        ]
        job_cfg["quality"] = quick_cfg.get(
            "quality", saved_cfg.get("default_quality", "LOSSLESS")
        )
        job_cfg["embed_lyrics"] = quick_cfg.get(
            "embed_lyrics", saved_cfg.get("embed_lyrics", True)
        )
        msg = await message.answer("⚡ Quick start with the given flags...")
        await enqueue_job(message.chat.id, msg.message_id, job_cfg)
        return

    # Otherwise: full wizard
    task_id = str(uuid.uuid4())[:8]
    session = default_job_cfg(url)
    session["services"] = []
    user_sessions[task_id] = session
    await message.answer(
        f"🔗 <b>URL:</b> <code>{he(url)}</code>\n\n🎵 <b>1. Select download provider(s)</b> "
        f"(tap in priority order, then Done):",
        reply_markup=service_keyboard(task_id, session["services"]),
        disable_web_page_preview=True,
    )


# ─── CALLBACK HANDLERS ───────────────────────────────────────────────────


def _session_or_expired(call_data: str) -> tuple[str, list[str], str] | None:
    return None  # placeholder, not used


@dp.callback_query(F.data.startswith("svc_toggle|"))
async def cb_svc_toggle(call: types.CallbackQuery):
    if not is_authorized(call.from_user.id):
        await call.answer("⛔ Not authorized.", show_alert=True)
        return
    _, task_id, svc = call.data.split("|")
    session = user_sessions.get(task_id)
    if session is None:
        await call.answer("Session expired, send the link again.", show_alert=True)
        return
    if svc in session["services"]:
        session["services"].remove(svc)
    else:
        session["services"].append(svc)
    await call.message.edit_reply_markup(
        reply_markup=service_keyboard(task_id, session["services"])
    )
    await call.answer()


@dp.callback_query(F.data.startswith("svc_all|"))
async def cb_svc_all(call: types.CallbackQuery):
    if not is_authorized(call.from_user.id):
        await call.answer("⛔ Not authorized.", show_alert=True)
        return
    _, task_id = call.data.split("|")
    session = user_sessions.get(task_id)
    if session is None:
        await call.answer("Session expired.", show_alert=True)
        return
    session["services"] = list(VALID_SERVICES)
    await call.message.edit_reply_markup(
        reply_markup=service_keyboard(task_id, session["services"])
    )
    await call.answer()


@dp.callback_query(F.data.startswith("svc_done|"))
async def cb_svc_done(call: types.CallbackQuery):
    if not is_authorized(call.from_user.id):
        await call.answer("⛔ Not authorized.", show_alert=True)
        return
    _, task_id = call.data.split("|")
    session = user_sessions.get(task_id)
    if session is None:
        await call.answer("Session expired.", show_alert=True)
        return
    if not session["services"]:
        await call.answer("Select at least one provider.", show_alert=True)
        return
    await goto_extensions_fallback(call, task_id)
    await call.answer()


@dp.callback_query(F.data.startswith("ext|"))
async def cb_ext(call: types.CallbackQuery):
    _, task_id, val = call.data.split("|")
    session = user_sessions.get(task_id)
    if session is None:
        await call.answer("Session expired.", show_alert=True)
        return
    session["use_extensions_fallback"] = val == "yes"
    await goto_quality(call, task_id)
    await call.answer()


@dp.callback_query(F.data.startswith("qual|"))
async def cb_quality(call: types.CallbackQuery):
    _, task_id, quality = call.data.split("|")
    session = user_sessions.get(task_id)
    if session is None:
        await call.answer("Session expired.", show_alert=True)
        return
    session["quality"] = quality
    await goto_quality_fallback(call, task_id)
    await call.answer()


@dp.callback_query(F.data.startswith("qualfb|"))
async def cb_quality_fallback(call: types.CallbackQuery):
    _, task_id, val = call.data.split("|")
    session = user_sessions.get(task_id)
    if session is None:
        await call.answer("Session expired.", show_alert=True)
        return
    session["allow_fallback"] = val == "yes"
    await goto_track_numbers(call, task_id)
    await call.answer()


@dp.callback_query(F.data.startswith("tracknum|"))
async def cb_track_numbers(call: types.CallbackQuery):
    _, task_id, val = call.data.split("|")
    session = user_sessions.get(task_id)
    if session is None:
        await call.answer("Session expired.", show_alert=True)
        return
    session["use_track_numbers"] = val == "yes"
    if session["use_track_numbers"]:
        session["use_artist_subfolders"] = False
        session["use_album_subfolders"] = False
        session["first_artist_only"] = False
        await goto_album_track_numbers(call, task_id)
    else:
        session["use_album_track_numbers"] = False
        await goto_artist_subfolders(call, task_id)
    await call.answer()


@dp.callback_query(F.data.startswith("albumtracknum|"))
async def cb_album_track_numbers(call: types.CallbackQuery):
    _, task_id, val = call.data.split("|")
    session = user_sessions.get(task_id)
    if session is None:
        await call.answer("Session expired.", show_alert=True)
        return
    session["use_album_track_numbers"] = val == "yes"
    await goto_lyrics(call, task_id)
    await call.answer()


@dp.callback_query(F.data.startswith("artistsub|"))
async def cb_artist_subfolders(call: types.CallbackQuery):
    _, task_id, val = call.data.split("|")
    session = user_sessions.get(task_id)
    if session is None:
        await call.answer("Session expired.", show_alert=True)
        return
    session["use_artist_subfolders"] = val == "yes"
    await goto_album_subfolders(call, task_id)
    await call.answer()


@dp.callback_query(F.data.startswith("albumsub|"))
async def cb_album_subfolders(call: types.CallbackQuery):
    _, task_id, val = call.data.split("|")
    session = user_sessions.get(task_id)
    if session is None:
        await call.answer("Session expired.", show_alert=True)
        return
    session["use_album_subfolders"] = val == "yes"
    await goto_first_artist_only(call, task_id)
    await call.answer()


@dp.callback_query(F.data.startswith("firstartist|"))
async def cb_first_artist_only(call: types.CallbackQuery):
    _, task_id, val = call.data.split("|")
    session = user_sessions.get(task_id)
    if session is None:
        await call.answer("Session expired.", show_alert=True)
        return
    session["first_artist_only"] = val == "yes"
    await goto_lyrics(call, task_id)
    await call.answer()


@dp.callback_query(F.data.startswith("lyr|"))
async def cb_lyrics(call: types.CallbackQuery):
    _, task_id, val = call.data.split("|")
    session = user_sessions.get(task_id)
    if session is None:
        await call.answer("Session expired.", show_alert=True)
        return
    session["embed_lyrics"] = val == "yes"
    if session["embed_lyrics"]:
        await goto_lyrics_providers(call, task_id)
    else:
        await goto_enrichment(call, task_id)
    await call.answer()


@dp.callback_query(F.data.startswith("lyrp_toggle|"))
async def cb_lyrp_toggle(call: types.CallbackQuery):
    _, task_id, prov = call.data.split("|")
    session = user_sessions.get(task_id)
    if session is None:
        await call.answer("Session expired.", show_alert=True)
        return
    lst = session["lyrics_providers"]
    if prov in lst:
        lst.remove(prov)
    else:
        lst.append(prov)
    await call.message.edit_reply_markup(
        reply_markup=multi_provider_keyboard("lyrp", task_id, LYRICS_PROVIDERS, lst),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("lyrp_done|"))
async def cb_lyrp_done(call: types.CallbackQuery):
    _, task_id = call.data.split("|")
    session = user_sessions.get(task_id)
    if session is None:
        await call.answer("Session expired.", show_alert=True)
        return
    if not session["lyrics_providers"]:
        session["lyrics_providers"] = ["lrclib"]
    await goto_enrichment(call, task_id)
    await call.answer()


@dp.callback_query(F.data.startswith("enr|"))
async def cb_enrichment(call: types.CallbackQuery):
    _, task_id, val = call.data.split("|")
    session = user_sessions.get(task_id)
    if session is None:
        await call.answer("Session expired.", show_alert=True)
        return
    session["enrich_metadata"] = val == "yes"
    if session["enrich_metadata"]:
        await goto_enrich_providers(call, task_id)
    else:
        await goto_retries(call, task_id)
    await call.answer()


@dp.callback_query(F.data.startswith("enrp_toggle|"))
async def cb_enrp_toggle(call: types.CallbackQuery):
    _, task_id, prov = call.data.split("|")
    session = user_sessions.get(task_id)
    if session is None:
        await call.answer("Session expired.", show_alert=True)
        return
    lst = session["enrich_providers"]
    if prov in lst:
        lst.remove(prov)
    else:
        lst.append(prov)
    await call.message.edit_reply_markup(
        reply_markup=multi_provider_keyboard("enrp", task_id, ENRICH_PROVIDERS, lst),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("enrp_done|"))
async def cb_enrp_done(call: types.CallbackQuery):
    _, task_id = call.data.split("|")
    session = user_sessions.get(task_id)
    if session is None:
        await call.answer("Session expired.", show_alert=True)
        return
    if not session["enrich_providers"]:
        session["enrich_providers"] = ["deezer"]
    await goto_retries(call, task_id)
    await call.answer()


@dp.callback_query(F.data.startswith("retries|"))
async def cb_retries(call: types.CallbackQuery):
    _, task_id, val = call.data.split("|")
    session = user_sessions.get(task_id)
    if session is None:
        await call.answer("Session expired.", show_alert=True)
        return
    session["track_max_retries"] = int(val)
    await goto_timeout(call, task_id)
    await call.answer()


@dp.callback_query(F.data.startswith("timeout|"))
async def cb_timeout(call: types.CallbackQuery):
    _, task_id, val = call.data.split("|")
    session = user_sessions.get(task_id)
    if session is None:
        await call.answer("Session expired.", show_alert=True)
        return
    session["timeout_s"] = int(val)
    await goto_filename_format(call, task_id)
    await call.answer()


@dp.callback_query(F.data.startswith("fmt|"))
async def cb_filename_format(call: types.CallbackQuery):
    _, task_id, preset = call.data.split("|")
    session = user_sessions.get(task_id)
    if session is None:
        await call.answer("Session expired.", show_alert=True)
        return
    session["filename_format"] = FILENAME_FORMAT_PRESETS.get(
        preset, FILENAME_FORMAT_PRESETS["default"]
    )
    await goto_confirm(call, task_id)
    await call.answer()


@dp.callback_query(F.data.startswith("confirm|"))
async def cb_confirm(call: types.CallbackQuery):
    _, task_id, val = call.data.split("|")
    session = user_sessions.pop(task_id, None)
    if session is None:
        await call.answer("Session expired.", show_alert=True)
        return
    if val != "yes":
        await call.message.edit_text("❌ Cancelled.")
        await call.answer()
        return

    await call.message.edit_text("⏳ <b>Download queued...</b>")
    await call.answer()
    await enqueue_job(call.message.chat.id, call.message.message_id, session)


# ─── STARTUP ─────────────────────────────────────────────────────────────


async def main():
    db_init()
    asyncio.create_task(download_worker())
    me = await bot.get_me()
    log.info("SpotiFLAC bot started as @%s", me.username)
    log.info(
        "Downloads → %s · library → %s · access: %s",
        DOWNLOAD_DIR,
        DB_PATH,
        f"{len(ALLOWED_USER_IDS)} allowed user(s)"
        if ALLOWED_USER_IDS
        else "open to anyone (ALLOWED_USER_IDS unset)",
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
