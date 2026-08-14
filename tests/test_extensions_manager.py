import hashlib
import io
import json
import zipfile

import pytest

from SpotiFLAC.extensions.catalog import extension_id
from SpotiFLAC.extensions.manager import ExtensionManager
from SpotiFLAC.extensions.python_provider import PythonExtensionProvider


def package(name="demo", extra=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"name": name, "version": "1.0.0"}))
        archive.writestr("index.js", "registerExtension({initialize: () => true})")
        if extra:
            archive.writestr(extra, "unsafe")
    return stream.getvalue()


def test_installs_verified_package_and_preserves_settings(tmp_path):
    raw = package()
    manager = ExtensionManager(tmp_path, auto_install_downloads=False)
    installed = manager._install_from_bytes(raw, sha256=hashlib.sha256(raw).hexdigest())
    manager.save_settings(installed.name, {"token": "saved"})
    manager._install_from_bytes(package())
    assert manager.load_settings("demo") == {"token": "saved"}


def test_rejects_bad_checksum_and_archive_traversal(tmp_path):
    manager = ExtensionManager(tmp_path, auto_install_downloads=False)
    with pytest.raises(ValueError, match="checksum"):
        manager._install_from_bytes(package(), sha256="0" * 64)
    with pytest.raises(ValueError, match="unsafe path"):
        manager._install_from_bytes(package(extra="../escape"))


def test_resolves_legacy_alias_to_extension_id(tmp_path):
    manager = ExtensionManager(tmp_path, auto_install_downloads=False)
    assert extension_id("tidal", manager) == "tidal-web"
    manager._install_from_bytes(package("tidal"))
    assert extension_id("tidal", manager) == "tidal"


def test_loads_legacy_python_sflx_as_a_provider(tmp_path):
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        archive.writestr(
            "demo_native.py",
            "from SpotiFLAC.core.base import BaseProvider\n"
            "class DemoProvider(BaseProvider):\n"
            "    name = 'demo'\n"
            "    async def download_track_async(self, *args, **kwargs):\n        return None\n",
        )
    manager = ExtensionManager(tmp_path, auto_install_downloads=False)
    installed = manager._install_from_bytes(raw.getvalue())
    assert installed.runtime == "python"
    provider = PythonExtensionProvider("demo", ext_dir=str(tmp_path))
    assert provider.name == "demo"
