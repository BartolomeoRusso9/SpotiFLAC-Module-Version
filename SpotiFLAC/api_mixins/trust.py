"""api_mixins/trust.py — trusted signing-key management, GUI/web surface.

See extensions/trust.py for what a trusted key buys you (an Ed25519
signature check on top of the sha256 checksum ExtensionManager already
enforces) and why nothing is trusted by default. {"ok": True, ...} return
shape matches add_registry()/remove_registry() (adding/removing an entry
from a persisted list) rather than scan_local()'s {"status": ...}.
"""

from __future__ import annotations


class TrustMixin:
    def get_trusted_keys(self) -> list | dict:
        try:
            from ..extensions.trust import list_trusted_keys

            return list_trusted_keys()
        except Exception as e:
            return {"error": str(e)}

    def add_trusted_key(self, name: str, public_key_b64: str) -> dict:
        try:
            from ..extensions.trust import add_trusted_key

            return {"ok": True, "keys": add_trusted_key(name, public_key_b64)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def remove_trusted_key(self, name: str) -> dict:
        try:
            from ..extensions.trust import remove_trusted_key

            found = remove_trusted_key(name)
            return {"ok": found}
        except Exception as e:
            return {"ok": False, "error": str(e)}
