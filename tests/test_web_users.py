"""Tests for core/web_users.py (--web-multiuser accounts + sessions)."""

from __future__ import annotations

import time

import pytest

from SpotiFLAC.core import web_users


@pytest.fixture(autouse=True)
def _isolated_users_file(tmp_path, monkeypatch):
    monkeypatch.setattr(web_users, "USERS_FILE", tmp_path / "web_users.json")


def test_no_users_by_default():
    assert web_users.has_any_users() is False
    assert web_users.list_usernames() == []


def test_create_and_verify_user():
    web_users.create_user("alice", "correct horse battery staple")
    assert web_users.has_any_users() is True
    assert web_users.list_usernames() == ["alice"]

    assert web_users.verify_password("alice", "correct horse battery staple") is True
    assert web_users.verify_password("alice", "wrong password") is False
    assert web_users.verify_password("nobody", "anything") is False


def test_passwords_are_never_stored_in_plaintext(tmp_path):
    web_users.create_user("alice", "hunter2")
    raw = (tmp_path / "web_users.json").read_text()
    assert "hunter2" not in raw


def test_create_user_rejects_duplicate_username():
    web_users.create_user("alice", "pw1")
    with pytest.raises(web_users.WebUserError):
        web_users.create_user("alice", "pw2")


def test_create_user_rejects_empty_username_or_password():
    with pytest.raises(web_users.WebUserError):
        web_users.create_user("", "pw")
    with pytest.raises(web_users.WebUserError):
        web_users.create_user("alice", "")


def test_delete_user():
    web_users.create_user("alice", "pw")
    assert web_users.delete_user("alice") is True
    assert web_users.list_usernames() == []
    assert web_users.delete_user("alice") is False


def test_change_password():
    web_users.create_user("alice", "old-password")
    assert web_users.change_password("alice", "new-password") is True
    assert web_users.verify_password("alice", "old-password") is False
    assert web_users.verify_password("alice", "new-password") is True


def test_change_password_for_missing_user_returns_false():
    assert web_users.change_password("nobody", "pw") is False


def test_two_users_with_the_same_password_get_different_hashes(tmp_path):
    web_users.create_user("alice", "same-password")
    web_users.create_user("bob", "same-password")
    users = web_users._load()
    alice = next(u for u in users if u.username == "alice")
    bob = next(u for u in users if u.username == "bob")
    assert alice.salt != bob.salt
    assert alice.password_hash != bob.password_hash


def test_session_store_create_and_lookup():
    store = web_users.SessionStore()
    token = store.create("alice")
    assert store.username_for(token) == "alice"
    assert store.username_for("not-a-real-token") is None
    assert store.username_for(None) is None


def test_session_store_revoke():
    store = web_users.SessionStore()
    token = store.create("alice")
    store.revoke(token)
    assert store.username_for(token) is None
    store.revoke(None)  # must not raise


def test_session_store_expires_old_tokens():
    store = web_users.SessionStore(ttl_s=0.05)
    token = store.create("alice")
    assert store.username_for(token) == "alice"
    time.sleep(0.1)
    assert store.username_for(token) is None


def test_session_store_instances_are_independent():
    store1 = web_users.SessionStore()
    store2 = web_users.SessionStore()
    token = store1.create("alice")
    assert store2.username_for(token) is None
