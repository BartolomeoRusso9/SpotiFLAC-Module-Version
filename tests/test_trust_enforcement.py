"""`--min-trust-tier` / $SPOTIFLAC_MIN_TRUST — refusing registry entries
below a chosen assurance level.

Before this existed, `trust_tier` was computed and then only ever rendered
as a badge: an entry whose signature failed to verify installed exactly like
one that verified. These tests are about the floor actually stopping things.
"""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from SpotiFLAC.extensions import trust
from SpotiFLAC.extensions.manager import (
    ExtensionManager,
    RegistryEntry,
    TrustRejectedError,
)

SHA = "a" * 64
URL = "https://example.invalid/ext.spotiflac-ext"


@pytest.fixture(autouse=True)
def _isolated_trust_store(tmp_path, monkeypatch):
    monkeypatch.setattr(trust, "TRUSTED_KEYS_FILE", tmp_path / "trusted_keys.json")
    monkeypatch.delenv(trust.MIN_TRUST_ENV, raising=False)


def _entry(ext_id="tidal", sha256=SHA, signature=None) -> RegistryEntry:
    return RegistryEntry(
        id=ext_id,
        display_name=ext_id,
        version="1.0.0",
        description="",
        download_url=URL,
        sha256=sha256,
        signature=signature,
    )


def _signed_entry(ext_id="tidal", *, trust_the_key: bool) -> RegistryEntry:
    key = Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode()
    if trust_the_key:
        trust.add_trusted_key("maintainer", pub_b64)

    signature = base64.b64encode(
        key.sign(trust.canonical_message(ext_id, "1.0.0", SHA, URL))
    ).decode()
    return _entry(ext_id, signature=signature)


def _manager(tmp_path, min_tier=None) -> ExtensionManager:
    return ExtensionManager(
        ext_dir=tmp_path / "ext",
        auto_install_downloads=False,
        min_trust_tier=min_tier,
    )


# ── tier ordering ──────────────────────────────────────────────────────────


def test_tiers_are_ordered_by_assurance() -> None:
    assert trust.meets_min_trust("signed", "unverified")
    assert trust.meets_min_trust("signed", "checksum-only")
    assert trust.meets_min_trust("checksum-only", "checksum-only")
    assert not trust.meets_min_trust("checksum-only", "signed")
    assert not trust.meets_min_trust("unverified", "checksum-only")


def test_an_unknown_tier_never_grants_trust() -> None:
    """A typo must fail closed. If `tier_rank` returned, say, 0 for junk, a
    misspelling in a registry entry would silently pass a "checksum-only"
    floor.
    """
    assert trust.tier_rank("signedd") == -1
    assert not trust.meets_min_trust("signedd", "unverified")


def test_a_misspelled_floor_is_rejected_rather_than_ignored() -> None:
    with pytest.raises(trust.TrustKeyError):
        trust.normalise_min_trust("signd")


# ── resolution ─────────────────────────────────────────────────────────────


def test_default_floor_keeps_the_historical_behaviour(tmp_path) -> None:
    """No registry ships with SpotiFLAC, so defaulting to anything above
    'unverified' would make a fresh install unable to install anything.
    """
    assert _manager(tmp_path).min_trust_tier == "unverified"


def test_environment_sets_the_floor(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(trust.MIN_TRUST_ENV, "signed")
    assert _manager(tmp_path).min_trust_tier == "signed"


def test_explicit_argument_beats_the_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(trust.MIN_TRUST_ENV, "signed")
    assert _manager(tmp_path, "checksum-only").min_trust_tier == "checksum-only"


# ── enforcement ────────────────────────────────────────────────────────────


def test_default_floor_accepts_everything(tmp_path) -> None:
    mgr = _manager(tmp_path)
    mgr.enforce_trust(_entry(sha256=None))  # unverified
    mgr.enforce_trust(_entry())  # checksum-only


def test_checksum_floor_rejects_an_entry_without_a_checksum(tmp_path) -> None:
    mgr = _manager(tmp_path, "checksum-only")
    mgr.enforce_trust(_entry())  # has sha256 — fine
    with pytest.raises(TrustRejectedError, match="unverified"):
        mgr.enforce_trust(_entry(sha256=None))


def test_signed_floor_rejects_a_merely_checksummed_entry(tmp_path) -> None:
    mgr = _manager(tmp_path, "signed")
    with pytest.raises(TrustRejectedError, match="checksum-only"):
        mgr.enforce_trust(_entry())


def test_signed_floor_accepts_a_signature_from_a_trusted_key(tmp_path) -> None:
    entry = _signed_entry(trust_the_key=True)
    assert entry.trust_tier == "signed"
    _manager(tmp_path, "signed").enforce_trust(entry)


def test_signed_floor_rejects_a_signature_from_an_untrusted_key(tmp_path) -> None:
    """The core case the whole scheme exists for: the entry *claims* a
    signature, but it verifies against no key we trust.
    """
    entry = _signed_entry(trust_the_key=False)
    assert entry.trust_tier == "checksum-only"

    with pytest.raises(TrustRejectedError) as exc:
        _manager(tmp_path, "signed").enforce_trust(entry)
    assert "did not verify" in str(exc.value)


def test_a_local_file_install_is_not_gated(tmp_path) -> None:
    """Raising the floor means "distrust the registry", not "you may no
    longer install your own extension from disk" — a local file has no
    registry entry to be signed against and could only ever score
    'unverified'.
    """
    import io
    import json
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps({"name": "local", "version": "1.0.0", "runtime": "javascript"}),
        )
        zf.writestr("index.js", "module.exports = {};")

    path = tmp_path / "local.spotiflac-ext"
    path.write_bytes(buf.getvalue())

    mgr = _manager(tmp_path, "signed")
    assert mgr.install_from_file(path).name == "local"
