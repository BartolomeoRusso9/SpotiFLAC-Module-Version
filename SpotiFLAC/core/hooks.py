"""Post-download hooks: your own Python called after each track.

Why this exists
---------------
`--post-action command` hands a string to a shell. That is fine when you
type it into your own shell — you already had one — but it is a poor
interface for the thing people actually want to do after a download
(retag, move into a library, notify something, write a sidecar file):

  - Everything arrives as text. Filenames with quotes, apostrophes,
    semicolons or newlines have to be escaped correctly by whoever wrote
    the template, and an album title is not a safe string.
  - It runs once per batch, so per-track work means parsing whatever the
    command can see on disk afterwards.
  - It cannot be reached safely from the GUI or the web API, precisely
    because "run this shell string" is not something a request should get
    to choose (see app.py's POST_COMMAND_ENV).

A hook takes the same information as typed Python objects, per track, and
carries no shell at all.

Writing one
-----------
    # mylib/hooks.py
    from SpotiFLAC.core.models import DownloadResult, TrackMetadata

    def on_track(result: DownloadResult, metadata: TrackMetadata) -> None:
        if result.success:
            print(metadata.title, "->", result.file_path)

Then: ``spotiflac <url> --post-hook mylib.hooks:on_track`` (repeatable), or
``post_download_hooks=["mylib.hooks:on_track"]`` from the API.

`async def` hooks work too and are awaited.

Contract
--------
Hooks are called for every finished track, successful or not — check
`result.success`. They run after the file is in its final location, so
`result.file_path` is the real path, not a `.part`.

A hook that raises is logged and skipped: a broken notifier must not fail a
download that already succeeded. A hook that blocks, blocks — they run on
the event loop's thread pool if synchronous, so slow work is fine, but an
infinite loop will stall the batch.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
from collections.abc import Callable, Sequence
from typing import Any

logger = logging.getLogger(__name__)

HookSpec = str
Hook = Callable[..., Any]


class HookLoadError(ValueError):
    """A hook spec could not be resolved to a callable."""


def load_hook(spec: HookSpec) -> Hook:
    """Resolves ``"package.module:function"`` to the callable itself.

    Raises HookLoadError with the reason. Deliberately strict and eager: a
    typo in a hook name should fail when the run is configured, not silently
    do nothing after a two-hour discography download.
    """
    text = str(spec).strip()
    if ":" not in text:
        msg = (
            f"Invalid hook {spec!r}: expected 'module.path:function', "
            "e.g. 'mylib.hooks:on_track'"
        )
        raise HookLoadError(msg)

    module_name, _, attr = text.partition(":")
    if not module_name or not attr:
        msg = f"Invalid hook {spec!r}: both a module and a function are required"
        raise HookLoadError(msg)

    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        msg = f"Could not import '{module_name}' for hook {spec!r}: {exc}"
        raise HookLoadError(msg) from exc

    target: Any = module
    for part in attr.split("."):
        try:
            target = getattr(target, part)
        except AttributeError as exc:
            msg = f"'{module_name}' has no attribute '{attr}' (hook {spec!r})"
            raise HookLoadError(msg) from exc

    if not callable(target):
        msg = (
            f"Hook {spec!r} resolved to {type(target).__name__}, which is not callable"
        )
        raise HookLoadError(msg)
    return target


def load_hooks(specs: Sequence[HookSpec | Hook] | None) -> list[Hook]:
    """Resolves every spec up front, so configuration errors surface at the
    start of a run rather than after the first track.

    An entry that is already callable passes through untouched. That is how
    in-process hooks reach the same pipeline as CLI-configured ones —
    core/report.RunReport registers itself this way for `--json`, instead of
    the downloader growing a second, parallel notion of "tell me about each
    track".
    """
    hooks: list[Hook] = []
    for spec in specs or []:
        hooks.append(spec if callable(spec) else load_hook(spec))
    return hooks


async def run_hooks(
    hooks: Sequence[Hook],
    result: Any,
    metadata: Any,
) -> None:
    """Invokes each hook with (result, metadata). Never raises.

    Synchronous hooks are run in a worker thread: a hook that writes to a
    library database or shells out to beets would otherwise block the event
    loop, and hooks are exactly the place where people put slow work.
    """
    for hook in hooks:
        name = getattr(hook, "__qualname__", repr(hook))
        try:
            # inspect, not asyncio: asyncio.iscoroutinefunction is
            # deprecated and removed in 3.16.
            if inspect.iscoroutinefunction(hook):
                await hook(result, metadata)
            else:
                # Checking the *result* as well as the function covers the
                # shapes iscoroutinefunction says no to but which are still
                # async: functools.partial around a coroutine function, and
                # a class with `async def __call__`. Without this they went
                # to a thread, returned an un-awaited coroutine, and the
                # hook silently never ran.
                outcome = await asyncio.to_thread(hook, result, metadata)
                if inspect.isawaitable(outcome):
                    await outcome
        except Exception:
            # Logged, not raised: the download already happened, and a
            # failing notifier must not turn a completed track into a failed
            # one. exc_info so the hook author gets a real traceback.
            logger.exception("[hooks] '%s' raised; continuing", name)
