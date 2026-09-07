"""Shared test setup.

The suite was reaching the network and installing real extensions. Any test
that builds the `--web` app runs its lifespan, which constructs an
`ExtensionManager` with `auto_install_downloads=True`; that reads whatever
registries the developer happens to have in `.env` or the environment,
downloads whatever they list, and unpacks it into `~/.spotiflac/extensions`.

So the suite was slow, dependent on someone else's server being up, and
capable of modifying the machine it ran on — and its results depended on
the developer's own configuration, which is the opposite of what a test
suite is for.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_registry_bootstrap(request, monkeypatch):
    """Neutralises the automatic registry fetch for every test.

    Patched at `ensure_download_providers` rather than by blanking the
    environment: registry URLs are merged from the environment, `.env` files
    *and* the saved GUI config (see registry_config.effective_urls), so
    unsetting a variable is not enough to be sure nothing is reachable.

    A test that genuinely exercises the bootstrap opts back in with
    `@pytest.mark.uses_registry` — tests/test_core_logic.py's
    dedup-per-process checks do, and pass their own fake registry URL.
    """
    if request.node.get_closest_marker("uses_registry"):
        return

    from SpotiFLAC.extensions.manager import ExtensionManager

    monkeypatch.setattr(
        ExtensionManager,
        "ensure_download_providers",
        lambda self, registry_url=None: None,
    )


@pytest.fixture(autouse=True)
def _isolated_download_dir(request, monkeypatch, tmp_path_factory):
    """Keeps the suite out of the user's real music library.

    `SpotiFLAC_API.__init__` starts every instance at DEFAULT_DOWNLOAD_DIR —
    ~/Music/SpotiFLAC — and several code paths mkdir it before doing
    anything else. `--web-multiuser` is the loud one: webapp's ApiRegistry
    gives each account its own folder under the base download dir, so
    merely logging a test account in left `alice-2bd806c9` and
    `bob-81b637d8` sitting in the developer's library, in among their
    actual albums (tests/test_webapp_multiuser.py,
    test_webapp_isolation.py, test_webapi_integration.py all did it).
    test_playcount_throttle.py had already worked around the same thing one
    test at a time; this closes it for the whole suite.

    Only the constant is moved, not `paths.default_download_dir()` itself.
    Modules and tests that did `from ... import default_download_dir` at
    import time hold the original function and would not see a patch there
    anyway, so patching it buys nothing and makes the two disagree — which
    is a difference some tests quite reasonably assert on.

    The stand-in still ends in "Music/SpotiFLAC": that suffix is itself
    asserted, and a temporary directory is no reason to change the shape of
    the path under test.
    """
    if request.node.get_closest_marker("uses_real_download_dir"):
        return

    import os

    stand_in = os.path.join(str(tmp_path_factory.mktemp("home")), "Music", "SpotiFLAC")
    monkeypatch.setattr("SpotiFLAC.app.DEFAULT_DOWNLOAD_DIR", stand_in)


@pytest.fixture(autouse=True)
def _isolated_extension_dir(request, monkeypatch, tmp_path_factory):
    """Keeps anything that *does* install from touching ~/.spotiflac."""
    if request.node.get_closest_marker("uses_real_ext_dir"):
        return
    monkeypatch.setenv("SPOTIFLAC_EXT_DIR", str(tmp_path_factory.mktemp("extensions")))


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "uses_registry: test drives ExtensionManager.ensure_download_providers "
        "itself (with its own URL); the autouse no-op is skipped",
    )
    config.addinivalue_line(
        "markers",
        "uses_real_ext_dir: test needs the configured extension directory "
        "rather than a temporary one",
    )
    config.addinivalue_line(
        "markers",
        "uses_real_download_dir: test needs the configured download directory "
        "rather than a temporary one",
    )


@pytest.fixture(autouse=True)
def _isolated_database(monkeypatch, tmp_path_factory):
    """Points core/db.py at a throwaway file for every test.

    Without this the suite would read and write ~/.spotiflac/spotiflac.db —
    the developer's real queue, download log and subscriptions — for the same
    reasons _isolated_extension_dir exists.
    """
    from SpotiFLAC.core import db

    db_file = tmp_path_factory.mktemp("db") / "spotiflac.db"
    monkeypatch.setenv(db.DB_PATH_ENV, str(db_file))
    db.reset_for_tests()
    yield
    db.reset_for_tests()
