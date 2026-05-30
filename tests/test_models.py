import os
import tempfile

from SpotiFLAC.core.models import DownloadResult


def test_download_result_ok():
    path = os.path.join("/tmp", "track.flac")
    result = DownloadResult.ok("dummy", path)

    assert result.success is True
    assert result.provider == "dummy"
    assert result.file_path == path
    assert result.format == "flac"
    assert result.skipped is False


def test_download_result_skipped_result():
    path = os.path.join("/tmp", "track.flac")
    result = DownloadResult.skipped_result("dummy", path, fmt="flac")

    assert result.success is True
    assert result.provider == "dummy"
    assert result.file_path == path
    assert result.format == "flac"
    assert result.skipped is True
