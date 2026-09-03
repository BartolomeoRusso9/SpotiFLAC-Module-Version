"""End-to-end sanity checks for the new Discovery/Dedup/Trust frontend
markup and its round-trip through webapp.py's dynamic dispatcher — this is
what actually proves web-shim.js's positional-array calling convention
still lines up with each mixin method's signature, beyond the static
name-parity check in test_web_shim_methods_in_sync.py.
"""

from __future__ import annotations

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
webapp = pytest.importorskip("SpotiFLAC.webapp")

TestClient = fastapi_testclient.TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from SpotiFLAC.extensions import directories, trust

    monkeypatch.setattr(
        directories, "CONFIG_FILE", tmp_path / "directory_settings.json"
    )
    monkeypatch.delenv(directories.DIRECTORY_ENV_KEY, raising=False)
    monkeypatch.setattr(directories, "ENV_FILES_TO_CHECK", ())
    monkeypatch.setattr(trust, "TRUSTED_KEYS_FILE", tmp_path / "trusted_keys.json")
    return TestClient(webapp.create_app())


def test_index_page_contains_the_new_sections(client) -> None:
    html = client.get("/").text
    assert 'id="directory-list"' in html
    assert 'id="trust-key-list"' in html
    assert 'id="dedup-results-wrap"' in html
    assert 'id="login-modal"' in html
    assert 'id="account-signout-row"' in html


def test_the_status_line_has_somewhere_to_land(client) -> None:
    """setStatus() writes to #status-text and spins #spinner, and for a long
    while neither element existed — so every set_progress() the backend
    pushed (reading a CSV, matching its rows, fetching metadata, downloading)
    went nowhere, and minutes of work looked like a frozen window.
    """
    html = client.get("/").text
    assert 'id="status-bar"' in html
    assert 'id="status-text"' in html
    assert 'id="spinner"' in html

    app_js = client.get("/app.js").text
    for element_id in ("status-bar", "status-text", "spinner"):
        assert f"$('{element_id}')" in app_js


def test_the_parallel_downloads_setting_is_wired_end_to_end(client) -> None:
    """The GUI had no equivalent of --max-concurrent, so every download ran
    at client.SpotiFLAC's default of 2. The control is only worth having if
    buildConfig() reads it and load_settings() puts it back.
    """
    html = client.get("/").text
    assert 'id="config-concurrent"' in html

    app_js = client.get("/app.js").text
    assert "$('config-concurrent')" in app_js
    assert "max_concurrent_downloads" in app_js


def test_the_download_finished_event_takes_the_batch_it_closed(client) -> None:
    app_js = client.get("/app.js").text
    assert "window.app_download_finished = (success = true, indices = null)" in app_js


def test_the_download_dock_lives_outside_the_views(client) -> None:
    """It used to sit inside #view-home, which cost it two things: .view is
    display:none unless active, so the dock and its drawer vanished the
    moment the user opened another view mid-download; and #content is a
    stacking context (z-index: var(--z-content)), so --z-dock could not lift
    it over the sidebar rail — the rail painted across its left edge, and the
    drawer's backdrop stopped where the rail began.
    """
    html = client.get("/").text
    last_view = html.index('id="view-settings"')
    assert html.index('id="queue-dock"') > last_view
    assert html.index('id="queue-drawer"') > last_view

    css = client.get("/styles.css").text
    # And it is placed beside the rail rather than under it.
    assert "left: calc(var(--rail-w) + 20px)" in css


def test_discovery_directory_add_list_remove_round_trip_over_http(client) -> None:
    added = client.post(
        "/api/add_registry_directory", json=["https://example.com/dir.json"]
    ).json()["result"]
    assert added["ok"] is True

    listed = client.post("/api/get_registry_directories", json=[]).json()["result"]
    assert len(listed) == 1
    assert listed[0]["url"] == "https://example.com/dir.json"

    removed = client.post(
        "/api/remove_registry_directory", json=["https://example.com/dir.json"]
    ).json()["result"]
    assert removed["ok"] is True


def test_trusted_keys_are_readable_over_http(client) -> None:
    resp = client.post("/api/get_trusted_keys", json=[])
    assert resp.status_code == 200
    assert isinstance(resp.json()["result"], list)


def test_writing_the_trust_store_is_not_reachable_over_http(client) -> None:
    """Adding a trusted key is what makes an extension signature verify, so
    it must not be reachable from the HTTP API — otherwise anyone who can
    reach the port installs their own key and signs their own extensions.
    """
    for method, args in (
        ("add_trusted_key", ["alice", "AAAA"]),
        ("remove_trusted_key", ["alice"]),
    ):
        resp = client.post(f"/api/{method}", json=args)
        assert resp.status_code == 404, method
        assert "result" not in resp.json()


def test_search_code_is_not_reachable_over_http(client) -> None:
    """It returns matching *lines* from a caller-supplied path — an arbitrary
    file read if exposed. The UI never used it.
    """
    resp = client.post("/api/search_code", json=["password", "/"])
    assert resp.status_code == 404
    assert "result" not in resp.json()


def test_dedup_status_reachable_over_http(client) -> None:
    resp = client.post("/api/get_dedup_status", json=[])
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert "available" in result


def test_the_library_dedup_panel_is_in_the_page(client) -> None:
    html = client.get("/").text
    for element_id in (
        "libdedup-match",
        "libdedup-tolerance",
        "libdedup-verify",
        "libdedup-db",
        "libdedup-groups",
        "libdedup-actions",
        "libdedup-undo",
    ):
        assert f'id="{element_id}"' in html
    assert 'onclick="startLibraryDedupScan()"' in html
    assert "onclick=\"resolveLibraryDuplicates('trash')\"" in html
    assert "onclick=\"resolveLibraryDuplicates('delete')\"" in html


def test_library_dedup_calls_line_up_with_the_mixin_signature(client) -> None:
    """web-shim.js calls these with a positional array; this is the check
    that the array the frontend sends still fits the Python signature."""
    resp = client.post(
        "/api/scan_library_duplicates",
        json=["/does/not/exist", True, "both", 4.0, False, 0.95, False],
    )
    assert resp.status_code == 200
    assert "does not exist" in resp.json()["result"]["error"]

    resp = client.post(
        "/api/resolve_library_duplicates", json=[["/a"], ["/b"], "trash", False]
    )
    assert resp.status_code == 200
    assert "run a library duplicate scan first" in resp.json()["result"]["error"]

    resp = client.post("/api/restore_library_duplicates", json=[""])
    assert resp.status_code == 200
    assert resp.json()["result"]["error"] == "No path given"


def test_the_dashboard_and_csv_import_are_in_the_page(client) -> None:
    html = client.get("/").text
    assert 'id="view-stats"' in html
    assert 'id="stats-body"' in html
    assert "onclick=\"switchView('stats'); loadStats();\"" in html
    # The CSV button sits next to the link/search toggle: a third way in.
    assert 'id="csvFileInput"' in html
    assert 'onclick="openCsvPicker()"' in html


def test_the_dashboard_round_trips_over_http(client) -> None:
    from SpotiFLAC.core import download_log

    download_log.record(
        title="Song", artist="Queen", provider="ext:tidal-web", genre="Rock"
    )

    # Positional arguments, exactly as web-shim.js sends them:
    # get_stats(year, days, top).
    result = client.post("/api/get_stats", json=[None, None, 5]).json()["result"]

    assert result["totals"]["tracks"] == 1
    assert result["top_artists"][0]["name"] == "Queen"


def test_csv_preview_round_trips_over_http(client) -> None:
    content = (
        "Track URI,Track Name,Artist Name(s)\n"
        "spotify:track:4uLU6hMCjMI75M1A2tKUQC,Never Gonna Give You Up,Rick Astley\n"
    )
    # preview_csv(content, name, delimiter, min_score)
    result = client.post(
        "/api/preview_csv", json=[content, "export.csv", None, None]
    ).json()["result"]

    assert result["ok"] is True
    assert result["urls"] == ["https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"]
