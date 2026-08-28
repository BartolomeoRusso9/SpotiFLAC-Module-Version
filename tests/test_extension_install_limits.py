"""Where an extension archive is allowed to write, and how big it may get.

The install path already validated the *member* paths inside the ZIP
(absolute paths, `..` components, symlink entries). It did not validate
`manifest["name"]`, which is what decides the destination directory — and
pathlib does not normalise, so `ext_dir / "../../x"` really does escape.
The same value reached shutil.rmtree() via uninstall().
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from SpotiFLAC.extensions import manager as manager_module
from SpotiFLAC.extensions.manager import ExtensionManager, _safe_ext_target


def _archive(name: str, extra: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "manifest.json",
            json.dumps({"name": name, "version": "1.0.0", "runtime": "javascript"}),
        )
        zf.writestr("index.js", "module.exports = {};")
        for member, payload in (extra or {}).items():
            zf.writestr(member, payload)
    return buf.getvalue()


@pytest.fixture
def manager(tmp_path):
    return ExtensionManager(ext_dir=tmp_path / "ext", auto_install_downloads=False)


@pytest.fixture
def install(manager, tmp_path):
    """Installs an in-memory archive through the public install_from_file()."""

    def _install(raw: bytes):
        path = tmp_path / "ext.spotiflac-ext"
        path.write_bytes(raw)
        return manager.install_from_file(path)

    return _install


# ── _safe_ext_target ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "../evil",
        "../../evil",
        "a/b",
        "/absolute",
        "..",
        ".",
        "",
        "   ",
        ".hidden",
    ],
)
def test_unsafe_names_are_rejected(tmp_path, name) -> None:
    with pytest.raises(ValueError):
        _safe_ext_target(tmp_path, name)


@pytest.mark.parametrize("name", ["tidal", "tidal.web", "my_ext", "ext-2", "a1"])
def test_ordinary_names_are_accepted(tmp_path, name) -> None:
    target = _safe_ext_target(tmp_path, name)
    assert target.parent == tmp_path.resolve()
    assert target.name == name


# ── install ────────────────────────────────────────────────────────────────


def test_a_traversing_manifest_name_cannot_escape_the_extensions_dir(
    install, tmp_path
) -> None:
    outside = tmp_path / "evil"
    with pytest.raises(ValueError, match="Unsafe extension name"):
        install(_archive("../evil"))
    assert not outside.exists()


def test_an_ordinary_extension_still_installs(manager, install) -> None:
    installed = install(_archive("tidal.web"))
    assert installed.name == "tidal.web"
    assert (manager.ext_dir / "tidal.web" / "index.js").exists()


def test_uninstall_refuses_a_traversing_id(manager, tmp_path) -> None:
    victim = tmp_path / "important"
    victim.mkdir()
    (victim / "keep.txt").write_text("do not delete")

    assert manager.uninstall("../important") is False
    assert (victim / "keep.txt").exists()


# ── size limits ────────────────────────────────────────────────────────────


def test_an_archive_that_unpacks_too_large_is_refused_before_extracting(
    manager, install, monkeypatch
) -> None:
    """A highly compressible payload: small on the wire, enormous on disk.
    Rejected from the ZIP header, so nothing is written and rolled back.

    The limit is lowered rather than the payload raised: building a real
    500 MB buffer to prove a header check works costs half a gigabyte of RAM
    in every test run, on every machine, forever.
    """
    monkeypatch.setattr(manager_module, "MAX_EXT_UNPACKED_BYTES", 4096)
    bomb = _archive("bomb", {"payload.bin": b"\0" * 8192})

    with pytest.raises(ValueError, match="unpacks to more than"):
        install(bomb)

    assert not (manager.ext_dir / "bomb").exists()


def test_an_archive_within_the_limit_still_installs(install, monkeypatch) -> None:
    """Guards the other side of the check: the cap must not reject ordinary
    extensions.
    """
    monkeypatch.setattr(manager_module, "MAX_EXT_UNPACKED_BYTES", 4096)
    assert install(_archive("small", {"payload.bin": b"\0" * 512})).name == "small"
