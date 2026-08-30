"""An oversized cover must not cost the track its tags.

A FLAC METADATA_BLOCK carries a 24-bit length, so nothing in it may exceed
16 MiB. mutagen does not drop an oversized picture — it refuses the whole
save() with "block is too long to write", which means one 3000x3000 PNG
from an artwork service loses *every* tag on the track, not just the
artwork. Verified against mutagen directly, not assumed.

Pillow is a declared dependency now, so the no-Pillow path covers a broken
install rather than a normal one — but it is still tested, because letting
the import raise would turn a partial install back into a lost tag write.
"""

from __future__ import annotations

import builtins
import io
import os

import pytest

from SpotiFLAC.core.tagger import MAX_FLAC_PICTURE_BYTES, fit_cover

PIL = pytest.importorskip("PIL", reason="Pillow is a declared dependency")


def _oversized_png(side: int = 3000) -> bytes:
    """Random noise, so it does not compress — a smooth synthetic gradient
    lands at under a megabyte and never exercises the resizing at all.
    """
    from PIL import Image

    image = Image.frombytes("RGB", (side, side), os.urandom(side * side * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=0)
    return buffer.getvalue()


def test_a_cover_that_already_fits_is_returned_untouched() -> None:
    """No re-encoding, so nothing is lost to a needless JPEG round trip."""
    small = b"\xff\xd8" + b"x" * 1000
    assert fit_cover(small) is small


def test_an_oversized_cover_is_brought_under_the_limit() -> None:
    big = _oversized_png()
    assert len(big) > MAX_FLAC_PICTURE_BYTES

    fitted = fit_cover(big)

    assert fitted is not None
    assert len(fitted) <= MAX_FLAC_PICTURE_BYTES


def test_resolution_is_kept_when_quality_alone_is_enough() -> None:
    """Downscaling is the last resort: a 3000x3000 cover fits comfortably as
    JPEG, and shrinking it would throw away detail for nothing.
    """
    from PIL import Image

    fitted = fit_cover(_oversized_png(3000))
    assert Image.open(io.BytesIO(fitted)).size == (3000, 3000)


def test_the_fitted_cover_actually_embeds(tmp_path) -> None:
    """The point of the whole exercise: mutagen accepts the result where it
    refused the original.
    """
    ffmpeg = pytest.importorskip("shutil").which("ffmpeg")
    if not ffmpeg:
        pytest.skip("needs ffmpeg to make a FLAC to tag")

    import subprocess

    from mutagen.flac import FLAC, Picture

    path = tmp_path / "t.flac"
    subprocess.run(
        [
            "ffmpeg",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=1",
            "-y",
            str(path),
            "-loglevel",
            "error",
        ],
        check=True,
    )

    big = _oversized_png()

    # The failure this exists for.
    audio = FLAC(str(path))
    raw = Picture()
    raw.data, raw.mime, raw.type = big, "image/png", 3
    audio.add_picture(raw)
    with pytest.raises(Exception, match="too long"):
        audio.save()

    # And the same file with a fitted cover.
    audio = FLAC(str(path))
    fitted = Picture()
    fitted.data, fitted.mime, fitted.type = fit_cover(big), "image/jpeg", 3
    audio.add_picture(fitted)
    audio.save()
    assert len(FLAC(str(path)).pictures) == 1


def test_without_pillow_the_cover_is_dropped_not_the_tags(monkeypatch) -> None:
    """A broken Pillow install must cost the artwork and nothing else."""
    # Built before Pillow is hidden — the fixture needs it to make the image.
    big = _oversized_png()

    real_import = builtins.__import__

    def no_pillow(name, *args, **kwargs):
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("Pillow is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pillow)
    assert fit_cover(big) is None


def test_undecodable_data_is_dropped_rather_than_embedded() -> None:
    """A truncated download or an HTML error page served as artwork."""
    assert fit_cover(b"<html>404</html>" + b"x" * MAX_FLAC_PICTURE_BYTES) is None
