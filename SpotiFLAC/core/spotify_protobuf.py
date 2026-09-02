"""Decoder for Spotify's ``spclient`` metadata protobufs.

``spclient.wg.spotify.com/metadata/4/{track,album}/{gid}`` answers with a
binary protobuf (``content-type: vnd.spotify/metadata-track``), not JSON.
The endpoint was already being called for the ISRC alone, which was scraped
out with a regex over the raw bytes — everything else in the message was
thrown away. That was a lot to throw away: the UPC, the disc layout, the
label, the copyright line and the release date all live in the same
response, and several of them have no other source in this codebase.

There is no protobuf *schema* here and no ``protobuf`` dependency. The wire
format is self-describing enough to walk without one — every field carries
its number and wire type — so this reads the message generically and then
picks out the field numbers Spotify uses, which were confirmed against live
responses rather than taken from a published ``.proto``.

One subtlety worth stating, because getting it wrong is silent: Spotify
mixes ``sint``-encoded integers with plain enums in the same message.
Track/disc numbers, durations and the year/month/day of a release date are
zigzag-encoded and must go through :func:`decode_sint` — the track number 8
arrives on the wire as 16. The album-type and explicit fields are plain
enums and must *not*. Applying zigzag uniformly turns track 8 into -9 and
"explicit" into "not explicit"; skipping it uniformly halves every number.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_BASE62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

#: Wire types we know how to skip over. Anything else ends the walk, since
#: without a length we cannot find where the next field starts.
_WIRE_VARINT = 0
_WIRE_64BIT = 1
_WIRE_LEN = 2
_WIRE_32BIT = 5

#: album.type — a plain enum, deliberately not zigzag-decoded.
_ALBUM_TYPES = {
    1: "album",
    2: "single",
    3: "compilation",
    4: "ep",
    5: "audiobook",
    6: "podcast",
}

#: track.artist_with_role.role — 5 is the composer credit.
_ROLE_COMPOSER = 5


# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------


def _read_varint(data: bytes, pos: int) -> tuple[int | None, int]:
    result = 0
    shift = 0
    # 10 bytes is the widest a 64-bit varint can be; a longer run means the
    # buffer is not what we think it is, so stop rather than loop forever.
    for _ in range(10):
        if pos >= len(data):
            return None, pos
        current = data[pos]
        pos += 1
        result |= (current & 0x7F) << shift
        if not current & 0x80:
            return result, pos
        shift += 7
    return None, pos


def read_fields(data: bytes) -> dict[int, list[tuple[int, Any]]]:
    """The message as ``{field_number: [(wire_type, value), ...]}``.

    Truncated or unparseable input yields whatever was read before the
    damage rather than raising: these are best-effort enrichment reads, and
    a partial album is worth more than an exception.
    """
    fields: dict[int, list[tuple[int, Any]]] = {}
    if not data:
        return fields

    pos = 0
    length = len(data)
    while pos < length:
        tag, pos = _read_varint(data, pos)
        if not tag:
            break
        field_number, wire_type = tag >> 3, tag & 7

        if wire_type == _WIRE_VARINT:
            value, pos = _read_varint(data, pos)
            if value is None:
                break
        elif wire_type == _WIRE_LEN:
            size, pos = _read_varint(data, pos)
            if size is None or size < 0 or pos + size > length:
                break
            value = data[pos : pos + size]
            pos += size
        elif wire_type == _WIRE_64BIT:
            if pos + 8 > length:
                break
            value = data[pos : pos + 8]
            pos += 8
        elif wire_type == _WIRE_32BIT:
            if pos + 4 > length:
                break
            value = data[pos : pos + 4]
            pos += 4
        else:
            break

        fields.setdefault(field_number, []).append((wire_type, value))

    return fields


def field_values(
    fields: dict[int, list[tuple[int, Any]]],
    number: int,
    wire_type: int | None = None,
) -> list[Any]:
    entries = fields.get(number) or []
    return [
        value
        for entry_wire, value in entries
        if wire_type is None or entry_wire == wire_type
    ]


def first_value(
    fields: dict[int, list[tuple[int, Any]]],
    number: int,
    wire_type: int | None = None,
) -> Any:
    values = field_values(fields, number, wire_type)
    return values[0] if values else None


def field_str(fields: dict[int, list[tuple[int, Any]]], number: int) -> str:
    value = first_value(fields, number, _WIRE_LEN)
    if not value:
        return ""
    return bytes(value).decode("utf-8", errors="replace")


def decode_sint(value: Any) -> int:
    """Zigzag decode. See the module docstring for when this applies."""
    number = int(value or 0)
    return (number >> 1) ^ -(number & 1)


def gid_to_id(gid: bytes | None) -> str:
    """The 16-byte binary GID as the 22-character base62 ID used in URLs."""
    if not gid:
        return ""
    number = int.from_bytes(bytes(gid), "big")
    if not number:
        return "0" * 22
    out: list[str] = []
    while number:
        number, remainder = divmod(number, 62)
        out.append(_BASE62[remainder])
    return "".join(reversed(out)).rjust(22, "0")


def id_to_gid_hex(spotify_id: str) -> str:
    """The inverse of :func:`gid_to_id`, as the hex the endpoint wants."""
    number = 0
    for char in spotify_id:
        index = _BASE62.find(char)
        if index < 0:
            raise ValueError(f"invalid base62 character in Spotify ID: {char!r}")
        number = number * 62 + index
    return number.to_bytes(16, "big").hex()


# ---------------------------------------------------------------------------
# Message shapes
# ---------------------------------------------------------------------------


def _unique(values: Any) -> list[str]:
    """Order-preserving dedup, case-insensitively, dropping blanks."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _parse_external_id(raw: bytes) -> tuple[str, str]:
    fields = read_fields(raw)
    return field_str(fields, 1).lower(), field_str(fields, 2)


def _parse_artist(raw: bytes) -> dict[str, str]:
    fields = read_fields(raw)
    artist_id = gid_to_id(first_value(fields, 1, _WIRE_LEN))
    return {
        "id": artist_id,
        "name": field_str(fields, 2),
        "url": f"https://open.spotify.com/artist/{artist_id}" if artist_id else "",
    }


def _parse_date(raw: bytes | None) -> str:
    """A ``Date`` message as ``YYYY``/``YYYY-MM``/``YYYY-MM-DD``.

    The precision is preserved rather than padded, because Spotify genuinely
    does not always know the month: emitting "1979-01-01" for an album it
    only dates to 1979 invents a day, and taggers downstream cannot tell the
    invented part from the known part.
    """
    if not raw:
        return ""
    fields = read_fields(raw)
    year = decode_sint(first_value(fields, 1, _WIRE_VARINT))
    month = decode_sint(first_value(fields, 2, _WIRE_VARINT))
    day = decode_sint(first_value(fields, 3, _WIRE_VARINT))
    if year <= 0:
        return ""
    if month <= 0:
        return f"{year:04d}"
    if day <= 0:
        return f"{year:04d}-{month:02d}"
    return f"{year:04d}-{month:02d}-{day:02d}"


def parse_album(raw: bytes) -> dict[str, Any]:
    """The ``album`` message as a plain dict.

    Note that the album embedded inside a *track* response carries only a
    subset of these fields — no UPC, no disc list, no copyright. Those only
    arrive from a separate ``/metadata/4/album/`` fetch, which is why
    :func:`SpotiFLAC.core.spotfetch.SpotifyWebClient.get_native_track_metadata`
    fills the gaps from a second request.
    """
    fields = read_fields(raw)

    album_id = gid_to_id(first_value(fields, 1, _WIRE_LEN))
    artists = [_parse_artist(value) for value in field_values(fields, 3, _WIRE_LEN)]

    external_ids = dict(
        _parse_external_id(value) for value in field_values(fields, 10, _WIRE_LEN)
    )
    # EAN is the same barcode in a different registry; either identifies the
    # release, so take whichever the message happens to carry.
    upc = external_ids.get("upc") or external_ids.get("ean") or ""

    total_discs = 0
    total_tracks = 0
    discs = field_values(fields, 11, _WIRE_LEN)
    for disc in discs:
        disc_fields = read_fields(disc)
        number = decode_sint(first_value(disc_fields, 1, _WIRE_VARINT))
        total_discs = max(total_discs, number)
        total_tracks += len(field_values(disc_fields, 3, _WIRE_LEN))
    if not total_discs and discs:
        total_discs = len(discs)

    copyrights = [
        field_str(read_fields(value), 2)
        for value in field_values(fields, 13, _WIRE_LEN)
    ]
    genres = [
        bytes(value).decode("utf-8", errors="replace")
        for value in field_values(fields, 8, _WIRE_LEN)
    ]

    first_artist = artists[0] if artists else {}
    # Field 4 is a plain enum — no zigzag. See the module docstring.
    album_type = _ALBUM_TYPES.get(
        int(first_value(fields, 4, _WIRE_VARINT) or 1), "album"
    )

    return {
        "album_id": album_id,
        "album_name": field_str(fields, 2),
        "album_artist": ", ".join(
            _unique(artist.get("name", "") for artist in artists)
        ),
        "album_artist_names": _unique(artist.get("name", "") for artist in artists),
        "artist_id": first_artist.get("id", ""),
        "artist_url": first_artist.get("url", ""),
        "album_url": f"https://open.spotify.com/album/{album_id}" if album_id else "",
        "album_type": album_type,
        "label": field_str(fields, 5),
        "release_date": _parse_date(first_value(fields, 6, _WIRE_LEN)),
        "upc": upc,
        "total_tracks": total_tracks,
        "total_discs": total_discs,
        "genre": "; ".join(_unique(genres)),
        "copyright": "; ".join(_unique(copyrights)),
    }


def parse_track(raw: bytes) -> dict[str, Any]:
    """The ``track`` message as a plain dict, album fields folded in."""
    fields = read_fields(raw)

    album_value = first_value(fields, 3, _WIRE_LEN)
    result: dict[str, Any] = parse_album(album_value) if album_value else {}

    external_ids = dict(
        _parse_external_id(value) for value in field_values(fields, 10, _WIRE_LEN)
    )
    result["isrc"] = (external_ids.get("isrc") or "").upper()

    composers = []
    for role_value in field_values(fields, 32, _WIRE_LEN):
        role_fields = read_fields(role_value)
        # Field 3 is a plain role enum — no zigzag.
        role = int(first_value(role_fields, 3, _WIRE_VARINT) or 0)
        if role == _ROLE_COMPOSER:
            composers.append(field_str(role_fields, 2))

    result["track_name"] = field_str(fields, 2)
    result["track_number"] = decode_sint(first_value(fields, 5, _WIRE_VARINT))
    result["disc_number"] = decode_sint(first_value(fields, 6, _WIRE_VARINT))
    result["duration_ms"] = decode_sint(first_value(fields, 7, _WIRE_VARINT))
    # Field 9 is a plain enum: 1 means explicit. Zigzag-decoding it would
    # turn every explicit track into -1 and the flag would never be set.
    result["explicit"] = int(first_value(fields, 9, _WIRE_VARINT) or 0) == 1
    result["composer"] = "; ".join(_unique(composers))
    return result


def merge_fallbacks(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    """Fills blanks in `target` from `source`, never overwriting.

    Used to complete a track's partial embedded album from the fuller album
    response. The direction matters: the track's own copy of a field is the
    one scoped to the requested market, so it wins wherever it exists.
    """
    for key, value in source.items():
        if value in (None, "", 0, [], {}):
            continue
        if target.get(key) in (None, "", 0, [], {}):
            target[key] = value
    return target
