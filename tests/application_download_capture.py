from __future__ import annotations

from types import SimpleNamespace


class CapturingDownloadService:
    def __init__(self, *, provider_executor=None, seen=None, **_kwargs):
        self._provider_executor = provider_executor
        self._seen = seen if seen is not None else []

    async def download(self, request):
        options = self._provider_executor.downloader._opts
        payload = dict(vars(options))
        sources = list(request.sources)
        payload["url"] = sources if len(sources) > 1 else sources[0]
        payload["batch_tracks"] = len(sources) > 1 or bool(request.prefetched)
        payload["prefetched_tracks"] = (
            dict(request.prefetched) if request.prefetched else None
        )
        self._seen.append(payload)
        return SimpleNamespace()


def capture_service(monkeypatch, seen):
    class Service(CapturingDownloadService):
        def __init__(self, **kwargs):
            super().__init__(seen=seen, **kwargs)

    monkeypatch.setattr("SpotiFLAC.application.DownloadService", Service)
