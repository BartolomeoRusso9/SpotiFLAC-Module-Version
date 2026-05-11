import importlib.metadata
import requests
from packaging.version import Version

def check_for_updates():
    package_name = "spotiflac"

    try:
        current_version = importlib.metadata.version(package_name)

        resp = requests.get(f"https://pypi.org/pypi/{package_name}/json", timeout=2)

        if resp.status_code == 200:
            latest_version = resp.json()["info"]["version"]

            # 1. Determiniamo se l'aggiornamento è disponibile
            update_available = False
            try:
                if Version(current_version) < Version(latest_version):
                    update_available = True
            except Exception:
                # Fallback al confronto stringa se il parsing semver fallisce
                if current_version != latest_version:
                    update_available = True

            # 2. Se c'è un aggiornamento, stampiamo il box
            if update_available:
                width = 68

                print(f"\n ╭" + "─" * (width-2) + "╮")

                title_line = f"  NEW VERSION AVAILABLE! ({current_version} -> {latest_version})"
                print(f" │{title_line.ljust(width-2)}│")

                print(f" ├" + "─" * (width-2) + "┤")

                mod_line = f"  Module: pip install -U {package_name}"
                app_line = f"  App:    https://github.com/ShuShuzinhuu/SpotiFLAC-Module-Version"

                print(f" │{mod_line.ljust(width-2)}│")
                print(f" │{app_line.ljust(width-2)}│")

                print(f" ╰" + "─" * (width-2) + "╯\n")

    except importlib.metadata.PackageNotFoundError:
        pass
    except Exception:
        pass