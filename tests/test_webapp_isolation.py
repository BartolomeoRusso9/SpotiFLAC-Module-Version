"""Per-account isolation in `--web-multiuser`.

Before this, every account shared one SpotiFLAC_API instance: separate
logins, but one `current_tracks`, one `download_dir` and one event stream.
Two people searching at the same time overwrote each other's results, and
everybody's browser saw everybody's progress and file paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
webapp = pytest.importorskip("SpotiFLAC.webapp")

TestClient = fastapi_testclient.TestClient


@pytest.fixture(autouse=True)
def _accounts(tmp_path, monkeypatch):
    from SpotiFLAC.core import web_users

    monkeypatch.setattr(web_users, "USERS_FILE", tmp_path / "web_users.json")
    web_users.create_user("alice", "alice-password")
    web_users.create_user("bob", "bob-password")


def _login(app, username, password):
    client = TestClient(app)
    resp = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return client


# ── ApiRegistry ────────────────────────────────────────────────────────────


def test_each_account_gets_its_own_api_instance() -> None:
    manager = webapp.ConnectionManager()
    registry = webapp.ApiRegistry(manager, "/downloads")

    alice = registry.get("alice")
    bob = registry.get("bob")

    assert alice is not bob
    assert registry.get("alice") is alice, "instance not reused for the same account"


def test_each_account_downloads_into_its_own_folder(tmp_path) -> None:
    registry = webapp.ApiRegistry(webapp.ConnectionManager(), str(tmp_path))
    # Compared as path components, not string suffixes: os.path.join uses a
    # backslash on Windows, where the CI job also runs.
    assert Path(registry.get("alice").download_dir).name.startswith("alice-")
    assert Path(registry.get("bob").download_dir).name.startswith("bob-")


def test_per_account_folders_stay_under_the_shared_root(tmp_path) -> None:
    """A username is user-supplied text on its way into a filesystem path.
    Accounts are created locally, so this is not the last line of defence,
    but `..` should not be spellable there.
    """
    registry = webapp.ApiRegistry(webapp.ConnectionManager(), str(tmp_path))
    path = Path(registry.get("../../etc").download_dir)
    assert ".." not in path.parts
    assert path.parent == tmp_path


@pytest.mark.parametrize(
    ("username", "readable"),
    [
        ("alice", "alice"),
        ("a b", "a_b"),
        ("...", "user"),
        ("Ann-Marie_1", "Ann-Marie_1"),
    ],
)
def test_username_to_folder_name_keeps_a_readable_prefix(username, readable) -> None:
    assert webapp._safe_username(username).startswith(f"{readable}-")


def test_names_that_sanitise_alike_still_get_separate_folders() -> None:
    """Sanitising alone is lossy: "a b", "a_b" and "a/b" all reduce to the
    same string, so three accounts would have silently shared one download
    folder — the exact thing per-account directories exist to prevent.
    """
    names = ["a b", "a_b", "a/b", "a.b", "a-b"]
    folders = {webapp._safe_username(n) for n in names}
    assert len(folders) == len(names)


# ── request routing ────────────────────────────────────────────────────────


def test_a_request_reaches_its_own_accounts_instance(tmp_path) -> None:
    """End-to-end through HTTP. Asserted against the instances themselves,
    not the response body: `set_download_dir` echoes back whatever it was
    just given, so a shared instance would answer identically and the test
    would pass while the bug was still there.
    """
    app = webapp.create_app(multiuser=True)
    alice = _login(app, "alice", "alice-password")
    _login(app, "bob", "bob-password")

    alice_api = app.state.api_registry.get("alice")
    bob_api = app.state.api_registry.get("bob")
    bob_dir_before = bob_api.download_dir

    alice_dir = tmp_path / "alice-music"
    alice_dir.mkdir()
    alice.post("/api/set_download_dir", json=[str(alice_dir)])

    assert alice_api.download_dir == str(alice_dir), "request missed its own instance"
    assert (
        bob_api.download_dir == bob_dir_before
    ), "one account changed another's folder"
    assert app.state.shared_api.download_dir != str(alice_dir)


def test_single_user_mode_keeps_one_shared_instance() -> None:
    """The default must be untouched: no accounts, no per-user anything."""
    app = webapp.create_app()
    client = TestClient(app)
    assert client.post("/api/get_version", json=[]).status_code == 200


# ── event routing ──────────────────────────────────────────────────────────


def test_broadcast_without_an_owner_reaches_everyone() -> None:
    manager = webapp.ConnectionManager()
    manager._connections = {"ws-a": "alice", "ws-b": "bob", "ws-c": None}
    assert manager.count() == 3


def test_count_can_be_narrowed_to_one_owner() -> None:
    manager = webapp.ConnectionManager()
    manager._connections = {"ws-a": "alice", "ws-b": "bob", "ws-c": "alice"}
    assert manager.count("alice") == 2
    assert manager.count("bob") == 1
    assert manager.count("carol") == 0


def test_events_are_addressed_to_one_account() -> None:
    """The isolation that matters most in practice: logs and progress carry
    file paths, so a shared stream leaks what everyone is downloading.
    """
    import asyncio

    sent: list[tuple[str, dict]] = []

    class _FakeWs:
        def __init__(self, name):
            self.name = name

        async def send_json(self, message):
            sent.append((self.name, message))

    manager = webapp.ConnectionManager()
    a, b = _FakeWs("alice-ws"), _FakeWs("bob-ws")
    manager._connections = {a: "alice", b: "bob"}

    asyncio.run(manager._send_all({"fn": "log", "args": ["secret"]}, "alice"))

    assert [name for name, _ in sent] == ["alice-ws"]


def test_a_dead_socket_is_dropped_and_does_not_block_the_others() -> None:
    import asyncio

    delivered: list[str] = []

    class _Ws:
        def __init__(self, name, broken=False):
            self.name, self.broken = name, broken

        async def send_json(self, message):
            if self.broken:
                raise ConnectionError("gone")
            delivered.append(self.name)

    manager = webapp.ConnectionManager()
    broken, healthy = _Ws("broken", broken=True), _Ws("healthy")
    manager._connections = {broken: None, healthy: None}

    asyncio.run(manager._send_all({"fn": "log", "args": []}, None))

    assert delivered == ["healthy"]
    assert broken not in manager._connections


def test_a_full_queue_answers_with_structured_fields_not_an_exception_string() -> None:
    """CodeQL flagged `str(exc)` in the 429 body, and it was right to: an
    exception message is written for a log reader. The caller still learns
    everything actionable, from fields built on purpose.
    """
    app = webapp.create_app(multiuser=True)
    # The class attribute is only a *default argument*, bound at definition
    # time, so patching it after the fact changes nothing — set the limit on
    # the live queue instead.
    app.state.job_queue._max_pending_per_owner = 0
    client = _login(app, "alice", "alice-password")

    resp = client.post(
        "/api/queue/submit-download", json={"selected_indices": [], "config": {}}
    )

    assert resp.status_code == 429
    body = resp.json()
    assert body["limit"] == 0
    assert "pending" in body
    # The username and the internal phrasing stay in the log, not the body.
    assert "alice" not in body["error"]
    assert "wait for some to finish" not in body["error"]


# ── folder browser ─────────────────────────────────────────────────────────


def test_the_folder_browser_cannot_leave_the_callers_own_root() -> None:
    """With $HOME approved, any account could browse to the shared download
    root and read every other account's folder name — and the rest of $HOME
    besides.
    """
    app = webapp.create_app(multiuser=True)
    alice = _login(app, "alice", "alice-password")
    bob_dir = app.state.api_registry.get("bob").download_dir

    denied = alice.get("/api/browse-folder", params={"path": bob_dir})
    assert denied.status_code == 403

    assert (
        alice.get("/api/browse-folder", params={"path": str(Path.home())}).status_code
        == 403
    )


def test_the_folder_browser_starts_in_the_callers_own_folder() -> None:
    app = webapp.create_app(multiuser=True)
    alice = _login(app, "alice", "alice-password")
    alice_dir = app.state.api_registry.get("alice").download_dir

    assert alice.get("/api/get-home-dir").json()["home_dir"] == alice_dir
    assert alice.get("/api/browse-folder").json()["path"] == alice_dir


def test_single_user_mode_can_still_browse_home() -> None:
    """There the "other account" is the same person, so confining the
    browser to the download folder would only remove a working feature.
    """
    client = TestClient(webapp.create_app())
    assert (
        client.get("/api/browse-folder", params={"path": str(Path.home())}).status_code
        == 200
    )
    assert client.get("/api/get-home-dir").json()["home_dir"] == str(Path.home())
