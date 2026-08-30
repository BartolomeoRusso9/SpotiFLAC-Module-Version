"""Two downloads must not write the same file at once.

The output filename comes from the metadata, so identical metadata gives an
identical name: a playlist that lists a track twice, an album queued
alongside the single it came from, the same track picked from two sources.
Nothing stopped those writing concurrently — both stream into the same
`.part` and rename over the same destination, and what survives is a file
that looks complete and may be two downloads interleaved.

Serialising per path rather than globally is the whole point, so the test
that unrelated paths still run in parallel matters as much as the one that
a collision waits.
"""

from __future__ import annotations

import asyncio

import pytest

from SpotiFLAC.core.output_lock import output_path_lock


async def _write_interleaved(path: str, marker: str, log: list[str]) -> None:
    """Two of these running concurrently on one path would interleave."""
    async with output_path_lock(path):
        log.append(f"{marker}-start")
        await asyncio.sleep(0.02)
        log.append(f"{marker}-end")


def test_the_same_path_is_written_one_at_a_time() -> None:
    log: list[str] = []
    asyncio.run(
        _gather(
            _write_interleaved("/m/Artist/Song.flac", "a", log),
            _write_interleaved("/m/Artist/Song.flac", "b", log),
        )
    )
    # Whoever went first finished before the other started.
    assert log in (
        ["a-start", "a-end", "b-start", "b-end"],
        ["b-start", "b-end", "a-start", "a-end"],
    ), log


def test_different_paths_are_not_serialised() -> None:
    """A global lock would be correct and useless: every download would
    queue behind every other.
    """
    log: list[str] = []
    asyncio.run(
        _gather(
            _write_interleaved("/m/One.flac", "a", log),
            _write_interleaved("/m/Two.flac", "b", log),
        )
    )
    assert log[:2] == ["a-start", "b-start"], log


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("/m/Artist/Song.flac", "/m/Artist/./Song.flac"),
        ("/m/Artist//Song.flac", "/m/Artist/Song.flac"),
        ("/m/Artist/Song.flac", "/m/Other/../Artist/Song.flac"),
    ],
)
def test_paths_that_name_the_same_file_share_a_lock(left, right) -> None:
    """A lock keyed on the raw string would let "./" past it — the same file
    under a different spelling is the same file.
    """
    log: list[str] = []
    asyncio.run(
        _gather(
            _write_interleaved(left, "a", log),
            _write_interleaved(right, "b", log),
        )
    )
    assert log[1].endswith("-end"), log


def test_the_lock_is_released_when_the_body_raises() -> None:
    """A failed download must not wedge that filename for the session."""

    async def _boom() -> None:
        with pytest.raises(RuntimeError):
            async with output_path_lock("/m/Boom.flac"):
                raise RuntimeError("provider exploded")
        # If the lock leaked, this would deadlock rather than return.
        async with output_path_lock("/m/Boom.flac"):
            pass

    asyncio.run(asyncio.wait_for(_boom(), timeout=5))


def test_a_second_event_loop_gets_its_own_lock() -> None:
    """The GUI runs each API call in its own thread with its own
    asyncio.run(), so a lock cached once and awaited under a second loop is
    the cross-loop reuse asyncio warns about — the same reason
    AsyncRateLimiter keys its locks by loop.
    """

    async def _once() -> None:
        async with output_path_lock("/m/Shared.flac"):
            await asyncio.sleep(0)

    asyncio.run(_once())
    asyncio.run(_once())  # a reused, dead-loop lock would raise here


def test_the_provider_holds_it_across_the_whole_write() -> None:
    """Locking only the download would leave the rename, the validation and
    the tag write racing.
    """
    import ast
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "SpotiFLAC" / "extensions" / "provider.py"
    ).read_text()

    guarded = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncWith)
        and any(
            "output_path_lock" in ast.unparse(item.context_expr) for item in node.items
        )
    ]
    assert guarded, "the provider does not take the lock at all"

    body = ast.unparse(guarded[0])
    for step in (
        "'download'",
        "validate_downloaded_track_async",
        "track_identity_mismatch",
        "embed_metadata_async",
    ):
        assert step in body, f"{step} is outside the lock"


async def _gather(*coros) -> None:
    await asyncio.gather(*coros)
