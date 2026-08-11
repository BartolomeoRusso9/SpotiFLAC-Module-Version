import asyncio
from types import SimpleNamespace

import pytest
from pydoll.exceptions import FailedToStartBrowser

from SpotiFLAC.core import solver
from SpotiFLAC.core.signed_session_desktop import (
    CommunitySessionExchange,
    CommunitySessionRecord,
    ensure_community_session,
)
from SpotiFLAC.core.signed_session_mobile import perform_signed_fetch


class DummyClient:
    def __init__(self) -> None:
        self.authenticated = False
        self.namespace = "dummy"
        self.calls = []

    def _load(self) -> None:
        return None

    async def authenticate_with_turnstile(self, **kwargs) -> None:
        raise RuntimeError("browser unavailable")

    async def authenticate_with_manual_grant(self, **kwargs) -> None:
        self.calls.append(kwargs)
        self.authenticated = True

    async def request(self, method, path, json_body=None, extra_headers=None):
        return SimpleNamespace(
            status_code=200,
            headers={},
            text="{}",
            url="https://example.test",
        )


def test_perform_signed_fetch_returns_browser_error_without_manual_grant_fallback() -> None:
    async def run_test() -> None:
        client = DummyClient()
        result = await perform_signed_fetch(client, "GET", "/x", None, None, timeout=42)
        assert result == {"error": "browser unavailable"}
        assert client.calls == []

    asyncio.run(run_test())


def test_wait_before_desktop_solver_start_uses_expected_delay(monkeypatch) -> None:
    calls = {}

    def fake_sleep(seconds):
        calls["seconds"] = seconds

    monkeypatch.setattr(
        "SpotiFLAC.core.signed_session_desktop.time.sleep",
        fake_sleep,
    )

    from SpotiFLAC.core.signed_session_desktop import (
        DESKTOP_VERIFICATION_SOLVER_STARTUP_DELAY_SECONDS,
        wait_before_desktop_solver_start,
    )

    wait_before_desktop_solver_start()

    assert calls.get("seconds") == DESKTOP_VERIFICATION_SOLVER_STARTUP_DELAY_SECONDS


def test_desktop_verify_queue_wait_exceeds_solver_callback_window() -> None:
    from SpotiFLAC.core.signed_session_desktop import COMMUNITY_VERIFY_TIMEOUT

    assert COMMUNITY_VERIFY_TIMEOUT >= 60


def test_ensure_community_session_retries_after_timeout(monkeypatch) -> None:
    record = CommunitySessionRecord(install_id="install-1")
    attempts = {"count": 0}

    def fake_run_community_verification(rec):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("Automated verification timed out (nessun grant ricevuto in tempo).")
        return "grant-ok"

    def fake_exchange_community_grant(rec, grant):
        return CommunitySessionExchange(
            session_id="sess-1",
            session_secret="secret-1",
            expires_at="2099-01-01T00:00:00Z",
        )

    monkeypatch.setattr(
        "SpotiFLAC.core.signed_session_desktop.community_session_valid",
        lambda _: False,
    )
    monkeypatch.setattr(
        "SpotiFLAC.core.signed_session_desktop.load_community_session",
        lambda: record,
    )
    monkeypatch.setattr(
        "SpotiFLAC.core.signed_session_desktop.save_community_session",
        lambda _record: None,
    )
    monkeypatch.setattr(
        "SpotiFLAC.core.signed_session_desktop.run_community_verification",
        fake_run_community_verification,
    )
    monkeypatch.setattr(
        "SpotiFLAC.core.signed_session_desktop.exchange_community_grant",
        fake_exchange_community_grant,
    )

    result = ensure_community_session()

    assert attempts["count"] == 2
    assert result.session_id == "sess-1"


def test_build_chromium_options_includes_container_sandbox_flags(monkeypatch) -> None:
    monkeypatch.setattr(solver, "_find_chrome", lambda: "/usr/bin/chromium")
    monkeypatch.setattr(solver, "_get_profile_dir", lambda: "/tmp/ts_profile")

    options = solver.build_chromium_options(hidden=True)

    assert "--no-sandbox" in options.arguments
    assert "--disable-setuid-sandbox" in options.arguments
    assert "--disable-dev-shm-usage" in options.arguments


def test_solver_wraps_browser_start_failure_with_clear_runtime_error(monkeypatch) -> None:
    class FakeBrowser:
        def __init__(self, options) -> None:
            self.options = options

        async def start(self):
            raise FailedToStartBrowser()

        async def stop(self):
            return None

    monkeypatch.setattr(solver, "Chrome", FakeBrowser)

    with pytest.raises(RuntimeError, match="Browser failed to start"):
        asyncio.run(solver._solve_impl("sitekey", "https://example.com", 1))
