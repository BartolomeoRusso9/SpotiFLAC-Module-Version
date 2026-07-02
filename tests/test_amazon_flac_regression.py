import asyncio
import os
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from SpotiFLAC.core import flac_validation
from SpotiFLAC.providers.amazon import AmazonProvider


class AmazonFlacRegressionTests(unittest.TestCase):
    @patch("asyncio.create_subprocess_exec")
    @patch("os.path.exists", return_value=True)
    def test_remux_to_flac_uses_explicit_flac_codec(self, mock_exists, mock_exec):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0
        mock_exec.return_value = mock_proc

        provider = AmazonProvider()
        result = asyncio.run(provider._remux_to_flac("input.m4a", "output.flac"))

        self.assertTrue(result)
        args = mock_exec.call_args.args
        self.assertIn("-map", args)
        self.assertEqual(args[args.index("-map") + 1], "0:a:0")
        self.assertIn("-c:a", args)
        self.assertEqual(args[args.index("-c:a") + 1], "flac")

    @patch("SpotiFLAC.core.flac_validation.shutil.which", return_value="/usr/bin/flac")
    @patch("SpotiFLAC.core.flac_validation.subprocess.run")
    def test_validate_flac_file_rejects_failed_integrity_check(
        self, mock_run, mock_which
    ):
        with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as tmp:
            tmp.write(b"fake-flac")
            tmp_path = tmp.name

        try:
            mock_run.side_effect = [
                subprocess.CompletedProcess(args=["ffmpeg"], returncode=0, stdout="", stderr=""),
                subprocess.CompletedProcess(args=["flac"], returncode=1, stdout="", stderr="integrity check failed"),
            ]

            is_valid, error_msg = flac_validation.validate_flac_file(tmp_path)

            self.assertFalse(is_valid)
            self.assertIn("FLAC integrity test failed", error_msg)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    @patch("SpotiFLAC.core.flac_validation.shutil.which", return_value=None)
    @patch("SpotiFLAC.core.flac_validation.subprocess.run")
    def test_validate_flac_file_fails_when_flac_binary_missing(
        self, mock_run, mock_which
    ):
        with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as tmp:
            tmp.write(b"fake-flac")
            tmp_path = tmp.name

        try:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["ffmpeg"], returncode=0, stdout="", stderr=""
            )

            is_valid, error_msg = flac_validation.validate_flac_file(tmp_path)

            self.assertFalse(is_valid)
            self.assertIn("flac binary not found", error_msg.lower())
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    @patch("SpotiFLAC.providers.amazon.validate_and_repair_if_needed", return_value=(True, ""))
    @patch.object(AmazonProvider, "_remux_to_flac", new_callable=AsyncMock, return_value=True)
    @patch.object(AmazonProvider, "_get_codec", new_callable=AsyncMock, return_value="flac")
    @patch("SpotiFLAC.providers.amazon.AmazonProvider._do_request_with_retry", new_callable=AsyncMock)
    def test_spotbye_api_uses_flac_remux_helper(
        self, mock_do_request, mock_get_codec, mock_remux, mock_validate
    ):
        provider = AmazonProvider()
        provider._async_http = MagicMock()
        provider._async_http.stream_to_file = AsyncMock()

        with patch("SpotiFLAC.providers.amazon.get_amazon_endpoint", return_value="https://example.com"):
            response = MagicMock()
            response.status_code = 200
            response.headers = {"Content-Type": "application/json"}
            response.json.return_value = {
                "metadata": {},
                "streamUrl": "https://example.com/stream",
                "decryptionKey": "abc123",
            }
            mock_do_request.return_value = response

            with tempfile.TemporaryDirectory() as out_dir:
                result = asyncio.run(
                    provider._download_from_spotbye_api("B123456789", out_dir, "spotbye")
                )

        self.assertIsNotNone(result)
        mock_remux.assert_awaited_once()
