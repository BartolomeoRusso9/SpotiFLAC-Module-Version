from __future__ import annotations

from mornye.core import solver


def test_captured_grant_is_success_without_turnstile_token(monkeypatch) -> None:
    """Verify a captured callback grant succeeds even without a Turnstile token."""

    async def captured_grant(*args, **kwargs):
        """Return a captured network grant without a challenge token."""
        return (None, "grant-from-network")

    monkeypatch.setattr(solver, "_ensure_xvfb", lambda: None)
    monkeypatch.setattr(solver, "_solve_impl", captured_grant)

    token, grant = solver.solve_with_callback("sitekey", "https://challenge.test")

    assert token is None
    assert grant == "grant-from-network"
