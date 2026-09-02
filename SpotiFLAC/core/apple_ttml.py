"""Apple Music's syllable-timed lyrics (TTML) as LRC.

Apple serves lyrics as TTML, and its TTML carries more than any of the
JSON relays this codebase talks to expose: a `begin`/`end` on every
*syllable*, background vocals marked as their own role, and — for tracks
that have them — an official translation and a romanisation shipped in the
same document. The relay used until now flattens all of that to a list of
words, so the translation and the backing vocals were simply unavailable.

The catch is that `songs/{id}/syllable-lyrics` needs a subscriber's
Media-User-Token; without one Apple answers 401 and there is nothing here
to parse. That is why this is a *fallback-first* addition rather than a
replacement: it is used when a token is configured and the existing relay
handles everything else.

Two shapes of document turn up in practice, and both are handled:

  * ``itunes:timing="Word"`` — each ``<p>`` holds ``<span>``s, one per
    syllable, each with its own ``begin``. This is what enhanced LRC's
    inline ``<mm:ss.xx>`` tags are for.
  * ``itunes:timing="Line"`` (or absent) — the ``<p>`` carries the text and
    a single ``begin``. Line-synced LRC, nothing more to say.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from xml.etree import ElementTree

logger = logging.getLogger(__name__)

#: ttm:role on a span whose contents are background/backing vocals. Apple
#: nests these inside the line they belong to rather than giving them a
#: line of their own, which is exactly how they are sung.
_ROLE_BACKGROUND = "x-bg"

#: A clock value: [[hh:]mm:]ss[.fff], which is what Apple writes. The
#: offset form ("12.5s") is legal TTML and shows up in older documents.
_CLOCK_RE = re.compile(
    r"^(?:(?P<h>\d+):)?(?:(?P<m>\d{1,2}):)?(?P<s>\d{1,2})(?:\.(?P<frac>\d+))?$",
)
_OFFSET_RE = re.compile(r"^(?P<value>\d+(?:\.\d+)?)(?P<unit>h|m|s|ms)$")


def _local(tag: str) -> str:
    """The tag name without its ``{namespace}``.

    TTML documents from Apple carry four namespaces and have changed which
    prefix they use for what; matching on the local name means the parser
    does not break when they add a fifth.
    """
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _attr(element: ElementTree.Element, name: str) -> str:
    """An attribute by local name, whatever namespace it was written in."""
    value = element.get(name)
    if value is not None:
        return value
    for key, val in element.attrib.items():
        if _local(key) == name:
            return val
    return ""


def parse_time(value: str) -> int:
    """A TTML time expression in milliseconds, or 0 if it is not one."""
    text = (value or "").strip()
    if not text:
        return 0

    offset = _OFFSET_RE.match(text)
    if offset:
        amount = float(offset.group("value"))
        unit = offset.group("unit")
        factor = {"h": 3_600_000.0, "m": 60_000.0, "s": 1000.0, "ms": 1.0}[unit]
        return int(round(amount * factor))

    clock = _CLOCK_RE.match(text)
    if not clock:
        return 0
    hours = clock.group("h")
    minutes = clock.group("m")
    seconds = int(clock.group("s"))
    # "01:23" is minutes:seconds, not hours:minutes — the hour group only
    # captures when all three components are present.
    if hours is not None and minutes is None:
        minutes, hours = hours, None
    total = seconds
    total += int(minutes or 0) * 60
    total += int(hours or 0) * 3600
    frac = clock.group("frac") or ""
    millis = int((frac + "000")[:3]) if frac else 0
    return total * 1000 + millis


@dataclass
class Syllable:
    #: The syllable, keeping any trailing space the document had after it.
    #: That space is the whole word-boundary signal: syllables of one word
    #: ("ex|pres|sions") sit flush against each other with none, so joining
    #: the texts back in order reproduces Apple's own spacing.
    text: str
    start_ms: int
    end_ms: int


@dataclass
class Line:
    start_ms: int
    end_ms: int
    text: str
    syllables: list[Syllable] = field(default_factory=list)
    #: Background vocals sung under this line, already flattened to text.
    background: str = ""
    #: Apple's own id for the line (``itunes:key``, e.g. "L12"), which is
    #: how the translation and romanisation blocks refer back to it.
    key: str = ""


@dataclass
class TtmlLyrics:
    lines: list[Line] = field(default_factory=list)
    #: Per-line, keyed by the same ``itunes:key`` the lines carry.
    translations: dict[str, str] = field(default_factory=dict)
    romanizations: dict[str, str] = field(default_factory=dict)
    translation_lang: str = ""
    romanization_lang: str = ""
    #: "Word", "Line" or "None", straight from ``itunes:timing``.
    timing: str = ""

    @property
    def word_by_word(self) -> bool:
        return self.timing.lower() == "word"

    @property
    def synced(self) -> bool:
        return any(line.start_ms or line.syllables for line in self.lines)


def _text_of(element: ElementTree.Element, skip_background: bool = True) -> str:
    """All text under `element`, with the background spans left out.

    Spacing follows the document: TTML puts the separating whitespace in
    the tail text between spans, so joining the pieces in order reproduces
    it. Syllables of one word have no tail, and so stay joined.
    """
    parts: list[str] = [element.text or ""]
    for child in element:
        if skip_background and _attr(child, "role") == _ROLE_BACKGROUND:
            parts.append(child.tail or "")
            continue
        parts.append(_text_of(child, skip_background))
        parts.append(child.tail or "")
    return "".join(parts)


def _collect_syllables(
    element: ElementTree.Element,
    out: list[Syllable],
    skip_background: bool = True,
) -> None:
    for child in element:
        if skip_background and _attr(child, "role") == _ROLE_BACKGROUND:
            continue
        if _local(child.tag) != "span":
            continue
        begin = _attr(child, "begin")
        if begin and (child.text or "").strip():
            out.append(
                Syllable(
                    text=child.text or "",
                    start_ms=parse_time(begin),
                    end_ms=parse_time(_attr(child, "end")),
                ),
            )
        else:
            _collect_syllables(child, out, skip_background)
        # The tail is the space Apple puts *between* words. Recording it on
        # the syllable just emitted is what lets the joiner tell "ex|pres"
        # (one word) from "the cat" (two).
        if out and child.tail and not child.tail.strip():
            out[-1].text += " "


def _background_of(element: ElementTree.Element) -> str:
    parts = [
        _text_of(child, skip_background=False)
        for child in element
        if _attr(child, "role") == _ROLE_BACKGROUND
    ]
    return " ".join(part.strip() for part in parts if part.strip()).strip()


def _parse_annotation_block(
    block: ElementTree.Element,
) -> tuple[str, dict[str, str]]:
    """One ``<translation>``/``<transliteration>`` as (language, {key: text})."""
    lang = _attr(block, "lang")
    mapping: dict[str, str] = {}
    for text_el in block.iter():
        if _local(text_el.tag) != "text":
            continue
        key = _attr(text_el, "for")
        if not key:
            continue
        value = " ".join(_text_of(text_el, skip_background=False).split())
        if value:
            mapping[key] = value
    return lang, mapping


def parse_ttml(xml_text: str) -> TtmlLyrics:
    """An Apple TTML document, structured.

    Never raises for bad input: a malformed or truncated document yields an
    empty result, because every caller here is enriching a file it already
    has and an exception would fail the whole tag write over a nicety.
    """
    result = TtmlLyrics()
    if not xml_text or not xml_text.strip():
        return result

    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        logger.debug("[apple_ttml] not parseable as XML: %s", exc)
        return result

    result.timing = _attr(root, "timing")

    for element in root.iter():
        name = _local(element.tag)
        if name == "translation":
            lang, mapping = _parse_annotation_block(element)
            if mapping:
                result.translations.update(mapping)
                result.translation_lang = result.translation_lang or lang
        elif name == "transliteration":
            lang, mapping = _parse_annotation_block(element)
            if mapping:
                result.romanizations.update(mapping)
                result.romanization_lang = result.romanization_lang or lang

    for paragraph in root.iter():
        if _local(paragraph.tag) != "p":
            continue
        syllables: list[Syllable] = []
        _collect_syllables(paragraph, syllables)
        text = " ".join(_text_of(paragraph).split())
        if not text and syllables:
            text = "".join(s.text for s in syllables).strip()
        if not text:
            continue
        result.lines.append(
            Line(
                start_ms=parse_time(_attr(paragraph, "begin")),
                end_ms=parse_time(_attr(paragraph, "end")),
                text=text,
                syllables=syllables,
                background=_background_of(paragraph),
                key=_attr(paragraph, "key"),
            ),
        )

    return result


def _timestamp(milliseconds: int, opening: str = "[") -> str:
    minutes, remainder = divmod(max(0, milliseconds), 60_000)
    seconds, remainder = divmod(remainder, 1_000)
    centiseconds = remainder // 10
    closing = ">" if opening == "<" else "]"
    return f"{opening}{minutes:02d}:{seconds:02d}.{centiseconds:02d}{closing}"


def _line_body(line: Line, word_by_word: bool) -> str:
    if not (word_by_word and line.syllables):
        return line.text
    parts: list[str] = []
    for syllable in line.syllables:
        stripped = syllable.text.strip()
        if not stripped:
            continue
        parts.append(f"{_timestamp(syllable.start_ms, '<')}{stripped}")
        if syllable.text.endswith(" "):
            parts.append(" ")
    return "".join(parts).strip() or line.text


def to_lrc(
    lyrics: TtmlLyrics,
    *,
    word_by_word: bool = True,
    include_background: bool = True,
    translation: bool = False,
    romanization: bool = False,
) -> str:
    """`lyrics` as LRC text.

    `word_by_word` emits the enhanced form with an inline ``<mm:ss.xx>``
    per syllable; it is ignored for a document Apple only timed per line,
    since there are no syllable times to emit.

    Translation and romanisation lines repeat the line's own timestamp
    rather than getting one of their own — that is how players that show
    two languages at once expect to find them, and a player that does not
    understand the duplicate simply shows both.
    """
    if not lyrics.lines:
        return ""

    use_syllables = word_by_word and lyrics.word_by_word
    out: list[str] = []
    for line in lyrics.lines:
        stamp = _timestamp(line.start_ms)
        body = _line_body(line, use_syllables)
        if include_background and line.background:
            # Apple usually writes the parentheses into the backing-vocal
            # text itself; adding another pair gives "((oh yeah))".
            backing = line.background
            if not (backing.startswith("(") and backing.endswith(")")):
                backing = f"({backing})"
            body = f"{body} {backing}"
        out.append(f"{stamp}{body}")

        if romanization and line.key:
            roman = lyrics.romanizations.get(line.key, "")
            if roman:
                out.append(f"{stamp}{roman}")
        if translation and line.key:
            translated = lyrics.translations.get(line.key, "")
            if translated:
                out.append(f"{stamp}{translated}")

    return "\n".join(out)


def ttml_to_lrc(
    xml_text: str,
    *,
    word_by_word: bool = True,
    include_background: bool = True,
    translation: bool = False,
    romanization: bool = False,
) -> str:
    """Convenience wrapper: parse then render, in one call."""
    return to_lrc(
        parse_ttml(xml_text),
        word_by_word=word_by_word,
        include_background=include_background,
        translation=translation,
        romanization=romanization,
    )
