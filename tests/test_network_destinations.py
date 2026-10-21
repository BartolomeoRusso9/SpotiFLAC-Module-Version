from __future__ import annotations

import io
import json
import stat
import sys
import threading
import traceback
from types import SimpleNamespace

import pytest

from mornye.core.network_destinations import (
    DestinationError,
    DestinationStore,
    SMBBackend,
    normalize_mounted_path,
    validate_profile,
    _smb_relative_parts,
)


def _profile(**overrides):
    values = {
        "id": "destination1234",
        "name": "Home NAS",
        "host": "nas.example.net",
        "share": "Music",
        "subdirectory": "Mornye/Albums",
        "username": "music-user",
        "domain": "HOME",
    }
    values.update(overrides)
    return validate_profile(values)


class _RemoteWriter:
    def __init__(self, client, path, exclusive):
        self.client = client
        self.path = path
        self.exclusive = exclusive
        self.buffer = io.BytesIO()

    def __enter__(self):
        if self.exclusive and self.path in self.client.files:
            raise FileExistsError(self.path)
        return self

    def write(self, data):
        if self.client.fail_next_write:
            self.client.fail_next_write = False
            raise ConnectionError("credentials should not appear here")
        if self.client.deny_write:
            raise PermissionError("sensitive server error detail")
        return self.buffer.write(data)

    def flush(self):
        self.client.files[self.path] = self.buffer.getvalue()

    def __exit__(self, *_):
        if self.buffer.getvalue():
            self.client.files[self.path] = self.buffer.getvalue()
        self.buffer.close()


class _FakeSMBClient:
    def __init__(self):
        self.files = {}
        self.directories = {r"\\nas.example.net\Music"}
        self.sessions = []
        self.fail_next_write = False
        self.deny_write = False
        self.corrupt_reads = False

    def register_session(self, server, **kwargs):
        self.sessions.append((server, kwargs))

    def stat(self, path):
        if path in self.files:
            data = self.files[path]
            return SimpleNamespace(st_size=len(data), st_mode=stat.S_IFREG)
        if path in self.directories:
            return SimpleNamespace(st_size=0, st_mode=stat.S_IFDIR)
        raise FileNotFoundError(path)

    def makedirs(self, path, exist_ok=False):
        del exist_ok
        self.directories.add(path)

    def open_file(self, path, mode):
        if mode == "rb":
            if path not in self.files:
                raise FileNotFoundError(path)
            data = self.files[path]
            if self.corrupt_reads:
                data = data + b"corrupt"
            return io.BytesIO(data)
        if mode == "xb":
            return _RemoteWriter(self, path, exclusive=True)
        raise AssertionError(mode)

    def remove(self, path):
        del self.files[path]

    def rename(self, source, destination):
        replace_if_exists = False
        if destination in self.files and not replace_if_exists:
            raise FileExistsError(destination)
        self.files[destination] = self.files.pop(source)


def test_mounted_paths_preserve_unc_and_normalize_os_mounts(tmp_path):
    assert (
        normalize_mounted_path(r"\\nas\Music\Albums", platform="nt")
        == r"\\nas\Music\Albums"
    )
    assert normalize_mounted_path("Z:\\Music\\", platform="nt") == "Z:\\Music"
    assert normalize_mounted_path(str(tmp_path)) == str(tmp_path)
    with pytest.raises(DestinationError, match="absolute drive path or UNC"):
        normalize_mounted_path("relative\\music", platform="nt")
    with pytest.raises(DestinationError, match="absolute drive path or UNC"):
        normalize_mounted_path("\\Music", platform="nt")
    with pytest.raises(DestinationError, match="absolute drive path or UNC"):
        normalize_mounted_path(r"\\server", platform="nt")


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"host": "nas/share"}, "hostname or IP"),
        ({"share": "Music/../../Windows"}, "one SMB share name"),
        ({"subdirectory": "../outside"}, "within its share"),
        ({"subdirectory": "C:/outside"}, "within its share"),
        ({"timeout": 0}, "between 1 and 120"),
    ],
)
def test_profile_validation_rejects_invalid_network_targets(changes, message):
    values = {
        "id": "destination1234",
        "name": "Home NAS",
        "host": "nas.example.net",
        "share": "Music",
    }
    values.update(changes)
    with pytest.raises(DestinationError, match=message):
        validate_profile(values)


def test_ipv6_server_uses_bracketed_unc_path():
    profile = _profile(host="2001:db8::1", subdirectory="")
    backend = SMBBackend(profile, "user", "pass", client=_FakeSMBClient())

    assert backend.root == r"\\[2001:db8::1]\Music"


def test_smb_audio_listing_is_recursive_read_only_and_filters_unsafe_entries():
    profile = _profile(subdirectory="")
    root = r"\\nas.example.net\Music"
    nested = root + r"\Albums"

    class Entry:
        def __init__(self, name, mode, size=0):
            self.name = name
            self._info = SimpleNamespace(st_mode=mode, st_size=size, st_mtime=1234)

        def stat(self, follow_symlinks=False):
            assert follow_symlinks is False
            return self._info

    class ListingClient(_FakeSMBClient):
        def scandir(self, path):
            if path == root:
                return [
                    Entry("Albums", stat.S_IFDIR),
                    Entry("top.flac", stat.S_IFREG, 100),
                    Entry("notes.txt", stat.S_IFREG, 20),
                    Entry("link.flac", stat.S_IFLNK, 40),
                    Entry("../outside.flac", stat.S_IFREG, 50),
                    Entry(".hidden", stat.S_IFDIR),
                ]
            if path == nested:
                return [Entry("record.m4a", stat.S_IFREG, 200)]
            raise FileNotFoundError(path)

    client = ListingClient()
    backend = SMBBackend(profile, "user", "pass", client=client)
    files = backend.list_audio_files()

    assert [item["path"] for item in files] == [
        root + r"\Albums\record.m4a",
        root + r"\top.flac",
    ]
    assert files[0]["size"] == 200
    assert all(item["modified"] == 1234 for item in files)
    assert client.files == {}


def test_smb_open_audio_file_rejects_paths_outside_the_configured_share():
    backend = SMBBackend(
        _profile(subdirectory=""), "user", "pass", client=_FakeSMBClient()
    )

    with pytest.raises(DestinationError, match="outside this SMB destination"):
        backend.open_audio_file(r"\\other.example.net\Music\song.flac")


def test_smb_stats_scan_aggregates_tags_without_writing_to_the_share(monkeypatch):
    from mornye.core import smb_library_scan

    profile = _profile()
    files = [
        {"path": r"\\nas.example.net\Music\song-a.flac", "size": 100, "modified": 1},
        {"path": r"\\nas.example.net\Music\song-b.m4a", "size": 200, "modified": 2},
        {"path": r"\\nas.example.net\Music\broken.flac", "size": 50, "modified": 3},
    ]

    class FakeBackend:
        def __init__(self, selected_profile, username, password):
            assert selected_profile == profile
            assert (username, password) == ("user", "pass")
            self.writes = 0

        def list_audio_files(self):
            return files

        def open_audio_file(self, path):
            return io.BytesIO(path.encode())

    class Audio:
        def __init__(self, title, album, genre, date, duration):
            self.tags = {
                "title": [title],
                "artist": ["Queen"],
                "album": [album],
                "genre": [genre] if genre else [],
                "date": [date] if date else [],
            }
            self.info = SimpleNamespace(length=duration)

    def fake_mutagen_file(stream, easy):
        assert easy is True
        if stream.name.endswith("song-a.flac"):
            return Audio("First", "Night at the Opera", "Rock", "1975", 300)
        if stream.name.endswith("song-b.m4a"):
            return Audio("Second", "Night at the Opera", "", "", 240)
        return None

    monkeypatch.setattr(smb_library_scan, "SMBBackend", FakeBackend)
    monkeypatch.setattr(smb_library_scan.mutagen, "File", fake_mutagen_file)
    progress = []

    result = smb_library_scan.scan_smb_library(
        profile,
        "user",
        "pass",
        progress=lambda done, total: progress.append((done, total)),
    )

    assert result["source"] == "smb"
    assert result["source_name"] == "Home NAS"
    assert result["scan"] == {"files": 3, "readable": 2, "unreadable": 1}
    assert result["totals"]["tracks"] == 2
    assert result["totals"]["failed"] == 1
    assert result["totals"]["bytes"] == 350
    assert result["top_artists"][0]["name"] == "Queen"
    assert result["top_genres"]["entries"][0]["name"] == "Rock"
    assert result["decades"]["entries"][0]["name"] == "1970s"
    assert progress == [(3, 3)]


def test_profile_store_keeps_password_out_of_private_profile_file(tmp_path):
    store = DestinationStore(tmp_path / "destinations.json")
    profile = store.save(_profile().public_dict(), "top-secret", owner="alice")
    assert store.credentials(profile, owner="alice") == "top-secret"
    with pytest.raises(DestinationError, match="not saved"):
        store.credentials(profile, owner="bob")
    content = (tmp_path / "destinations.json").read_text(encoding="utf-8")
    assert "top-secret" not in content
    assert "password" not in json.loads(content)["destinations"][0]


def test_secure_os_keyring_persists_password_without_profile_file_secret(
    tmp_path, monkeypatch
):
    class SecureBackend:
        __module__ = "keyring.backends.macOS"
        priority = 1

    secrets = {}
    keyring = SimpleNamespace(
        get_keyring=lambda: SecureBackend(),
        set_password=lambda service, account, password: secrets.__setitem__(
            (service, account), password
        ),
        get_password=lambda service, account: secrets.get((service, account)),
        delete_password=lambda service, account: secrets.pop((service, account)),
    )
    monkeypatch.setitem(sys.modules, "keyring", keyring)
    path = tmp_path / "destinations.json"
    store = DestinationStore(path)
    profile = store.save(_profile().public_dict(), "keychain-secret")

    reloaded_store = DestinationStore(path)
    assert reloaded_store.credentials(profile) == "keychain-secret"
    assert reloaded_store.credential_storage(profile.id) == "OS credential store"
    assert "keychain-secret" not in path.read_text(encoding="utf-8")
    assert reloaded_store.remove(profile.id)
    assert not secrets


def test_insecure_keyring_backend_falls_back_to_session_only(tmp_path, monkeypatch):
    class InsecureBackend:
        priority = 1

    keyring = SimpleNamespace(
        get_keyring=lambda: InsecureBackend(),
        set_password=lambda *_args: pytest.fail("insecure backend must not be used"),
    )
    monkeypatch.setitem(sys.modules, "keyring", keyring)
    store = DestinationStore(tmp_path / "destinations.json")
    profile = store.save(_profile().public_dict(), "session-secret")

    assert store.credentials(profile) == "session-secret"
    assert store.credential_storage(profile.id) == "session-only"


def test_test_connection_checks_share_and_write_permissions():
    profile = _profile()
    client = _FakeSMBClient()
    backend = SMBBackend(profile, "music-user", "session-secret", client=client)
    result = backend.test_connection()
    assert result["ok"]
    assert not any(name.endswith(".tmp") for name in client.files)
    server, session = client.sessions[0]
    assert server == profile.host
    assert session["username"] == "HOME\\music-user"
    assert session["password"] == "session-secret"
    assert session["connection_timeout"] == profile.timeout


def test_unreachable_and_permission_errors_are_actionable_and_redacted():
    class Unreachable(_FakeSMBClient):
        def register_session(self, server, **kwargs):
            raise TimeoutError("secret detail")

    backend = SMBBackend(_profile(), "alice", "dont-log-this", client=Unreachable())
    with pytest.raises(DestinationError, match="timed out") as error:
        backend.test_connection()
    assert "dont-log-this" not in str(error.value)
    assert "secret detail" not in str(error.value)
    assert "secret detail" not in "".join(traceback.format_exception(error.value))

    client = _FakeSMBClient()
    client.deny_write = True
    backend = SMBBackend(_profile(), "alice", "password", client=client)
    with pytest.raises(DestinationError, match="access was denied") as error:
        backend.test_connection()
    assert "password" not in str(error.value)
    assert "sensitive server" not in str(error.value)


def test_upload_is_verified_and_renamed_only_after_transfer(tmp_path):
    client = _FakeSMBClient()
    backend = SMBBackend(_profile(subdirectory=""), "user", "pass", client=client)
    local_root = tmp_path / "stage"
    local_root.mkdir()
    (local_root / "album.flac").write_bytes(b"lossless audio bytes")

    delivered = backend.upload_tree(local_root)
    final = r"\\nas.example.net\Music\album.flac"
    assert delivered == [final]
    assert client.files[final] == b"lossless audio bytes"
    assert not any(".mornye-part" in name for name in client.files)


def test_existing_equal_file_is_deduplicated_and_collision_gets_new_name(tmp_path):
    client = _FakeSMBClient()
    root = r"\\nas.example.net\Music"
    client.files[root + r"\same.flac"] = b"same"
    client.files[root + r"\different.flac"] = b"older contents"
    backend = SMBBackend(_profile(subdirectory=""), "user", "pass", client=client)
    local_root = tmp_path / "stage"
    local_root.mkdir()
    (local_root / "same.flac").write_bytes(b"same")
    (local_root / "different.flac").write_bytes(b"new contents")

    delivered = backend.upload_tree(local_root)
    assert delivered == [
        root + r"\different (1).flac",
        root + r"\same.flac",
    ]
    assert client.files[root + r"\different.flac"] == b"older contents"


def test_hash_mismatch_cleans_remote_temporary_file(tmp_path):
    client = _FakeSMBClient()
    client.corrupt_reads = True
    backend = SMBBackend(_profile(subdirectory=""), "user", "pass", client=client)
    local_root = tmp_path / "stage"
    local_root.mkdir()
    (local_root / "track.flac").write_bytes(b"audio")

    with pytest.raises(DestinationError, match="verification failed"):
        backend.upload_tree(local_root)
    assert not client.files


@pytest.mark.parametrize("filename", ["C:outside.flac", "nested\\outside.flac"])
def test_smb_rejects_unsafe_relative_filename_components(filename):
    with pytest.raises(DestinationError, match="filename is unsafe"):
        _smb_relative_parts(filename)


def test_cancelled_transfer_cleans_temporary_file(tmp_path):
    client = _FakeSMBClient()
    backend = SMBBackend(_profile(subdirectory=""), "user", "pass", client=client)
    local_root = tmp_path / "stage"
    local_root.mkdir()
    (local_root / "track.flac").write_bytes(b"audio")
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(DestinationError, match="cancelled"):
        backend.upload_tree(local_root, cancel_event=cancelled)
    assert not client.files


def test_transient_failure_reconnects_and_retries(tmp_path):
    client = _FakeSMBClient()
    client.fail_next_write = True
    backend = SMBBackend(
        _profile(subdirectory=""),
        "user",
        "pass",
        client=client,
        sleep=lambda _seconds: None,
    )
    local_root = tmp_path / "stage"
    local_root.mkdir()
    (local_root / "track.flac").write_bytes(b"audio")

    result = backend.upload_tree(local_root)
    assert len(client.sessions) == 2
    assert result == [r"\\nas.example.net\Music\track.flac"]
    assert client.files[result[0]] == b"audio"


def test_initial_transient_connect_error_is_retried():
    class FlakyConnection(_FakeSMBClient):
        attempts = 0

        def register_session(self, server, **kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise ConnectionError("temporary disconnect")
            super().register_session(server, **kwargs)

    client = FlakyConnection()
    backend = SMBBackend(
        _profile(),
        "user",
        "pass",
        client=client,
        sleep=lambda _seconds: None,
    )

    assert backend.test_connection()["ok"]
    assert client.attempts == 2


def test_transient_verification_read_reconnects_and_retries(tmp_path):
    class InterruptedVerification(_FakeSMBClient):
        failed_read = False

        def open_file(self, path, mode):
            if mode == "rb" and not self.failed_read:
                self.failed_read = True
                raise ConnectionError("temporary disconnect")
            return super().open_file(path, mode)

    client = InterruptedVerification()
    backend = SMBBackend(
        _profile(subdirectory=""),
        "user",
        "pass",
        client=client,
        sleep=lambda _seconds: None,
    )
    local_root = tmp_path / "stage"
    local_root.mkdir()
    (local_root / "track.flac").write_bytes(b"audio")

    result = backend.upload_tree(local_root)

    assert len(client.sessions) == 2
    assert client.files[result[0]] == b"audio"
    assert not any(".mornye-part" in name for name in client.files)
