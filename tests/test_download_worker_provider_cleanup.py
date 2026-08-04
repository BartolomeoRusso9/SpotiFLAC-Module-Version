import asyncio

from SpotiFLAC.downloader import DownloadOptions, DownloadWorker


class DummyProvider:
    name = "dummy"

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_run_async_closes_providers_on_success(tmp_path) -> None:
    opts = DownloadOptions(output_dir=str(tmp_path))
    worker = DownloadWorker(tracks=[], opts=opts)
    dummy = DummyProvider()
    worker._providers = [dummy]

    asyncio.run(worker.run_async())

    assert dummy.closed


def test_run_async_closes_providers_on_exception(tmp_path) -> None:
    opts = DownloadOptions(output_dir=str(tmp_path))
    worker = DownloadWorker(tracks=[], opts=opts)
    dummy = DummyProvider()
    worker._providers = [dummy]

    async def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    worker._run_downloads_async = boom

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(worker.run_async())

    assert dummy.closed
