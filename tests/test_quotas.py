"""Tests for roles and per-account quotas (core/web_users.py)."""

from __future__ import annotations

import time

import pytest

from SpotiFLAC.core import download_log, web_users
from SpotiFLAC.core.web_users import (
    QuotaExceededError,
    WebUserError,
    change_password,
    check_quota,
    create_user,
    get_user,
    is_admin,
    list_users,
    quota_usage,
    set_quota,
    set_role,
    verify_password,
)


@pytest.fixture(autouse=True)
def _isolated_users(tmp_path, monkeypatch):
    monkeypatch.setattr(web_users, "USERS_FILE", tmp_path / "web_users.json")


def _download(owner: str, size: int = 1024, *, age_s: float = 0.0) -> None:
    download_log.record(owner=owner, title="T", file_path="", size_bytes=size)
    if age_s:
        # Backdate the row so the rolling window can be exercised without
        # a test that has to wait a day.
        from SpotiFLAC.core import db

        with db.transaction() as conn:
            conn.execute(
                "UPDATE downloads SET downloaded_at = ? WHERE id = "
                "(SELECT MAX(id) FROM downloads)",
                (time.time() - age_s,),
            )


# ── Roles ─────────────────────────────────────────────────────────────────


def test_accounts_default_to_the_user_role():
    create_user("alice", "pw")
    assert get_user("alice").role == "user"
    assert is_admin("alice") is False


def test_an_admin_can_be_created_and_promoted():
    create_user("root", "pw", role="admin")
    create_user("bob", "pw")

    assert is_admin("root") is True
    assert set_role("bob", "admin") is True
    assert is_admin("bob") is True


def test_unknown_roles_are_refused():
    create_user("alice", "pw")
    with pytest.raises(WebUserError):
        create_user("carol", "pw", role="superuser")
    with pytest.raises(WebUserError):
        set_role("alice", "wizard")


def test_the_last_admin_cannot_be_demoted():
    create_user("root", "pw", role="admin")
    create_user("bob", "pw")

    with pytest.raises(WebUserError, match="only admin"):
        set_role("root", "user")

    # With a second admin in place it is allowed.
    set_role("bob", "admin")
    assert set_role("root", "user") is True


def test_is_admin_is_false_for_unknown_and_anonymous():
    assert is_admin(None) is False
    assert is_admin("") is False
    assert is_admin("nobody") is False


def test_listing_users_never_exposes_credential_material():
    create_user("alice", "pw", role="admin", daily_track_quota=5)
    rows = list_users()

    assert rows[0]["username"] == "alice"
    assert rows[0]["role"] == "admin"
    assert rows[0]["daily_track_quota"] == 5
    assert "password_hash" not in rows[0]
    assert "salt" not in rows[0]


def test_changing_a_password_keeps_role_and_quotas():
    """A rebuilt record used to silently demote an admin."""
    create_user(
        "root", "old-pw", role="admin", daily_track_quota=7, daily_byte_quota=1234
    )

    assert change_password("root", "new-pw") is True

    user = get_user("root")
    assert user.role == "admin"
    assert user.daily_track_quota == 7
    assert user.daily_byte_quota == 1234
    assert verify_password("root", "new-pw") is True
    assert verify_password("root", "old-pw") is False


# ── Quotas ────────────────────────────────────────────────────────────────


def test_no_quota_means_unlimited():
    create_user("alice", "pw")
    for _ in range(20):
        _download("alice")
    check_quota("alice")  # must not raise


def test_an_unknown_account_is_not_refused():
    """Enforcement is not the place to decide who may log in."""
    check_quota("ghost")


def test_track_quota_refuses_once_reached():
    create_user("alice", "pw", daily_track_quota=3)

    for _ in range(2):
        _download("alice")
    check_quota("alice")  # 2 of 3 — still allowed

    _download("alice")
    with pytest.raises(QuotaExceededError) as excinfo:
        check_quota("alice")

    assert excinfo.value.used == 3
    assert excinfo.value.limit == 3
    assert excinfo.value.unit == "tracks"


def test_byte_quota_refuses_once_reached():
    create_user("alice", "pw", daily_byte_quota=2048)
    _download("alice", size=2048)

    with pytest.raises(QuotaExceededError) as excinfo:
        check_quota("alice")
    assert excinfo.value.unit == "bytes"


def test_quotas_are_per_account():
    create_user("alice", "pw", daily_track_quota=1)
    create_user("bob", "pw", daily_track_quota=1)
    _download("alice")

    with pytest.raises(QuotaExceededError):
        check_quota("alice")
    check_quota("bob")  # bob is unaffected


def test_the_window_rolls_so_old_downloads_stop_counting():
    create_user("alice", "pw", daily_track_quota=2)
    _download("alice", age_s=web_users.QUOTA_WINDOW_S + 60)
    _download("alice")

    usage = quota_usage("alice")
    assert usage["tracks_used"] == 1  # the backdated one has aged out
    check_quota("alice")


def test_failed_downloads_do_not_consume_quota():
    create_user("alice", "pw", daily_track_quota=1)
    download_log.record(owner="alice", success=False)

    check_quota("alice")
    assert quota_usage("alice")["tracks_used"] == 0


def test_set_quota_leaves_the_limit_it_is_not_given_alone():
    create_user("alice", "pw", daily_track_quota=5, daily_byte_quota=99)

    set_quota("alice", daily_track_quota=10)
    user = get_user("alice")
    assert user.daily_track_quota == 10
    assert user.daily_byte_quota == 99

    # Zero is "unlimited", and must be distinguishable from "unchanged".
    set_quota("alice", daily_byte_quota=0)
    assert get_user("alice").daily_byte_quota == 0


def test_set_quota_reports_an_unknown_user():
    assert set_quota("nobody", daily_track_quota=1) is False


def test_usage_reports_both_limits_and_the_window():
    create_user("alice", "pw", daily_track_quota=4, daily_byte_quota=8192)
    _download("alice", size=100)

    usage = quota_usage("alice")
    assert usage == {
        "username": "alice",
        "tracks_used": 1,
        "tracks_limit": 4,
        "bytes_used": 100,
        "bytes_limit": 8192,
        "window_hours": 24.0,
    }


def test_legacy_files_without_roles_still_load(tmp_path, monkeypatch):
    """An upgraded instance must behave exactly as it did before."""
    legacy = tmp_path / "web_users.json"
    legacy.write_text(
        '{"users": [{"username": "old", "password_hash": "h", "salt": "00", '
        '"created_at": 1}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(web_users, "USERS_FILE", legacy)

    user = get_user("old")
    assert user.role == "user"
    assert user.daily_track_quota == 0
    check_quota("old")
