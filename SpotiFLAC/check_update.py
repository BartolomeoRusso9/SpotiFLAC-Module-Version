import importlib.metadata
from packaging.version import Version
from .core.http import NetworkManager

async def check_for_updates_async():
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
                width = 68

                print("\n ╭" + "─" * (width-2) + "╮")

                title_line = f"  NEW VERSION AVAILABLE! ({current_version} -> {latest_version})"
                print(f" │{title_line.ljust(width-2)}│")

                print(" ├" + "─" * (width-2) + "┤")

                mod_line = f"  Module: pip install -U {package_name}"
                app_line = "  App:    https://github.com/ShuShuzinhuu/SpotiFLAC-Module-Version"

                print(f" │{mod_line.ljust(width-2)}│")
                print(f" │{app_line.ljust(width-2)}│")
                print(" ╰" + "─" * (width-2) + "╯\n")
    except Exception:
        pass
