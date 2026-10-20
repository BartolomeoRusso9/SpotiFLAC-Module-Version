"""api_mixins/signed_sessions.py — which signed sessions are live, and until when.

A thin wrapper over `core/signed_session_status.py`, which is also what the
terminal UI and `--signed-sessions` call, so the three views cannot disagree.

Reading is harmless: no row carries a secret. Clearing and pruning edit
instance-wide state every account's downloads depend on, so in multi-user
mode (`owner` set) they are refused to anyone who is not an admin.
"""

from __future__ import annotations


class SignedSessionsMixin:
    def _may_edit_signed_sessions(self) -> bool:
        owner = getattr(self, "owner", "") or ""
        if not owner:
            return True
        try:
            from ..core.web_users import is_admin

            return is_admin(owner)
        except Exception:
            return False

    def get_signed_sessions(self) -> dict:
        """Shape: {"sessions": [row, ...]} — see `list_signed_sessions`."""
        try:
            from ..core.signed_session_status import list_signed_sessions

            return {"sessions": list_signed_sessions()}
        except Exception as e:
            return {"sessions": [], "error": str(e)}

    def clear_signed_session(self, key: str) -> dict:
        if not self._may_edit_signed_sessions():
            return {"ok": False, "error": "Only an admin can clear signed sessions."}
        try:
            from ..core.signed_session_status import clear_signed_session

            if not clear_signed_session(str(key or "")):
                return {"ok": False, "error": "Unknown session."}
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def prune_signed_sessions(self) -> dict:
        if not self._may_edit_signed_sessions():
            return {"ok": False, "error": "Only an admin can remove signed sessions."}
        try:
            from ..core.signed_session_status import prune_orphaned_sessions

            return {"ok": True, "removed": prune_orphaned_sessions()}
        except Exception as e:
            return {"ok": False, "error": str(e)}
