import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from SpotiFLAC.providers.base import BaseProvider


class DummyProvider(BaseProvider):
    name = "dummy"

    async def download_track_async(self, metadata, output_dir, **kwargs) -> None:
        pass


class BaseProviderTests(unittest.TestCase):
    @patch("asyncio.create_subprocess_exec")
    def test_run_ffprobe_executes_successfully(self, mock_exec) -> None:
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"some stdout", b"some stderr")
        mock_proc.returncode = 0
        mock_exec.return_value = mock_proc

        provider = DummyProvider()
        rc, stdout, stderr = asyncio.run(provider._run_ffprobe("ffprobe", "-version"))

        assert rc == 0
        assert stdout == "some stdout"
        assert stderr == "some stderr"
        mock_exec.assert_called_once_with(
            "ffprobe",
            "-version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    @patch("asyncio.create_subprocess_exec")
    def test_run_ffmpeg_executes_successfully(self, mock_exec) -> None:
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"some ffmpeg output", b"")
        mock_proc.returncode = 0
        mock_exec.return_value = mock_proc

        provider = DummyProvider()
        rc, stdout, stderr = asyncio.run(provider._run_ffmpeg("ffmpeg", "-version"))

        assert rc == 0
        assert stdout == "some ffmpeg output"
        assert stderr == ""
        mock_exec.assert_called_once_with(
            "ffmpeg",
            "-version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    @patch("SpotiFLAC.providers.deezer.fetch_mb_metadata_async")
    @patch("SpotiFLAC.providers.deezer.shutil.move")
    @patch("SpotiFLAC.providers.deezer.validate_downloaded_track_async", create=True)
    @patch("SpotiFLAC.providers.deezer.embed_metadata_async")
    @patch("SpotiFLAC.providers.deezer.DeezerProvider._get_track_by_isrc_async")
    @patch("SpotiFLAC.providers.deezer.DeezerProvider._download_flac_raw_async")
    def test_deezer_unlinks_dest_on_failure(
        self,
        mock_dl,
        mock_isrc,
        mock_embed,
        mock_validate,
        mock_move,
        mock_mb,
    ) -> None:
        from unittest.mock import MagicMock

        from SpotiFLAC.core.models import TrackMetadata
        from SpotiFLAC.providers.deezer import DeezerProvider

        # Setup mocks
        mock_isrc.return_value = {"isrc": "USUM71703861"}
        mock_dl.return_value = {"file_path": "dummy.flac", "extension": "flac"}
        mock_validate.return_value = (True, "")
        mock_embed.side_effect = Exception("Embedding failed")

        provider = DeezerProvider()

        mock_dest = MagicMock()
        mock_dest.suffix = ".flac"
        mock_dest.exists.return_value = True

        with patch.object(provider, "_build_output_path", return_value=mock_dest):
            meta = TrackMetadata(
                id="123",
                title="Test",
                artists="Artist",
                album="Album",
                album_artist="Artist",
            )
            meta.isrc = "USUM71703861"

            res = asyncio.run(provider.download_track_async(meta, "out"))

            assert not res.success
            mock_dest.unlink.assert_called_once()


if __name__ == "__main__":
    unittest.main()
