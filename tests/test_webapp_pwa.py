"""Tests for the PWA installability additions (manifest.json, sw.js, and
the tags/registration script wired into index.html) — see sw.js's own
comment for the network-first design and why it's installability, not
offline-caching-of-live-data.
"""

from __future__ import annotations

import json

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
webapp = pytest.importorskip("SpotiFLAC.webapp")

TestClient = fastapi_testclient.TestClient


def test_manifest_is_served_and_valid() -> None:
    client = TestClient(webapp.create_app())
    resp = client.get("/manifest.json")
    assert resp.status_code == 200

    manifest = resp.json()
    assert manifest["name"] == "SpotiFLAC"
    assert manifest["display"] == "standalone"
    assert len(manifest["icons"]) >= 1
    for icon in manifest["icons"]:
        assert {"src", "sizes", "type"} <= icon.keys()


def test_manifest_icons_exist_as_real_files() -> None:

    manifest = json.loads((webapp.FRONTEND_DIR / "manifest.json").read_text())
    for icon in manifest["icons"]:
        icon_path = webapp.FRONTEND_DIR / icon["src"]
        assert icon_path.is_file(), f"missing icon file: {icon_path}"
        if icon["type"] == "image/png":
            # Confirms it's a real raster image, not an accidental 0-byte
            # or HTML-error-page file at that path.
            assert icon_path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_service_worker_is_served_with_javascript_content_type() -> None:
    client = TestClient(webapp.create_app())
    resp = client.get("/sw.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
    assert "registerExtension" not in resp.text  # sanity: not the wrong file
    assert "self.addEventListener" in resp.text


def test_service_worker_never_intercepts_api_or_ws_paths() -> None:
    sw_source = (webapp.FRONTEND_DIR / "sw.js").read_text()
    assert '"/api/"' in sw_source
    assert '"/ws"' in sw_source


def test_index_html_declares_the_manifest_and_theme_color() -> None:
    client = TestClient(webapp.create_app())
    html = client.get("/").text
    assert 'rel="manifest"' in html
    assert 'name="theme-color"' in html


def test_index_html_registers_the_service_worker_gated_on_web_mode() -> None:
    client = TestClient(webapp.create_app())
    html = client.get("/").text
    assert "__SPOTIFLAC_WEB_MODE__" in html  # injected by webapp.py's index()
    assert "navigator.serviceWorker.register" in html
    # The registration script must run after the web-mode flag is set, so
    # it actually sees `true` in web mode instead of reading `undefined`
    # from a stale evaluation order.
    flag_pos = html.index("__SPOTIFLAC_WEB_MODE__ = true")
    register_pos = html.index("navigator.serviceWorker.register")
    assert flag_pos < register_pos
