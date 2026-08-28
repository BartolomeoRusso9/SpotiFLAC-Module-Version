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
