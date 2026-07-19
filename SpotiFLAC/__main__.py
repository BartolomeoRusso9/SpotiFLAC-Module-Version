"""
SpotiFLAC/__main__.py
"""

from __future__ import annotations

import asyncio
import logging
import sys

from .client import AsyncSpotiFLAC
from .launcher import (
    _load_profile_into_defaults,
    load_config,
    parse_args,
)
from .core.ffmpeg_check import print_ffmpeg_warning


async def _run_cli_download(args, merged_defaults: dict) -> None:
    """Costruisce AsyncSpotiFLAC dagli argomenti CLI ed esegue il download."""
    quality = args.quality or merged_defaults.get("quality", "LOSSLESS")
    qobuz_local_api_url = args.qobuz_local_api_url or merged_defaults.get(
        "qobuz_local_api_url"
    )
    tidal_custom_api = args.tidal_custom_api or merged_defaults.get("tidal_custom_api")
    timeout_s = (
        args.timeout_s
        if args.timeout_s is not None
        else merged_defaults.get("timeout_s")
    )
    track_max_retries = (
        args.retries
        if args.retries is not None
        else merged_defaults.get("track_max_retries", 0)
    )

    log_level = logging.DEBUG if args.verbose else logging.WARNING
    log_format = (
        "%(levelname)s:%(name)s: %(message)s"
        if args.verbose
        else "%(levelname)s: %(message)s"
    )
    logging.basicConfig(level=log_level, format=log_format)

    async with AsyncSpotiFLAC(
        output_dir=args.output_dir,
        services=args.service,
        filename_format=args.filename_format,
        use_track_numbers=args.use_track_numbers,
        use_album_track_numbers=args.use_album_track_numbers,
        use_artist_subfolders=args.use_artist_subfolders,
        use_album_subfolders=args.use_album_subfolders,
        allow_fallback=True,
        quality=quality,
        first_artist_only=args.first_artist_only,
        include_featuring=args.include_featuring,
        log_level=log_level,
        output_path=args.output_path,
        embed_lyrics=args.embed_lyrics,
        lyrics_providers=args.lyrics_providers,
        enrich_metadata=args.enrich,
        enrich_providers=args.enrich_providers,
        qobuz_local_api_url=qobuz_local_api_url,
        tidal_custom_api=tidal_custom_api,
        track_max_retries=track_max_retries,
        post_download_action=args.post_action,
        post_download_command=args.post_command,
        timeout_s=timeout_s,
        use_extensions_fallback=getattr(args, "use_extensions_fallback", True),
    ) as client:
        urls = args.url if isinstance(args.url, list) else [args.url]
        await client.download_batch(urls, loop_minutes=args.loop)

    if args.save_profile:
        try:
            from .core.profiles import save_profile_async

            profile_cfg = {
                "services": args.service,
                "quality": quality,
                "filename_format": args.filename_format,
                "use_track_numbers": args.use_track_numbers,
                "use_album_track_numbers": args.use_album_track_numbers,
                "use_artist_subfolders": args.use_artist_subfolders,
                "use_album_subfolders": args.use_album_subfolders,
                "first_artist_only": args.first_artist_only,
                "include_featuring": args.include_featuring,
                "allow_fallback": True,
                "embed_lyrics": args.embed_lyrics,
                "lyrics_providers": args.lyrics_providers,
                "enrich_metadata": args.enrich,
                "enrich_providers": args.enrich_providers,
                "track_max_retries": track_max_retries,
                "post_download_action": args.post_action,
                "post_download_command": args.post_command,
                "qobuz_local_api_url": qobuz_local_api_url,
                "tidal_custom_api": tidal_custom_api,
                "timeout_s": timeout_s,
                "loop": args.loop,
            }
            await save_profile_async(args.save_profile, profile_cfg)
            print(f"[profile] Saved as: {args.save_profile}")
        except Exception as exc:
            print(f"[profile] Save error: {exc}")


async def main() -> None:
    """
    Unico entry point asincrono del processo. Sostituisce `amain()` di
    launcher.py: nessuna logica qui apre un proprio event loop separato,
    tutto — check aggiornamenti, sync estensioni, GUI/interactive/CLI —
    gira sull'unico loop avviato da `asyncio.run(main())` in fondo al file.
    """
    from .check_update import check_for_updates_async

    try:
        await check_for_updates_async()
    except Exception:
        pass

    # GUI mode: la webview gestisce il proprio ciclo di eventi nativo (non
    # asyncio) — resta un caso speciale sincrono, invariato rispetto a prima.
    if "--gui" in sys.argv:
        from .app import run_gui

        run_gui()
        return

    if "--interactive" in sys.argv:
        from .interactive import run_interactive

        print_ffmpeg_warning()
        cfg = await run_interactive()

        log_level = logging.WARNING
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )

        async with AsyncSpotiFLAC(
            output_dir=cfg["output_dir"],
            services=cfg["services"],
            filename_format=cfg["filename_format"],
            use_track_numbers=cfg["use_track_numbers"],
            use_album_track_numbers=cfg["use_album_track_numbers"],
            use_artist_subfolders=cfg["use_artist_subfolders"],
            use_album_subfolders=cfg["use_album_subfolders"],
            quality=cfg["quality"],
            first_artist_only=cfg["first_artist_only"],
            include_featuring=cfg.get("include_featuring", False),
            log_level=log_level,
            output_path=cfg.get("output_path"),
            allow_fallback=cfg.get("allow_fallback", True),
            embed_lyrics=cfg["embed_lyrics"],
            lyrics_providers=cfg["lyrics_providers"],
            enrich_metadata=cfg["enrich_metadata"],
            enrich_providers=cfg["enrich_providers"],
            qobuz_local_api_url=cfg.get("qobuz_local_api_url"),
            tidal_custom_api=cfg.get("tidal_custom_api") or None,
            track_max_retries=cfg.get("track_max_retries", 0),
            post_download_action=cfg.get("post_download_action", "none"),
            post_download_command=cfg.get("post_download_command", ""),
            timeout_s=cfg.get("timeout_s"),
            use_extensions_fallback=cfg.get("use_extensions_fallback", True),
        ) as client:
            await client.download_batch([cfg["url"]], loop_minutes=cfg.get("loop"))
        return

    if len(sys.argv) == 1:
        import argparse

        parser = argparse.ArgumentParser(prog="spotiflac")
        parser.add_argument("--gui", action="store_true")
        parser.add_argument("--interactive", action="store_true")
        parser.print_help()
        return

    print_ffmpeg_warning()

    profile_defaults: dict = {}
    if "--profile" in sys.argv:
        idx = sys.argv.index("--profile")
        if idx + 1 < len(sys.argv):
            profile_defaults = await _load_profile_into_defaults(sys.argv[idx + 1])

    file_cfg = load_config()
    merged_defaults = {**file_cfg, **profile_defaults}
    args = parse_args(profile_defaults=merged_defaults)

    if not args.url or not args.output_dir:
        import argparse

        parser = argparse.ArgumentParser(prog="spotiflac")
        parser.add_argument("--gui", action="store_true")
        parser.add_argument("--interactive", action="store_true")
        parser.print_help()
        return

    await _run_cli_download(args, merged_defaults)


def run() -> None:
    """Punto d'ingresso sincrono (script console `spotiflac`)."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n[!] Operation interrupted by user.")


if __name__ == "__main__":
    run()
