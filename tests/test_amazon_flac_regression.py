import asyncio
import os
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from SpotiFLAC.core import flac_validation
from SpotiFLAC.providers.amazon import AmazonProvider


class AmazonFlacRegressionTests(unittest.TestCase):
    @patch("asyncio.create_subprocess_exec")
    @patch("os.path.exists", return_value=True)
    def test_remux_to_flac_uses_explicit_flac_codec(
        self,
        mock_exists,
        mock_exec,
    ) -> None:
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0
        mock_exec.return_value = mock_proc

        provider = AmazonProvider()
        result = asyncio.run(provider._remux_to_flac("input.m4a", "output.flac"))

        assert result
        args = mock_exec.call_args.args
        assert "-map" in args
        assert args[args.index("-map") + 1] == "0:a:0"
        assert "-c:a" in args
        assert args[args.index("-c:a") + 1] == "flac"

    @patch("SpotiFLAC.core.flac_validation.shutil.which", return_value="/usr/bin/flac")
    @patch("SpotiFLAC.core.flac_validation.subprocess.run")
    def test_validate_flac_file_rejects_failed_integrity_check(
        self,
        mock_run,
        mock_which,
    ) -> None:
        with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as tmp:
            tmp.write(b"fake-flac")
            tmp_path = tmp.name

        try:
            mock_run.side_effect = [
                subprocess.CompletedProcess(
                    args=["ffmpeg"],
                    returncode=0,
                    stdout="",
                    stderr="",
                ),
                subprocess.CompletedProcess(
                    args=["flac"],
                    returncode=1,
                    stdout="",
                    stderr="integrity check failed",
                ),
            ]

            is_valid, error_msg = flac_validation.validate_flac_file(tmp_path)

            assert not is_valid
            assert "FLAC integrity test failed" in error_msg
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    @patch("SpotiFLAC.core.flac_validation.shutil.which", return_value=None)
    @patch("SpotiFLAC.core.flac_validation.subprocess.run")
    def test_validate_flac_file_fails_when_flac_binary_missing(
        self,
        mock_run,
        mock_which,
    ) -> None:
        with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as tmp:
            tmp.write(b"fake-flac")
            tmp_path = tmp.name

        try:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["ffmpeg"],
                returncode=0,
                stdout="",
                stderr="",
            )

            is_valid, error_msg = flac_validation.validate_flac_file(tmp_path)

            assert not is_valid
            assert "flac binary not found" in error_msg.lower()
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
