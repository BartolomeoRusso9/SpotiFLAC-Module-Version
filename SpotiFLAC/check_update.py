import importlib.metadata

from packaging.version import Version

from .core.http import NetworkManager


async def check_for_updates_async() -> None:
    package_name = "spotiflac"
    client = await NetworkManager.get_async_client_safe()

    try:
        current_version = importlib.metadata.version(package_name)

        resp = await client.get(f"https://pypi.org/pypi/{package_name}/json", timeout=2)

        if resp.status_code == 200:
            latest_version = resp.json()["info"]["version"]

            update_available = False
            try:
                if Version(current_version) < Version(latest_version):
                    update_available = True
            except Exception:
                if current_version != latest_version:
                    update_available = True

            if update_available:
                pass

    except Exception:
        pass
