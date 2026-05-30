import os
import tempfile

from SpotiFLAC.core.models import DownloadResult, TrackMetadata
from SpotiFLAC.downloader import DownloadOptions, download_one


class DummyProvider:
    name = "dummy"

    def set_progress_callback(self, cb):
        pass

    def download_track(self, metadata, output_dir, **kwargs):
        return DownloadResult.ok("dummy", self.downloaded_path)


def test_output_path_override_moves_downloaded_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        download_dir = os.path.join(tmpdir, "files")
        os.makedirs(download_dir, exist_ok=True)

        downloaded_path = os.path.join(download_dir, "Track Title.flac")
        with open(downloaded_path, "wb") as f:
            f.write(b"dummy")

        target_path = os.path.join(download_dir, "wdfcew1234dfwsadqwe321ff.flac")
        opts = DownloadOptions(output_dir=tmpdir, output_path=target_path)

        provider = DummyProvider()
        provider.downloaded_path = downloaded_path

        metadata = TrackMetadata(
            id="test",
            title="Track Title",
            artists="Artist",
            album="Album",
            album_artist="Artist",
            isrc="ISRC",
            duration_ms=1000,
            external_url="",
        )

        result = download_one(metadata, tmpdir, [provider], opts)

        assert result.success is True
        assert result.file_path == target_path
        assert os.path.exists(target_path)
        assert not os.path.exists(downloaded_path)
