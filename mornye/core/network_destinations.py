"""Saved network destinations and safe SMB file delivery.

Destination profiles contain connection metadata only. Passwords are held in
process memory for the current session and are never written to disk.
"""

from __future__ import annotations

import builtins
import hashlib
import importlib
import ipaddress
import json
import logging
import ntpath
import os
import re
import stat
import threading
import time
import tempfile
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .atomic_io import write_private_json
from .paths import data_path

logger = logging.getLogger(__name__)

DESTINATIONS_FILE = data_path("network_destinations.json")
_CHUNK_SIZE = 1024 * 1024
_MAX_TRANSFER_ATTEMPTS = 3
_ACCESS_DENIED = {0xC0000022, 0xC000006D, 0xC000006E}
_KEYRING_SERVICE = "mornye-python-smb"
_SECURE_KEYRING_MODULES = (
    "keyring.backends.macOS",
    "keyring.backends.Windows",
    "keyring.backends.SecretService",
)


class DestinationError(RuntimeError):
    """A safe, actionable destination error that contains no credentials."""


def _smb_relative_parts(relative: str) -> tuple[str, ...]:
    components = PurePosixPath(relative).parts
    if not components or any(
        component in {"", ".", ".."}
        or any(character in component for character in "/\\:")
        or any(ord(character) < 32 for character in component)
        for component in components
    ):
        raise DestinationError("A staged filename is unsafe for SMB delivery.")
    return components


@dataclass(frozen=True)
class SMBProfile:
    id: str
    name: str
    host: str
    share: str
    subdirectory: str = ""
    username: str = ""
    domain: str = ""
    timeout: int = 15
    encrypt: bool = False

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_mounted_path(path: str, *, platform: str | None = None) -> str:
    """Normalize a local or OS-mounted share path without rewriting UNC paths."""
    value = str(path or "").strip()
    if not value:
        raise DestinationError("Choose a destination folder.")
    target_platform = platform or os.name
    if target_platform == "nt":
        normalized = ntpath.normpath(os.path.expandvars(value))
        if normalized.startswith("\\\\"):
            unc_parts = [part for part in normalized[2:].split("\\") if part]
            is_absolute = len(unc_parts) >= 2
        else:
            drive, tail = ntpath.splitdrive(normalized)
            is_absolute = (
                len(drive) == 2 and drive[1] == ":" and tail.startswith(("\\", "/"))
            )
        if not is_absolute:
            raise DestinationError(
                "Use an absolute drive path or UNC path such as \\\\server\\share."
            )
        return normalized
    return os.path.abspath(os.path.expanduser(value))


def create_staging_directory(owner: str = "") -> Path:
    owner_key = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:16]
    root = data_path("smb-staging", owner_key)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    return Path(tempfile.mkdtemp(prefix="delivery-", dir=root))


def retained_staging_directory(owner: str, staging_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", str(staging_id)):
        raise DestinationError("Invalid retained-transfer identifier.")
    owner_key = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:16]
    root = data_path("smb-staging", owner_key)
    stage = root / f"delivery-{staging_id}"
    if not stage.is_dir() or stage.is_symlink():
        raise DestinationError("That retained transfer could not be found.")
    return stage


def _validate_subdirectory(value: str) -> str:
    normalized = str(value or "").replace("\\", "/").strip("/")
    if not normalized:
        return ""
    if ":" in normalized or any(ord(character) < 32 for character in normalized):
        raise DestinationError("The SMB subdirectory must stay within its share.")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise DestinationError("The SMB subdirectory must stay within its share.")
    return "/".join(path.parts)


def validate_profile(data: dict[str, Any]) -> SMBProfile:
    name = str(data.get("name") or "").strip()
    host = str(data.get("host") or "").strip().strip("[]")
    share = str(data.get("share") or "").strip()
    username = str(data.get("username") or "").strip()
    domain = str(data.get("domain") or "").strip()
    if not name or len(name) > 80:
        raise DestinationError("Destination name must be 1–80 characters.")
    if not host or any(char in host for char in "/\\@"):
        raise DestinationError("Enter an SMB server hostname or IP address.")
    if ":" in host:
        try:
            ipaddress.IPv6Address(host)
        except ValueError as exc:
            raise DestinationError("Enter a hostname or a valid IP address.") from exc
    elif not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        try:
            ipaddress.ip_address(host)
        except ValueError as exc:
            raise DestinationError("Enter a hostname or a valid IP address.") from exc
    if (
        not share
        or share in {".", ".."}
        or any(c in share for c in "/\\:")
        or any(ord(character) < 32 for character in share)
    ):
        raise DestinationError("Enter one SMB share name, not a path.")
    if len(username) > 256 or len(domain) > 256:
        raise DestinationError(
            "SMB username and domain must be 256 characters or fewer."
        )
    try:
        timeout = int(data.get("timeout", 15))
    except (TypeError, ValueError) as exc:
        raise DestinationError(
            "Connection timeout must be a number of seconds."
        ) from exc
    if not 1 <= timeout <= 120:
        raise DestinationError("Connection timeout must be between 1 and 120 seconds.")
    profile_id = str(data.get("id") or uuid.uuid4().hex)
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", profile_id):
        raise DestinationError("Invalid destination identifier.")
    return SMBProfile(
        id=profile_id,
        name=name,
        host=host,
        share=share,
        subdirectory=_validate_subdirectory(str(data.get("subdirectory") or "")),
        username=username,
        domain=domain,
        timeout=timeout,
        encrypt=data.get("encrypt") is True,
    )


class DestinationStore:
    """Atomic profiles with secure-keyring or session-only password storage."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DESTINATIONS_FILE
        self._lock = threading.RLock()
        self._passwords: dict[tuple[str, str], str] = {}
        self._credential_storage: dict[tuple[str, str], str] = {}

    def list(self) -> builtins.list[dict[str, Any]]:
        return [profile.public_dict() for profile in self._load()]

    def get(self, profile_id: str) -> SMBProfile | None:
        profiles = self._load()
        return next(
            (
                profile
                for profile in profiles
                if profile.id == profile_id
                or profile.name.casefold() == str(profile_id).casefold()
            ),
            None,
        )

    def save(
        self,
        data: dict[str, Any],
        password: str | None = None,
        *,
        owner: str = "",
    ) -> SMBProfile:
        profile = validate_profile(data)
        with self._lock:
            profiles = [p for p in self._load() if p.id != profile.id]
            if any(p.name.casefold() == profile.name.casefold() for p in profiles):
                raise DestinationError("A destination with that name already exists.")
            profiles.append(profile)
            self._write(profiles)
            if password is not None:
                self._store_password(profile.id, str(password), owner=owner)
        return profile

    def remove(self, profile_id: str) -> bool:
        with self._lock:
            profiles = self._load()
            remaining = [p for p in profiles if p.id != profile_id]
            if len(remaining) == len(profiles):
                return False
            self._write(remaining)
            self._delete_keyring_password(profile_id)
            self._passwords = {
                key: value
                for key, value in self._passwords.items()
                if key[1] != profile_id
            }
            self._credential_storage = {
                key: value
                for key, value in self._credential_storage.items()
                if key[1] != profile_id
            }
            return True

    def set_password(self, profile_id: str, password: str, *, owner: str = "") -> None:
        if self.get(profile_id) is None:
            raise DestinationError("That SMB destination no longer exists.")
        self._passwords[(owner, profile_id)] = str(password)
        self._credential_storage[(owner, profile_id)] = "session-only"

    def credential_storage(self, profile_id: str, *, owner: str = "") -> str:
        key = (owner, profile_id)
        if key in self._passwords:
            return self._credential_storage.get(key, "session-only")
        if owner:
            return "not configured"
        password = self._read_keyring_password(profile_id)
        if password is None:
            return "not configured"
        self._passwords[key] = password
        self._credential_storage[key] = "OS credential store"
        return "OS credential store"

    def credentials(
        self,
        profile: SMBProfile,
        password: str | None = None,
        *,
        owner: str = "",
    ) -> str:
        if password is not None:
            self.set_password(profile.id, password, owner=owner)
            return str(password)
        if not profile.username:
            return ""
        key = (owner, profile.id)
        if key in self._passwords:
            return self._passwords[key]
        if not owner:
            stored_password = self._read_keyring_password(profile.id)
            if stored_password is not None:
                self._passwords[key] = stored_password
                self._credential_storage[key] = "OS credential store"
                return stored_password
        if key not in self._passwords:
            raise DestinationError(
                "Enter the SMB password for this session; passwords are not saved."
            )
        return self._passwords[key]

    def _store_password(self, profile_id: str, password: str, *, owner: str) -> None:
        key = (owner, profile_id)
        self._passwords[key] = password
        if owner:
            self._credential_storage[key] = "session-only"
            return
        keyring = _secure_keyring()
        if keyring is None:
            self._credential_storage[key] = "session-only"
            return
        try:
            keyring.set_password(
                _KEYRING_SERVICE,
                profile_id,
                password,
            )
        except Exception as exc:
            logger.warning(
                "Could not save the SMB password in the OS credential store (%s); "
                "it will be available for this session only.",
                type(exc).__name__,
            )
            self._credential_storage[key] = "session-only"
            return
        self._credential_storage[key] = "OS credential store"

    def _read_keyring_password(self, profile_id: str) -> str | None:
        keyring = _secure_keyring()
        if keyring is None:
            return None
        try:
            return keyring.get_password(_KEYRING_SERVICE, profile_id)
        except Exception as exc:
            logger.warning(
                "Could not read the SMB password from the OS credential store (%s).",
                type(exc).__name__,
            )
            return None

    def _delete_keyring_password(self, profile_id: str) -> None:
        keyring = _secure_keyring()
        if keyring is None:
            return
        try:
            keyring.delete_password(_KEYRING_SERVICE, profile_id)
        except Exception as exc:
            logger.warning(
                "Could not remove an SMB password from the OS credential store (%s).",
                type(exc).__name__,
            )

    def _load(self) -> builtins.list[SMBProfile]:
        try:
            content = json.loads(self.path.read_text(encoding="utf-8"))
            records = content.get("destinations", [])
            if not isinstance(records, builtins.list):
                raise ValueError("destinations must be a list")
            return [validate_profile(record) for record in records]
        except FileNotFoundError:
            return []
        except (OSError, ValueError, TypeError, DestinationError) as exc:
            logger.warning(
                "Unable to read saved network destinations (%s).", type(exc).__name__
            )
            raise DestinationError(
                "Saved network destinations could not be read."
            ) from exc

    def _write(self, profiles: builtins.list[SMBProfile]) -> None:
        write_private_json(
            self.path,
            {"destinations": [profile.public_dict() for profile in profiles]},
        )


class SMBBackend:
    """SMB2/SMB3 operations through smbprotocol's smbclient interface."""

    def __init__(
        self,
        profile: SMBProfile,
        username: str,
        password: str,
        *,
        client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.profile = profile
        self.username = username
        self.password = password
        self._client = client
        self._sleep = sleep

    def _smb(self) -> Any:
        if self._client is None:
            try:
                self._client = importlib.import_module("smbclient")
            except ImportError as exc:
                raise DestinationError(
                    "Native SMB support is optional. Install Mornye with "
                    "`pip install 'mornye-python[smb]'`."
                ) from exc
        return self._client

    @property
    def root(self) -> str:
        path = rf"\\{_unc_host(self.profile.host)}\{self.profile.share}"
        if self.profile.subdirectory:
            path = ntpath.join(path, *self.profile.subdirectory.split("/"))
        return path

    def connect(self) -> Any:
        client = self._smb()
        user = self.username
        if self.profile.domain and user:
            user = f"{self.profile.domain}\\{user}"
        for attempt in range(_MAX_TRANSFER_ATTEMPTS):
            try:
                client.register_session(
                    self.profile.host,
                    username=user or None,
                    password=self.password or None,
                    connection_timeout=self.profile.timeout,
                    encrypt=True if self.profile.encrypt else None,
                )
                return client
            except Exception as exc:
                if attempt + 1 < _MAX_TRANSFER_ATTEMPTS and _is_transient(exc):
                    self._sleep(0.25 * (attempt + 1))
                    continue
                raise self._safe_connection_error(exc) from None
        raise DestinationError("Could not connect to the configured SMB share.")

    def test_connection(self) -> dict[str, Any]:
        client = self.connect()
        try:
            share_root = rf"\\{_unc_host(self.profile.host)}\{self.profile.share}"
            client.stat(share_root)
            if self.profile.subdirectory:
                self._remote_call(lambda: client.makedirs(self.root, exist_ok=True))
            info = client.stat(self.root)
            if not _is_directory(info):
                raise DestinationError("The configured SMB share path is not a folder.")
        except DestinationError:
            raise
        except Exception as exc:
            raise self._safe_connection_error(exc) from None

        probe = ntpath.join(self.root, f".mornye-test-{uuid.uuid4().hex}.tmp")
        try:
            with client.open_file(probe, mode="xb") as remote:
                remote.write(b"mornye smb test")
                remote.flush()
            client.remove(probe)
        except Exception as exc:
            try:
                client.remove(probe)
            except Exception:
                pass
            raise self._safe_connection_error(exc, writing=True) from None
        return {"ok": True, "message": "SMB connection and write permissions verified."}

    def list_audio_files(self) -> list[dict[str, Any]]:
        """List supported audio files under this destination without writing."""
        from .tagger import SUPPORTED_SUFFIXES

        client = self.connect()
        pending = [self.root]
        found: list[dict[str, Any]] = []
        while pending:
            directory = pending.pop()
            entries = self._remote_call(
                lambda directory=directory: list(client.scandir(directory)),
                writing=False,
            )
            for entry in entries:
                name = str(entry.name)
                if (
                    name in {"", ".", ".."}
                    or ntpath.basename(name) != name
                    or any(separator in name for separator in ("/", "\\", ":"))
                ):
                    continue
                path = ntpath.join(directory, name)
                info = self._remote_call(
                    lambda entry=entry: entry.stat(follow_symlinks=False),
                    writing=False,
                )
                if _is_directory(info):
                    if not name.startswith("."):
                        pending.append(path)
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                suffix = ntpath.splitext(name)[1].lower()
                if suffix in SUPPORTED_SUFFIXES:
                    found.append(
                        {
                            "path": path,
                            "size": int(info.st_size),
                            "modified": float(info.st_mtime),
                        }
                    )
        return sorted(found, key=lambda entry: entry["path"].casefold())

    def open_audio_file(self, path: str) -> Any:
        """Open a listed SMB file for read-only metadata inspection."""
        root = ntpath.normcase(ntpath.normpath(self.root))
        target = ntpath.normcase(ntpath.normpath(str(path)))
        try:
            if ntpath.commonpath((root, target)) != root or target == root:
                raise DestinationError(
                    "The audio path is outside this SMB destination."
                )
        except ValueError as exc:
            raise DestinationError(
                "The audio path is outside this SMB destination."
            ) from exc
        client = self.connect()
        return self._remote_call(
            lambda: client.open_file(path, mode="rb"),
            writing=False,
        )

    def upload_tree(
        self,
        local_root: Path,
        *,
        progress: Callable[[str, int, int], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> list[str]:
        if not local_root.is_dir() or local_root.is_symlink():
            raise DestinationError("The local SMB staging directory is unavailable.")
        client = self.connect()
        try:
            self._remote_call(lambda: client.makedirs(self.root, exist_ok=True))
        except Exception as exc:
            raise self._safe_connection_error(exc, writing=True) from None

        candidates = sorted(local_root.rglob("*"))
        if any(path.is_symlink() for path in candidates):
            raise DestinationError(
                "Staged destination contains an unsafe symbolic link."
            )
        files = [path for path in candidates if path.is_file()]
        delivered: list[str] = []
        for local_path in files:
            if cancel_event is not None and cancel_event.is_set():
                raise DestinationError(
                    "SMB delivery was cancelled. Staged local files were retained."
                )
            relative = local_path.relative_to(local_root).as_posix()
            components = _smb_relative_parts(relative)
            remote_dir = (
                ntpath.join(self.root, *components[:-1])
                if len(components) > 1
                else self.root
            )
            try:
                self._remote_call(lambda: client.makedirs(remote_dir, exist_ok=True))
                remote_path = self._upload_one(
                    client,
                    local_path,
                    remote_dir,
                    cancel_event=cancel_event,
                    progress=(
                        (
                            lambda transferred, total: progress(
                                relative, transferred, total
                            )
                        )
                        if progress
                        else None
                    ),
                )
            except DestinationError:
                raise
            except Exception as exc:
                raise self._safe_connection_error(exc, writing=True) from None
            delivered.append(remote_path)
        return delivered

    def _remote_call(
        self,
        operation: Callable[[], Any],
        *,
        writing: bool = True,
        missing_ok: bool = False,
    ) -> Any:
        for attempt in range(_MAX_TRANSFER_ATTEMPTS):
            try:
                return operation()
            except Exception as exc:
                if missing_ok and _is_not_found(exc):
                    return None
                if attempt + 1 < _MAX_TRANSFER_ATTEMPTS and _is_transient(exc):
                    self._sleep(0.25 * (attempt + 1))
                    self.connect()
                    continue
                raise self._safe_connection_error(exc, writing=writing) from None
        raise DestinationError("SMB operation could not be completed.")

    def _upload_one(
        self,
        client: Any,
        local_path: Path,
        remote_dir: str,
        *,
        cancel_event: threading.Event | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> str:
        size, digest = _hash_local(local_path)
        remote_name = local_path.name
        for collision in range(1000):
            candidate_name = (
                remote_name
                if collision == 0
                else f"{local_path.stem} ({collision}){local_path.suffix}"
            )
            final_path = ntpath.join(remote_dir, candidate_name)
            remote_info = self._remote_call(
                lambda: client.stat(final_path),
                writing=False,
                missing_ok=True,
            )
            if remote_info is not None:
                if (
                    int(remote_info.st_size) == size
                    and self._remote_call(
                        lambda: _hash_remote(client, final_path),
                        writing=False,
                    )
                    == digest
                ):
                    return final_path
                continue

            for attempt in range(_MAX_TRANSFER_ATTEMPTS):
                temporary_path = ntpath.join(
                    remote_dir, f".{candidate_name}.{uuid.uuid4().hex}.mornye-part"
                )
                try:
                    _copy_verified(
                        client,
                        local_path,
                        temporary_path,
                        size,
                        digest,
                        cancel_event=cancel_event,
                        progress=progress,
                    )
                    client.rename(temporary_path, final_path)
                    return final_path
                except DestinationError:
                    _safe_remove(client, temporary_path)
                    raise
                except Exception as exc:
                    _safe_remove(client, temporary_path)
                    if _is_collision(exc):
                        break
                    if attempt + 1 < _MAX_TRANSFER_ATTEMPTS and _is_transient(exc):
                        self._sleep(0.25 * (attempt + 1))
                        self.connect()
                        continue
                    raise self._safe_connection_error(exc, writing=True) from None
        raise DestinationError("Could not find an unused filename on the SMB share.")

    def _safe_connection_error(
        self, exc: BaseException, *, writing: bool = False
    ) -> DestinationError:
        status = getattr(exc, "status", None)
        if isinstance(status, int) and status in _ACCESS_DENIED:
            return DestinationError(
                "SMB access was denied. Check the account, share permissions, "
                "and destination folder permissions."
            )
        name = type(exc).__name__.lower()
        if "accessdenied" in name or "permission" in name:
            return DestinationError(
                "SMB access was denied. Check the account, share permissions, "
                "and destination folder permissions."
            )
        if isinstance(exc, (TimeoutError, ConnectionError)) or "timeout" in name:
            return DestinationError(
                "Could not reach the SMB server before the connection timed out. "
                "Check the server address, port 445, and network route."
            )
        if writing:
            return DestinationError(
                "SMB destination could not be written. Check share and folder "
                "permissions, available space, and server connectivity."
            )
        return DestinationError(
            "Could not connect to the configured SMB share. Check the server, "
            "share name, credentials, and SMB2/SMB3 availability."
        )


def _is_directory(info: Any) -> bool:
    import stat

    return stat.S_ISDIR(info.st_mode)


def _unc_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def _is_not_found(exc: BaseException) -> bool:
    status = getattr(exc, "status", None)
    return status in (0xC0000034, 0xC000003A) or isinstance(
        exc, (FileNotFoundError, NotADirectoryError)
    )


def _is_collision(exc: BaseException) -> bool:
    status = getattr(exc, "status", None)
    return status == 0xC0000035 or isinstance(exc, FileExistsError)


def _is_transient(exc: BaseException) -> bool:
    status = getattr(exc, "status", None)
    return (
        isinstance(exc, (TimeoutError, ConnectionError, BrokenPipeError))
        or status in (0xC00000B5, 0xC000020C, 0xC000023C)
        or "timeout" in type(exc).__name__.lower()
    )


def _hash_local(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as source:
        while chunk := source.read(_CHUNK_SIZE):
            digest.update(chunk)
            total += len(chunk)
    return total, digest.hexdigest()


def _hash_remote(client: Any, path: str) -> str:
    digest = hashlib.sha256()
    with client.open_file(path, mode="rb") as remote:
        while chunk := remote.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_verified(
    client: Any,
    local_path: Path,
    temporary_path: str,
    size: int,
    digest: str,
    *,
    cancel_event: threading.Event | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> None:
    transferred = 0
    with (
        local_path.open("rb") as source,
        client.open_file(temporary_path, mode="xb") as remote,
    ):
        while chunk := source.read(_CHUNK_SIZE):
            if cancel_event is not None and cancel_event.is_set():
                raise DestinationError(
                    "SMB delivery was cancelled. Staged local files were retained."
                )
            remote.write(chunk)
            transferred += len(chunk)
            if progress:
                progress(transferred, size)
        remote.flush()
    if transferred != size:
        raise DestinationError(
            f"SMB transfer was incomplete ({transferred} of {size} bytes). "
            "The staged local file was retained for retry."
        )
    remote_size = client.stat(temporary_path).st_size
    remote_digest = _hash_remote(client, temporary_path)
    if int(remote_size) != size or remote_digest != digest:
        raise DestinationError(
            "SMB transfer verification failed (size or SHA-256 mismatch); "
            "the staged local file was retained."
        )


def _safe_remove(client: Any, path: str) -> None:
    try:
        client.remove(path)
    except FileNotFoundError:
        pass
    except Exception:
        logger.debug("Could not remove an SMB temporary file after failure.")


DESTINATIONS = DestinationStore()


def _secure_keyring() -> Any | None:
    try:
        keyring = importlib.import_module("keyring")
        backend = keyring.get_keyring()
    except ImportError:
        return None
    except Exception as exc:
        logger.warning(
            "OS credential store is unavailable (%s); SMB passwords will be "
            "session-only.",
            type(exc).__name__,
        )
        return None

    backend_module = type(backend).__module__
    if getattr(backend, "priority", 0) <= 0 or not any(
        backend_module == module or backend_module.startswith(f"{module}.")
        for module in _SECURE_KEYRING_MODULES
    ):
        return None
    return keyring
