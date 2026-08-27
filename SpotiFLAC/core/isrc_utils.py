from __future__ import annotations

import re

_ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")


def normalize_isrc(value: str) -> str:
    if not value:
        return ""
    s = str(value).upper().strip()
    # remove common prefixes or labels
    s = s.replace("ISRC:", "").strip()
    # keep only alnum
    s = re.sub(r"[^A-Z0-9]", "", s)
    if _ISRC_RE.match(s):
        return s
    return ""


def is_valid_isrc(value: str) -> bool:
    return bool(value and _ISRC_RE.match(value))


async def confirm_isrc_with_qobuz_async(
    isrc: str,
    title: str = "",
    artist: str = "",
    duration_ms: int = 0,
    qobuz_token: str | None = None,
) -> tuple[bool, dict | None]:
    """Confirm an ISRC by querying Qobuz via the dynamically loaded Extension."""
    if not isrc:
        return False, None

    prov = None
    try:
        from SpotiFLAC.extensions.manager import ExtensionManager
        from SpotiFLAC.extensions.python_provider import PythonExtensionProvider

        manager = ExtensionManager(auto_install_downloads=False)
        # Cerca il provider Python di Qobuz
        cand = next(
            (
                c.name
                for c in manager.list_installed()
                if c.runtime == "python" and "qobuz" in c.name.lower()
            ),
            None,
        )
        if cand:
            prov = PythonExtensionProvider(cand, qobuz_token=qobuz_token)
    except Exception:
        pass

    if not prov:
        return False, None

    try:
        # If the provider is the native Python one, it will have this method
        track = await prov._search_by_isrc_async(isrc)
    except Exception:
        return False, None

    if not track:
        return False, None

    candidate_dur = 0
    if isinstance(track.get("duration"), (int, float)):
        candidate_dur = int(track.get("duration") or 0) * 1000
    elif isinstance(track.get("duration_ms"), (int, float)):
        candidate_dur = int(track.get("duration_ms") or 0)

    if duration_ms and candidate_dur:
        diff = abs(duration_ms - candidate_dur)
        if diff <= 3000:
            return True, track
        if diff <= 10000:
            import re

            tnorm = re.sub(r"\s+", " ", (title or "").strip().lower())
            pname = str(track.get("title") or track.get("name") or "").strip().lower()
            performer = (
                str((track.get("performer") or {}).get("name", "") or "")
                .strip()
                .lower()
            )
            if tnorm and (
                tnorm in pname
                or tnorm in performer
                or (artist and artist.lower() in performer)
            ):
                return True, track
            return False, None

    return True, track
