import asyncio

from SpotiFLAC.core.models import DownloadResult, TrackMetadata
from SpotiFLAC.downloader import DownloadOptions, download_one_async


class DummyProvider:
    name = "dummy"

    def __init__(self) -> None:
        self._progress_cb = None
        self._stop_event = None

    def set_progress_callback(self, cb) -> None:
        self._progress_cb = cb

    def set_stop_event(self, ev) -> None:
        self._stop_event = ev

    async def download_track_async(self, metadata, output_dir, **kwargs):
        # Simulate long-running download that checks for stop_event
        for _i in range(10):
            if self._stop_event and self._stop_event.is_set():
                return DownloadResult.fail(self.name, "Cancelled")
            await asyncio.sleep(0.2)
        return DownloadResult.ok(self.name, "/tmp/fake.flac")


def test_download_one_timeout_triggers_cancellation() -> None:
    opts = DownloadOptions(output_dir="/tmp", track_max_retries=0, timeout_s=1)
    meta = TrackMetadata(
        id="t1",
        title="T1",
        artists="A",
        album="Album",
        album_artist="A",
    )
    provider = DummyProvider()
    result = asyncio.run(download_one_async(meta, "/tmp", [provider], opts))
    assert not result.success
    assert (
        "timed out" in (result.error or "").lower()
        or "cancel" in (result.error or "").lower()
    )
