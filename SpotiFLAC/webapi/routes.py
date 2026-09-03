"""webapi/routes.py — the `/api/v1` endpoints.

Transport only, exactly like webapp.py: nothing here decides anything about
downloading, following an artist or classifying a library. Every handler
validates its input against webapi/schemas.py, calls the same core module the
CLI and the GUI call, and shapes the result back into a declared model.

Auth is not reimplemented here. Every route lives under `/api/`, which is the
prefix webapp.py's token middleware and multi-user session gate already
cover — so this surface is exactly as protected as the RPC bridge next to it,
and there is no second implementation to keep in agreement.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from .schemas import (
    ApiInfo,
    CsvResolveRequest,
    CsvResolveResponse,
    CsvResolvedRow,
    CsvUnresolvedRow,
    DownloadRecordOut,
    DownloadRequest,
    ErrorResponse,
    ExtensionHealthResponse,
    HistoryResponse,
    JobListResponse,
    JobOut,
    LibraryDuplicatesRequest,
    LibraryDuplicatesResponse,
    LibraryScanRequest,
    LibraryScanResponse,
    ResolveRequest,
    ResolveResponse,
    SearchResponse,
    StatsResponse,
    SubscriptionCheckOut,
    SubscriptionCheckResponse,
    SubscriptionCreate,
    SubscriptionListResponse,
    SubscriptionOut,
    TrackOut,
)

logger = logging.getLogger(__name__)

_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
}


@dataclass
class ApiDeps:
    """What the router needs from the app that mounts it.

    Passed in rather than imported so this module has no opinion about how
    the server is put together — and so tests can mount the router over a
    fake without standing up the whole `--web` app.
    """

    api_for: Callable[[Request], Any]
    multiuser: bool = False
    token_required: bool = False
    job_queue: Any = None
    #: Only set in multi-user mode; None means "nobody in particular".
    username_for: Callable[[Request], str | None] = lambda _request: None


def _owner(deps: ApiDeps, request: Request) -> str:
    return (deps.username_for(request) or "") if deps.multiuser else ""


def _confined_path(path: str, download_dir: str) -> str:
    """A request's path, resolved, or a 400 if it escapes the download folder.

    The path comes from a request, so it is confined the same way
    /api/browse-folder confines its own: a caller must not be able to
    enumerate — or, for the routes that go on to act, touch — the filesystem
    by asking about "/".
    """
    import os
    from pathlib import Path

    root = os.path.realpath(str(Path(download_dir).expanduser()))
    # The request's own path stays a plain string until it has been checked.
    # Building a Path (or anything else that can touch a disk) out of it
    # first would mean the caller's value had already been turned into a
    # handle on whatever they named, with the check an afterthought — so
    # every step to the check below is string arithmetic, and nothing here
    # reaches the filesystem except realpath, which only reads.
    #
    # A relative path is taken against the download folder rather than
    # against whatever directory the server happens to have been started in,
    # which is not something a caller can see or reason about. An absolute
    # one is used as given. Either way it is the containment check below
    # that decides — never the join, which an absolute path wins outright.
    candidate = os.path.expanduser(path)
    if not os.path.isabs(candidate):
        candidate = os.path.join(root, candidate)
    resolved = os.path.realpath(candidate)
    # Compared as a prefix of the *resolved* path, so a symlink or a `..`
    # cannot smuggle the answer past this: realpath has already flattened
    # both. `os.path.join(root, "")` is root with exactly one trailing
    # separator, which is what stops "/musicians" passing for "/music" and
    # still does the right thing when root is "/".
    prefix = os.path.join(root, "")
    if not (resolved == root or resolved.startswith(prefix)):
        raise _fail(
            400,
            "That path is outside this instance's download folder.",
            "Scans are confined to the folder this account downloads into.",
        )
    return resolved


def _fail(status: int, message: str, detail: str | None = None) -> HTTPException:
    """An HTTPException whose body matches ErrorResponse.

    Messages here are written for the caller. Anything with internals in it —
    a traceback, a path, a library version — is logged server-side and
    replaced with something generic, the same rule the RPC bridge follows.

    The dict detail is rendered as the top-level body by `_ErrorShapeRoute`
    below; FastAPI's default handler would otherwise nest it under a second
    "detail" key and break the ErrorResponse shape these routes declare.
    """
    body: dict[str, Any] = {"error": message}
    if detail:
        body["detail"] = detail
    return HTTPException(status_code=status, detail=body)


class _ErrorShapeRoute(APIRoute):
    """Renders an HTTPException raised with a dict detail (see `_fail`) as that
    dict directly, so the response body matches the declared ErrorResponse
    model instead of being wrapped in an extra `{"detail": ...}`."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except StarletteHTTPException as exc:
                if isinstance(exc.detail, dict):
                    return JSONResponse(
                        exc.detail,
                        status_code=exc.status_code,
                        headers=getattr(exc, "headers", None),
                    )
                raise

        return handler


def build_v1_router(deps: ApiDeps) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["v1"], route_class=_ErrorShapeRoute)

    # ── Instance ──────────────────────────────────────────────────────────

    @router.get("/info", response_model=ApiInfo, summary="What this instance is")
    async def info(request: Request) -> ApiInfo:
        api = deps.api_for(request)
        return ApiInfo(
            version=getattr(api, "app_version", "unknown"),
            multiuser=deps.multiuser,
            authenticated=deps.multiuser or deps.token_required,
        )

    # ── Metadata ──────────────────────────────────────────────────────────

    @router.post(
        "/resolve",
        response_model=ResolveResponse,
        responses=_ERRORS,
        summary="Resolve a URL to its tracks, without downloading anything",
    )
    async def resolve(payload: ResolveRequest) -> ResolveResponse:
        from ..core.spotify_metadata import SpotifyMetadataClient, parse_spotify_url

        client = SpotifyMetadataClient()
        try:
            name, tracks, _cover, info = await client.get_url_async(
                payload.url, include_featuring=payload.include_featuring
            )
        except Exception as exc:
            logger.info("Could not resolve %s: %s", payload.url, exc)
            raise _fail(
                400,
                "Could not resolve that URL.",
                "It may be private, unsupported, or not a media link.",
            ) from exc

        try:
            # Off the loop: parse_spotify_url() issues a blocking HTTP
            # request to expand a share short link (see resolve_short_link),
            # and this route is already running on the event loop.
            parsed = await asyncio.to_thread(parse_spotify_url, payload.url)
            kind = parsed["type"]
        except Exception:
            # A non-Spotify link that link_resolver handled: it resolved
            # fine, it just has no Spotify URL shape to classify.
            kind = str(info.get("type", "")) if isinstance(info, dict) else ""

        return ResolveResponse(
            name=name or "",
            kind=kind,
            total=len(tracks),
            tracks=[TrackOut.from_metadata(t) for t in tracks],
        )

    @router.get(
        "/search",
        response_model=SearchResponse,
        responses=_ERRORS,
        summary="Search Spotify for tracks",
    )
    async def search(
        q: str = Query(min_length=1, description="Free-text query."),
        limit: int = Query(default=20, ge=1, le=50),
    ) -> SearchResponse:
        from ..core.spotify_metadata import SpotifyMetadataClient

        try:
            results = await SpotifyMetadataClient().search_async(q, limit=limit)
        except Exception as exc:
            logger.info("Search failed for %r: %s", q, exc)
            raise _fail(500, "The search could not be completed.") from exc

        return SearchResponse(
            query=q,
            tracks=[TrackOut.from_metadata(t) for t in results.get("tracks") or []],
        )

    # ── Downloads ─────────────────────────────────────────────────────────

    @router.post(
        "/downloads",
        response_model=JobOut,
        status_code=202,
        responses=_ERRORS,
        summary="Queue a download",
    )
    async def create_download(request: Request, payload: DownloadRequest) -> JobOut:
        """Accepted, not completed — 202 with a job to poll.

        A download takes minutes; holding the connection open for it would
        make every client's timeout the real limit on album size.
        """
        owner = _owner(deps, request)
        api = deps.api_for(request)

        config: dict[str, Any] = {"quality": payload.quality}
        if payload.services:
            config["services"] = payload.services
        if payload.output_dir and not deps.multiuser:
            # Honoured only in single-user mode: in multi-user mode letting a
            # request pick the destination would let one account write into
            # another's folder, which is the isolation ApiRegistry exists to
            # provide.
            config["output_dir"] = payload.output_dir

        if deps.job_queue is not None:
            from ..core.job_queue import QueueFullError

            try:
                job = deps.job_queue.submit(
                    owner, {"owner": owner, "url": payload.url, "config": config}
                )
            except QueueFullError as exc:
                raise _fail(
                    429,
                    "Too many downloads already queued.",
                    f"{exc.pending} queued, limit {exc.limit}.",
                ) from exc
            except Exception as exc:
                # A quota_check refusal (see core/web_users.quota_check)
                # arrives here; its message is written for the account holder.
                raise _fail(429, str(exc)) from exc
            return JobOut.from_job(job)

        # Single-user mode has no queue: dispatch onto the same background
        # thread the GUI uses, and answer with a synthetic job so the response
        # shape does not change depending on how the server was started.
        await run_in_threadpool(api.fetch_metadata, payload.url)
        return JobOut(
            id="direct",
            owner=owner,
            status="running",
            created_at=time.time(),
            payload={"url": payload.url},
        )

    @router.get(
        "/downloads",
        response_model=JobListResponse,
        summary="Jobs belonging to the caller",
    )
    async def list_downloads(request: Request) -> JobListResponse:
        if deps.job_queue is None:
            return JobListResponse(jobs=[])
        owner = _owner(deps, request)
        jobs = (
            deps.job_queue.list_for(owner)
            if deps.multiuser
            else deps.job_queue.list_all()
        )
        return JobListResponse(jobs=[JobOut.from_job(j) for j in jobs])

    @router.get(
        "/downloads/{job_id}",
        response_model=JobOut,
        responses=_ERRORS,
        summary="One job",
    )
    async def get_download(request: Request, job_id: str) -> JobOut:
        if deps.job_queue is None:
            raise _fail(404, "This instance has no download queue.")
        job = deps.job_queue.get(job_id)
        if job is None:
            raise _fail(404, "No such job.")
        if deps.multiuser and job.owner != _owner(deps, request):
            # 404 rather than 403: whether someone else's job id exists is
            # not something an account should be able to probe for.
            raise _fail(404, "No such job.")
        return JobOut.from_job(job)

    @router.get(
        "/history",
        response_model=HistoryResponse,
        summary="What this instance has actually downloaded",
    )
    async def history(
        request: Request, limit: int = Query(default=100, ge=1, le=1000)
    ) -> HistoryResponse:
        from ..core import download_log

        owner = _owner(deps, request) if deps.multiuser else None
        records = await run_in_threadpool(download_log.recent, limit, owner=owner)
        totals = await run_in_threadpool(download_log.totals, owner)
        return HistoryResponse(
            total=totals["tracks"],
            downloads=[
                DownloadRecordOut(
                    id=r.id,
                    title=r.title,
                    artist=r.artist,
                    album=r.album,
                    isrc=r.isrc,
                    provider=r.provider,
                    format=r.format,
                    bytes=r.bytes,
                    success=r.success,
                    downloaded_at=r.downloaded_at,
                )
                for r in records
            ],
        )

    @router.get(
        "/stats",
        response_model=StatsResponse,
        summary="The download log as a dashboard",
    )
    async def download_stats(
        request: Request,
        year: int | None = Query(default=None, ge=1970, le=2999),
        days: int | None = Query(default=None, ge=1, le=3650),
        top: int = Query(default=10, ge=1, le=100),
    ) -> StatsResponse:
        """Totals, rankings and activity for one period.

        Read-only and derived entirely from what this instance has already
        downloaded — `year` and `days` are alternative ways to name the
        period, and giving neither covers all of it.
        """
        from ..core import stats

        owner = _owner(deps, request) if deps.multiuser else None
        window = stats.parse_window(year=year, days=days)
        document = await run_in_threadpool(
            stats.wrapped, owner=owner, window=window, top=top
        )
        return StatsResponse(**document)

    # ── CSV input ─────────────────────────────────────────────────────────

    @router.post(
        "/csv/resolve",
        response_model=CsvResolveResponse,
        responses=_ERRORS,
        summary="Turn a CSV of tracks into links",
    )
    async def resolve_csv(payload: CsvResolveRequest) -> CsvResolveResponse:
        """Parses a CSV and matches the rows that carry no link.

        Nothing is queued here: the caller reviews the matches and then posts
        the URLs it accepts to `/downloads`, so a wrong match is something to
        notice rather than something already on disk.
        """
        from ..core import csv_source
        from ..core.errors import SpotiflacError

        try:
            document = await run_in_threadpool(
                csv_source.read_text,
                payload.content,
                name=payload.name,
                delimiter=payload.delimiter,
            )
        except SpotiflacError as exc:
            raise _fail(400, exc.message) from exc

        resolution = await csv_source.resolve_rows(
            document.rows, document=document, min_score=payload.min_score
        )
        return CsvResolveResponse(
            rows=len(document.rows),
            resolved=[
                CsvResolvedRow(**entry.to_dict()) for entry in resolution.resolved
            ],
            unresolved=[
                CsvUnresolvedRow(**entry.to_dict()) for entry in resolution.unresolved
            ],
            urls=resolution.urls,
        )

    # ── Subscriptions ─────────────────────────────────────────────────────

    @router.get(
        "/subscriptions",
        response_model=SubscriptionListResponse,
        summary="Followed artists",
    )
    async def list_subscriptions(request: Request) -> SubscriptionListResponse:
        from ..core import subscriptions

        owner = _owner(deps, request) if deps.multiuser else None
        rows = await run_in_threadpool(subscriptions.list_all, owner=owner)
        return SubscriptionListResponse(
            subscriptions=[SubscriptionOut(**s.to_dict()) for s in rows]
        )

    @router.post(
        "/subscriptions",
        response_model=SubscriptionOut,
        status_code=201,
        responses=_ERRORS,
        summary="Follow an artist",
    )
    async def create_subscription(
        request: Request, payload: SubscriptionCreate
    ) -> SubscriptionOut:
        from ..core import subscriptions

        api = deps.api_for(request)
        try:
            sub = await run_in_threadpool(
                lambda: subscriptions.add(
                    payload.url,
                    name=payload.name,
                    include_groups=payload.include_groups,
                    owner=_owner(deps, request),
                    output_dir=api.download_dir,
                )
            )
        except subscriptions.SubscriptionError as exc:
            raise _fail(400, str(exc)) from exc
        return SubscriptionOut(**sub.to_dict())

    @router.delete(
        "/subscriptions/{subscription_id}",
        status_code=204,
        responses=_ERRORS,
        summary="Unfollow",
    )
    async def delete_subscription(request: Request, subscription_id: str) -> None:
        from ..core import subscriptions

        existing = await run_in_threadpool(subscriptions.get, subscription_id)
        if existing is None or (
            deps.multiuser and existing.owner != _owner(deps, request)
        ):
            raise _fail(404, "No such subscription.")
        await run_in_threadpool(subscriptions.remove, subscription_id)

    @router.post(
        "/subscriptions/check",
        response_model=SubscriptionCheckResponse,
        summary="Check every followed artist for new releases",
    )
    async def check_subscriptions(
        request: Request,
        backfill: bool = Query(
            default=False,
            description="Treat a first check's existing catalogue as new "
            "rather than recording it as already-seen.",
        ),
    ) -> SubscriptionCheckResponse:
        from ..core import subscriptions

        owner = _owner(deps, request) if deps.multiuser else None
        results = await subscriptions.check_all_async(owner=owner, backfill=backfill)
        return SubscriptionCheckResponse(
            checked=len(results),
            new=sum(len(r.new) for r in results),
            results=[SubscriptionCheckOut(**r.to_dict()) for r in results],
        )

    # ── Extensions ────────────────────────────────────────────────────────

    @router.get(
        "/extensions",
        response_model=ExtensionHealthResponse,
        summary="Installed extensions and how reliable they have been",
    )
    async def extensions(request: Request) -> ExtensionHealthResponse:
        api = deps.api_for(request)
        data = await run_in_threadpool(api.get_extension_health)
        if "error" in data:
            raise _fail(500, "Could not read extension health.")
        return ExtensionHealthResponse(**data)

    # ── Library ───────────────────────────────────────────────────────────

    @router.post(
        "/library/scan",
        response_model=LibraryScanResponse,
        responses=_ERRORS,
        summary="Find files below a target quality (read-only)",
    )
    async def library_scan(
        request: Request, payload: LibraryScanRequest
    ) -> LibraryScanResponse:
        from ..core.library_upgrade import scan_library

        api = deps.api_for(request)
        resolved = _confined_path(payload.path, api.download_dir)

        report = await run_in_threadpool(
            scan_library,
            resolved,
            payload.target_quality,
            recursive=payload.recursive,
            verify_hires=payload.verify_hires,
        )
        return LibraryScanResponse(**report.to_dict())

    @router.post(
        "/library/duplicates",
        response_model=LibraryDuplicatesResponse,
        responses=_ERRORS,
        summary="Find duplicate recordings in a library (read-only)",
    )
    async def library_duplicates(
        request: Request, payload: LibraryDuplicatesRequest
    ) -> LibraryDuplicatesResponse:
        """Groups every copy of the same recording under `path`.

        Read-only, like /library/scan next to it: it reports which copy of
        each group is worth keeping and never acts on that. Resolving —
        quarantining or deleting — is deliberately not on this surface;
        `spotiflac --dedup-library … --dedup-apply` and the GUI panel are
        where files get moved, both behind a second explicit step.
        """
        from pathlib import Path

        from ..core.library_dedup import TRASH_DIRNAME, export_sqlite, scan_duplicates

        api = deps.api_for(request)
        resolved = _confined_path(payload.path, api.download_dir)

        report = await run_in_threadpool(
            scan_duplicates,
            resolved,
            recursive=payload.recursive,
            match=payload.match,
            duration_tolerance_s=payload.duration_tolerance_s,
            verify=payload.verify,
            similarity_threshold=payload.similarity_threshold,
        )

        database = ""
        if payload.export_db:
            try:
                database = str(
                    await run_in_threadpool(
                        export_sqlite,
                        report,
                        Path(resolved) / TRASH_DIRNAME / "library-index.db",
                    )
                )
            except (OSError, sqlite3.Error) as exc:
                # The scan succeeded; the index is a convenience on top of
                # it and must not turn a good answer into a 500. sqlite3's
                # errors are not OSErrors — a locked or unwritable database
                # raises OperationalError, which was going straight up.
                logger.warning("[api] could not write the dedup index: %s", exc)

        return LibraryDuplicatesResponse(**report.to_dict(), database=database)

    return router
