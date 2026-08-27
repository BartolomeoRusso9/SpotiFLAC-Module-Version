"""extensions/trust.py — optional Ed25519 signature verification for registry
entries, on top of the sha256 checksum ExtensionManager already enforces.

A checksum proves a package wasn't corrupted or tampered with *in transit*
from wherever the registry says to fetch it. It says nothing about *who*
put that package there in the first place — the registry.json itself could
be swapped for a malicious one, with a matching (new) checksum, and nothing
in the existing checksum flow would object. A signature closes that gap:
a registry maintainer signs each entry with their own Ed25519 private key
(offline, with their own tooling — see tools/registry_signing_cli.py for a
minimal keygen+sign helper), and you decide whose public key you're
willing to trust, once, up front.

Nothing here is bundled or automatic, same as every other trust decision in
this project (see registry_config.py, extensions/directories.py): no
trusted key ships with SpotiFLAC. Until you add one yourself, every
registry entry — signed or not — is exactly as trusted as it is today
(checksum-only, if the registry provides one at all). Adding a trusted key
only makes verification *possible*; it never makes an unsigned entry look
more trustworthy than it did before.

Signed payload convention: a registry entry is signed over
    f"{id}|{version}|{sha256}|{download_url}"
(UTF-8 encoded) — see canonical_message(). All four fields are already
part of the registry entry itself, so nothing extra needs to travel with
the signature besides the signature bytes.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

logger = logging.getLogger(__name__)

TRUSTED_KEYS_FILE = Path.home() / ".spotiflac" / "trusted_keys.json"


class TrustKeyError(ValueError):
    pass


@dataclass(frozen=True)
class TrustedKey:
    name: str
    public_key_b64: str

    def to_dict(self) -> dict:
        return {"name": self.name, "public_key_b64": self.public_key_b64}


def canonical_message(
    ext_id: str, version: str, sha256: str, download_url: str
) -> bytes:
    """The exact bytes a registry maintainer signs and this module verifies
    against — order and separator matter and must match exactly between
    signing and verifying.
    """
    return f"{ext_id}|{version}|{sha256}|{download_url}".encode()


def _load() -> list[TrustedKey]:
    try:
        if TRUSTED_KEYS_FILE.exists():
            data = json.loads(TRUSTED_KEYS_FILE.read_text(encoding="utf-8"))
            return [
                TrustedKey(k["name"], k["public_key_b64"]) for k in data.get("keys", [])
            ]
    except Exception as e:
        logger.warning("[Trust] Unable to read %s: %s", TRUSTED_KEYS_FILE, e)
    return []


def _save(keys: list[TrustedKey]) -> None:
    TRUSTED_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TRUSTED_KEYS_FILE.write_text(
        json.dumps({"keys": [k.to_dict() for k in keys]}, indent=2),
        encoding="utf-8",
    )


def _validate_public_key(public_key_b64: str) -> None:
    """Raises TrustKeyError if `public_key_b64` isn't a well-formed Ed25519
    public key, so a typo is caught at add-time, not at the next verify.
    """
    try:
        raw = base64.b64decode(public_key_b64, validate=True)
        Ed25519PublicKey.from_public_bytes(raw)
    except Exception as exc:
        msg = f"Not a valid base64-encoded Ed25519 public key: {exc}"
        raise TrustKeyError(msg) from exc


def list_trusted_keys() -> list[dict]:
    return [k.to_dict() for k in _load()]


def add_trusted_key(name: str, public_key_b64: str) -> list[dict]:
    name = (name or "").strip()
    public_key_b64 = (public_key_b64 or "").strip()
    if not name:
        raise TrustKeyError("Key name cannot be empty")
    if not public_key_b64:
        raise TrustKeyError("Public key cannot be empty")
    _validate_public_key(public_key_b64)

    keys = [k for k in _load() if k.name != name]
    keys.append(TrustedKey(name, public_key_b64))
    _save(keys)
    return list_trusted_keys()


def remove_trusted_key(name: str) -> bool:
    keys = _load()
    remaining = [k for k in keys if k.name != name]
    if len(remaining) == len(keys):
        return False
    _save(remaining)
    return True


def verify_registry_entry(
    ext_id: str,
    version: str,
    sha256: str,
    download_url: str,
    signature_b64: str | None,
) -> str | None:
    """Returns the name of the trusted key that verifies `signature_b64`
    over this entry's canonical message, or None if the entry is unsigned,
    the signature doesn't match any trusted key, or no keys are configured
    at all. Never raises — a malformed signature is just "doesn't verify".
    """
    if not signature_b64:
        return None
    trusted = _load()
    if not trusted:
        return None

    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except Exception:
        return None

    message = canonical_message(ext_id, version, sha256 or "", download_url)
    for key in trusted:
        try:
            raw = base64.b64decode(key.public_key_b64, validate=True)
            Ed25519PublicKey.from_public_bytes(raw).verify(signature, message)
            return key.name
        except InvalidSignature:
            continue
        except Exception as e:
            logger.debug("[Trust] Skipping malformed trusted key '%s': %s", key.name, e)
            continue
    return None


def trust_tier(
    ext_id: str,
    version: str,
    sha256: str | None,
    download_url: str,
    signature_b64: str | None,
) -> str:
    """One of "signed", "checksum-only", "unverified" — a coarse label the
    GUI can show directly as a badge, without every caller re-deriving the
    same three-way logic.
    """
    if signature_b64 and verify_registry_entry(
        ext_id, version, sha256 or "", download_url, signature_b64
    ):
        return "signed"
    if sha256:
        return "checksum-only"
    return "unverified"
