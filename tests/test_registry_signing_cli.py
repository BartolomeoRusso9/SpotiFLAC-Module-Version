"""Tests for tools/registry_signing_cli.py (keygen + sign) — verifies its
output round-trips correctly through extensions/trust.py's verification,
since that's the entire point of the tool.
"""

from __future__ import annotations

import base64
import re

from SpotiFLAC.extensions import trust
from SpotiFLAC.tools import registry_signing_cli


def _run(monkeypatch, capsys, argv) -> str:
    monkeypatch.setattr("sys.argv", ["registry_signing_cli", *argv])
    code = registry_signing_cli.main(argv)
    return capsys.readouterr().out, code


def test_keygen_prints_a_usable_keypair(monkeypatch, capsys):
    out, code = _run(monkeypatch, capsys, ["keygen"])
    assert code == 0

    priv_match = re.search(r"Private key.*?\n\s+(\S+)", out, re.DOTALL)
    pub_match = re.search(r"Public key.*?\n\s+(\S+)", out, re.DOTALL)
    assert priv_match and pub_match

    # Both must be valid base64 of the right length for an Ed25519 key (32 bytes raw).
    assert len(base64.b64decode(priv_match.group(1))) == 32
    assert len(base64.b64decode(pub_match.group(1))) == 32


def test_sign_output_verifies_against_the_matching_public_key(
    monkeypatch, capsys, tmp_path
):
    monkeypatch.setattr(trust, "TRUSTED_KEYS_FILE", tmp_path / "trusted_keys.json")

    keygen_out, _ = _run(monkeypatch, capsys, ["keygen"])
    priv_b64 = re.search(r"Private key.*?\n\s+(\S+)", keygen_out, re.DOTALL).group(1)
    pub_b64 = re.search(r"Public key.*?\n\s+(\S+)", keygen_out, re.DOTALL).group(1)

    sign_out, code = _run(
        monkeypatch,
        capsys,
        [
            "sign",
            "--private-key",
            priv_b64,
            "--id",
            "tidal-web",
            "--version",
            "1.2.0",
            "--sha256",
            "deadbeef",
            "--download-url",
            "https://example.com/tidal-web.spotiflac-ext",
        ],
    )
    assert code == 0
    sig_match = re.search(r'"signature":\s*"([^"]+)"', sign_out)
    assert sig_match

    trust.add_trusted_key("maintainer", pub_b64)
    signer = trust.verify_registry_entry(
        "tidal-web",
        "1.2.0",
        "deadbeef",
        "https://example.com/tidal-web.spotiflac-ext",
        sig_match.group(1),
    )
    assert signer == "maintainer"


def test_sign_rejects_invalid_private_key(monkeypatch, capsys):
    out, code = _run(
        monkeypatch,
        capsys,
        [
            "sign",
            "--private-key",
            "not-a-real-key",
            "--id",
            "x",
            "--version",
            "1.0",
            "--sha256",
            "abc",
            "--download-url",
            "https://example.com/x.zip",
        ],
    )
    assert code == 1
