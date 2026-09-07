"""The provider list every "choose your sources" surface reads.

Settings in the GUI used to render a list compiled into app.js while the
interactive wizard read the installed extensions, so the two menus offered
different providers on the same machine. Both now go through
extensions/catalog.installed_download_services(); these are the tests that
keep them there.
"""

from __future__ import annotations

import pytest

from SpotiFLAC.app import SpotiFLAC_API
from SpotiFLAC.extensions.catalog import (
    canonical_service_name,
    installed_download_services,
    service_label,
)
from SpotiFLAC.webapp import ALLOWED_METHODS


class _Ext:
    def __init__(self, name: str, *, provider: bool = True) -> None:
        self.name = name
        self.is_download_provider = provider


class _Manager:
    def __init__(self, extensions) -> None:
        self._extensions = extensions

    def list_installed(self):
        return self._extensions


@pytest.mark.parametrize(
    ("extension", "service"),
    [
        ("tidal-web", "tidal"),
        ("tidal-py", "tidal"),
        ("ext:tidal-web", "tidal"),
        ("qobuz_py", "qobuz"),
        ("ytmusic-spotiflac", "youtube"),
        ("youtube-py", "youtube"),
        ("apple-music-py", "apple"),
        ("deezer", "deezer"),
        ("gdstudio-py", "gdstudio"),
        ("", None),
    ],
)
def test_extensions_collapse_onto_one_service_name(extension, service):
    assert canonical_service_name(extension) == service


def test_two_extensions_for_one_service_are_one_row():
    """The JS and the Python Tidal providers both mean "Tidal" to someone
    choosing a source."""
    services = installed_download_services(
        _Manager([_Ext("tidal-web"), _Ext("tidal-py"), _Ext("deezer")])
    )

    assert [s["id"] for s in services] == ["deezer", "tidal"]
    tidal = next(s for s in services if s["id"] == "tidal")
    assert tidal["extensions"] == ["tidal-py", "tidal-web"]
    assert tidal["label"] == "Tidal"


def test_extensions_that_do_not_download_are_not_sources():
    services = installed_download_services(
        _Manager([_Ext("tidal-web"), _Ext("some-lyrics-thing", provider=False)])
    )
    assert [s["id"] for s in services] == ["tidal"]


def test_a_third_party_provider_is_listed_too():
    """Nothing here is a fixed catalogue: an extension nobody hard-coded is
    still a source the user can pick."""
    services = installed_download_services(_Manager([_Ext("my-own-provider")]))
    assert services == [
        {
            "id": "my-own-provider",
            "label": "My Own Provider",
            "extensions": ["my-own-provider"],
        }
    ]


def test_a_broken_extension_directory_is_an_empty_list_not_a_crash():
    class _Angry:
        def list_installed(self):
            raise RuntimeError("no extensions directory")

    assert installed_download_services(_Angry()) == []


@pytest.mark.parametrize(
    ("service", "label"),
    [
        ("tidal", "Tidal"),
        ("qobuz", "Qobuz"),
        ("amazon", "Amazon Music"),
        ("youtube", "YouTube Music"),
        ("soundcloud", "SoundCloud"),
        ("gdstudio", "GDStudio"),
        ("some-new-thing", "Some New Thing"),
    ],
)
def test_labels_read_like_a_menu(service, label):
    assert service_label(service) == label


def test_every_frontend_reads_the_same_list(monkeypatch):
    """The point of the refactor: one machine, one answer, every menu.

    This is the class of test that protects against the real hazard of
    having several frontends — one of them quietly falling behind. It began
    as wizard-against-GUI; the wizard is gone and the TUI took its place, in
    the test below.
    """
    installed = [_Ext("tidal-web"), _Ext("tidal-py"), _Ext("gdstudio-py")]
    monkeypatch.setattr(
        "SpotiFLAC.extensions.catalog.ExtensionManager",
        lambda *a, **k: _Manager(installed),
    )
    from SpotiFLAC.extensions.catalog import installed_service_ids

    shared = installed_service_ids()
    gui = [s["id"] for s in SpotiFLAC_API().get_download_services()["services"]]

    assert shared == gui == ["gdstudio", "tidal"]


def test_the_tui_offers_exactly_the_installed_providers(monkeypatch):
    """The TUI's provider list is the same list, mounted as a widget.

    Read off the running screen rather than off the function it calls: what
    can go wrong here is the panel filtering or reordering the answer on its
    way into the SelectionList, and only the mounted widget shows that.
    """
    import asyncio

    installed = [_Ext("tidal-web"), _Ext("tidal-py"), _Ext("gdstudio-py")]
    monkeypatch.setattr(
        "SpotiFLAC.extensions.catalog.ExtensionManager",
        lambda *a, **k: _Manager(installed),
    )

    from SpotiFLAC.tui.app import SpotiFLACTui
    from SpotiFLAC.tui.config_state import ConfigState

    async def _offered():
        state = ConfigState(output_dir="/tmp/o", services=["tidal"])
        async with SpotiFLACTui(state).run_test() as pilot:
            from textual.widgets import SelectionList

            listing = pilot.app.query_one("#cfg-services", SelectionList)
            return [str(selection.value) for selection in listing.options]

    assert asyncio.run(_offered()) == ["gdstudio", "tidal"]


def test_the_method_is_reachable_from_the_frontend():
    assert "get_download_services" in ALLOWED_METHODS
    assert hasattr(SpotiFLAC_API, "get_download_services")
