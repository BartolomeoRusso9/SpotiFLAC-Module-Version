"""Storage hardening for the two files that hold trust material:
~/.spotiflac/web_users.json (account hashes) and trusted_keys.json (the
Ed25519 root of trust for extension signatures).

Both used to be written with a plain write_text(), which truncates the
destination first — so an interrupted write left an empty or partial file
rather than the previous contents — and both inherited whatever the process
umask happened to be.
"""

from __future__ import annotations

import json
import stat
import time

import pytest

from SpotiFLAC.core import web_users
from SpotiFLAC.core.atomic_io import write_json_atomic, write_private_json
from SpotiFLAC.extensions import trust


@pytest.fixture(autouse=True)
def _isolated_stores(tmp_path, monkeypatch):
    monkeypatch.setattr(web_users, "USERS_FILE", tmp_path / "sf" / "web_users.json")
    monkeypatch.setattr(
        trust, "TRUSTED_KEYS_FILE", tmp_path / "sf" / "trusted_keys.json"
    )


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


# ── atomic_io ──────────────────────────────────────────────────────────────


def test_a_failed_write_leaves_the_previous_contents_intact(tmp_path) -> None:
    target = tmp_path / "state.json"
    write_json_atomic(target, {"generation": 1})

    class Unserialisable:
        pass

    with pytest.raises(TypeError):
        write_json_atomic(target, {"generation": 2, "bad": Unserialisable()})

    assert json.loads(target.read_text()) == {"generation": 1}


def test_a_failed_write_leaves_no_temp_files_behind(tmp_path) -> None:
    target = tmp_path / "state.json"
    write_json_atomic(target, {"ok": True})
    with pytest.raises(TypeError):
        write_json_atomic(target, {"bad": object()})
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


@pytest.mark.skipif(not hasattr(stat, "S_IMODE"), reason="POSIX modes only")
def test_private_writes_are_owner_only_and_public_ones_are_not(tmp_path) -> None:
    private = tmp_path / "secret.json"
    public = tmp_path / "cache.json"
    write_private_json(private, {"k": "v"})
    write_json_atomic(public, {"k": "v"})

    # mkstemp always creates 0600; a public file has to be widened back to
    # what an ordinary write would have produced, or cache files silently
    # become owner-only.
    assert _mode(private) == 0o600
    assert _mode(public) == 0o644


# ── web_users ──────────────────────────────────────────────────────────────


def test_user_store_is_not_world_readable() -> None:
    web_users.create_user("alice", "alice-password")
    assert _mode(web_users.USERS_FILE) == 0o600
    assert _mode(web_users.USERS_FILE.parent) == 0o700


def test_verify_password_still_works() -> None:
    web_users.create_user("alice", "alice-password")
    assert web_users.verify_password("alice", "alice-password") is True
    assert web_users.verify_password("alice", "wrong") is False
    assert web_users.verify_password("nobody", "wrong") is False


def test_unknown_username_costs_the_same_as_a_known_one() -> None:
    """Returning early for an unknown account answered ~1000x faster than a
    known one, which turns the login form into an account-enumeration oracle.
    """
    web_users.create_user("alice", "alice-password")

    def elapsed(username: str) -> float:
        # Best-of-3: this measures a deliberate ~100ms of PBKDF2, so scheduler
        # noise is small next to the signal, but taking the minimum keeps the
        # test from flaking on a loaded CI runner.
        timings = []
        for _ in range(3):
            start = time.perf_counter()
            web_users.verify_password(username, "some-wrong-password")
            timings.append(time.perf_counter() - start)
        return min(timings)

    known = elapsed("alice")
    unknown = elapsed("does-not-exist")
    ratio = max(known, unknown) / max(min(known, unknown), 1e-9)
    assert ratio < 3, f"login timing still distinguishes accounts ({ratio:.1f}x)"


# ── trust store ────────────────────────────────────────────────────────────


def test_trust_store_is_not_world_readable() -> None:
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

    trust.add_trusted_key("alice", pub_b64)
    assert _mode(trust.TRUSTED_KEYS_FILE) == 0o600
    assert trust.list_trusted_keys() == [{"name": "alice", "public_key_b64": pub_b64}]
