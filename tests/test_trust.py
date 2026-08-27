"""Tests for extensions/trust.py (Ed25519 registry-entry signature
verification) and the RegistryEntry.trust_tier it backs.
"""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from SpotiFLAC.extensions import trust
from SpotiFLAC.extensions.manager import RegistryEntry


def _generate_keypair() -> tuple[str, Ed25519PrivateKey]:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    pub_b64 = base64.b64encode(
        public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode()
    return pub_b64, private_key


def _sign(private_key: Ed25519PrivateKey, *fields: str) -> str:
    message = trust.canonical_message(*fields)
    return base64.b64encode(private_key.sign(message)).decode()


@pytest.fixture(autouse=True)
def _isolated_trust_store(tmp_path, monkeypatch):
    monkeypatch.setattr(trust, "TRUSTED_KEYS_FILE", tmp_path / "trusted_keys.json")


def test_add_list_remove_trusted_key_round_trip():
    pub_b64, _priv = _generate_keypair()
    assert trust.list_trusted_keys() == []

    trust.add_trusted_key("alice", pub_b64)
    keys = trust.list_trusted_keys()
    assert keys == [{"name": "alice", "public_key_b64": pub_b64}]

    assert trust.remove_trusted_key("alice") is True
    assert trust.list_trusted_keys() == []
    assert trust.remove_trusted_key("alice") is False


def test_add_trusted_key_rejects_garbage():
    with pytest.raises(trust.TrustKeyError):
        trust.add_trusted_key("bob", "not-valid-base64-or-a-key")


def test_add_trusted_key_rejects_empty_name_or_key():
    pub_b64, _priv = _generate_keypair()
    with pytest.raises(trust.TrustKeyError):
        trust.add_trusted_key("", pub_b64)
    with pytest.raises(trust.TrustKeyError):
        trust.add_trusted_key("alice", "")


def test_adding_same_name_twice_replaces_the_key():
    pub1, _ = _generate_keypair()
    pub2, _ = _generate_keypair()
    trust.add_trusted_key("alice", pub1)
    trust.add_trusted_key("alice", pub2)
    keys = trust.list_trusted_keys()
    assert len(keys) == 1
    assert keys[0]["public_key_b64"] == pub2


def test_verify_registry_entry_succeeds_for_a_trusted_signer():
    pub_b64, priv = _generate_keypair()
    trust.add_trusted_key("alice", pub_b64)

    sig = _sign(priv, "tidal-web", "1.0.0", "abc123", "https://example.com/ext.zip")
    signer = trust.verify_registry_entry(
        "tidal-web", "1.0.0", "abc123", "https://example.com/ext.zip", sig
    )
    assert signer == "alice"


def test_verify_registry_entry_fails_for_an_untrusted_signer():
    _pub_b64, priv = _generate_keypair()  # never added as trusted

    sig = _sign(priv, "tidal-web", "1.0.0", "abc123", "https://example.com/ext.zip")
    signer = trust.verify_registry_entry(
        "tidal-web", "1.0.0", "abc123", "https://example.com/ext.zip", sig
    )
    assert signer is None


def test_verify_registry_entry_fails_if_any_field_was_tampered_with():
    pub_b64, priv = _generate_keypair()
    trust.add_trusted_key("alice", pub_b64)
    sig = _sign(priv, "tidal-web", "1.0.0", "abc123", "https://example.com/ext.zip")

    # Version bumped after signing (or a MITM'd registry) — must not verify.
    signer = trust.verify_registry_entry(
        "tidal-web", "1.0.1", "abc123", "https://example.com/ext.zip", sig
    )
    assert signer is None


def test_verify_registry_entry_handles_missing_or_garbage_signature():
    pub_b64, _priv = _generate_keypair()
    trust.add_trusted_key("alice", pub_b64)

    assert trust.verify_registry_entry("id", "1.0", "sha", "url", None) is None
    assert trust.verify_registry_entry("id", "1.0", "sha", "url", "") is None
    assert (
        trust.verify_registry_entry("id", "1.0", "sha", "url", "not-base64!!") is None
    )


def test_verify_registry_entry_with_no_trusted_keys_configured_returns_none():
    _pub_b64, priv = _generate_keypair()
    sig = _sign(priv, "id", "1.0", "sha", "url")
    assert trust.verify_registry_entry("id", "1.0", "sha", "url", sig) is None


def test_trust_tier_labels():
    pub_b64, priv = _generate_keypair()
    trust.add_trusted_key("alice", pub_b64)
    sig = _sign(priv, "id", "1.0", "sha", "url")

    assert trust.trust_tier("id", "1.0", "sha", "url", sig) == "signed"
    assert trust.trust_tier("id", "1.0", "sha", "url", None) == "checksum-only"
    assert trust.trust_tier("id", "1.0", None, "url", None) == "unverified"
    # Signature present but doesn't verify (wrong signer/tampered) still
    # falls back to checksum-only rather than claiming "signed".
    assert trust.trust_tier("id", "1.0", "sha", "url", "bm90LWEtcmVhbC1zaWc=") == (
        "checksum-only"
    )


def test_registry_entry_trust_tier_property_matches_module_function():
    pub_b64, priv = _generate_keypair()
    trust.add_trusted_key("alice", pub_b64)
    sig = _sign(priv, "tidal-web", "1.0.0", "abc123", "https://example.com/ext.zip")

    entry = RegistryEntry(
        id="tidal-web",
        display_name="Tidal",
        version="1.0.0",
        description="",
        download_url="https://example.com/ext.zip",
        sha256="abc123",
        signature=sig,
    )
    assert entry.trust_tier == "signed"

    unsigned = RegistryEntry(
        id="tidal-web",
        display_name="Tidal",
        version="1.0.0",
        description="",
        download_url="https://example.com/ext.zip",
        sha256="abc123",
    )
    assert unsigned.trust_tier == "checksum-only"
