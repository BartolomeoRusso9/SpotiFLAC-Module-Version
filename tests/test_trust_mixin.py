"""Tests for api_mixins/trust.py — thin-wrapper behavior; the actual
crypto/logic is covered by tests/test_trust.py.
"""

from __future__ import annotations

from SpotiFLAC.api_mixins.trust import TrustMixin
from SpotiFLAC.app import SpotiFLAC_API
from SpotiFLAC.extensions import trust
from SpotiFLAC.webapp import ALLOWED_METHODS


def test_trust_methods_resolve_to_the_mixin() -> None:
    for name in ("get_trusted_keys", "add_trusted_key", "remove_trusted_key"):
        assert name in TrustMixin.__dict__
        assert getattr(SpotiFLAC_API, name).__qualname__.startswith("TrustMixin.")


def test_only_reading_the_trust_store_is_exposed_over_http() -> None:
    """The trust store is the root of trust for extension signatures: a
    caller who can add a key can sign their own extensions and have them
    verify. Reading the list is harmless; writing it must stay CLI-only
    (tools/registry_signing_cli.py), so it is not reachable through the
    same channel an untrusted caller can reach.
    """
    assert "get_trusted_keys" in ALLOWED_METHODS
    assert "add_trusted_key" not in ALLOWED_METHODS
    assert "remove_trusted_key" not in ALLOWED_METHODS


def test_add_get_remove_trusted_key_round_trip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(trust, "TRUSTED_KEYS_FILE", tmp_path / "trusted_keys.json")
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    pub_b64 = base64.b64encode(
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode()

    api = SpotiFLAC_API()
    assert api.get_trusted_keys() == []

    added = api.add_trusted_key("alice", pub_b64)
    assert added["ok"] is True
    assert len(added["keys"]) == 1

    removed = api.remove_trusted_key("alice")
    assert removed["ok"] is True
    assert api.get_trusted_keys() == []

    assert api.remove_trusted_key("alice")["ok"] is False


def test_add_trusted_key_reports_error_for_invalid_key(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(trust, "TRUSTED_KEYS_FILE", tmp_path / "trusted_keys.json")
    api = SpotiFLAC_API()
    result = api.add_trusted_key("alice", "garbage")
    assert result["ok"] is False
