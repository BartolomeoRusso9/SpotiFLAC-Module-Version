# Migrating from SpotiFLAC Python

Mornye Python is the renamed continuation of the SpotiFLAC Python module. The
distribution, import namespace, command, project URLs, and default state
locations have changed. This is an in-place project rename, not an import shim
for the old `SpotiFLAC` package.

## Install the new distribution

The canonical repository is private, and `mornye-python` is not documented as
a public PyPI release. Clone the repository with an account that has access:

```bash
git clone https://github.com/BartolomeoRusso9/Mornye-Python.git
cd Mornye-Python
python -m pip uninstall SpotiFLAC
python -m pip install .
```

Use the same Python environment for uninstalling and installing. Update scripts,
service units, shortcuts, and container commands from `spotiflac` to `mornye`.
The new import namespace is `mornye`:

```python
from mornye import AsyncMornye, Mornye
```

There is no `import SpotiFLAC` compatibility package. A small set of former
public class names remains available from the new `mornye` namespace and emits
`DeprecationWarning`; update those imports to `Mornye`, `AsyncMornye`,
`MornyeConfig`, or `MornyeDownloader`.

## Environment variables

Use the new `MORNYE_` prefix for settings. Existing `SPOTIFLAC_` names are
accepted as fallbacks for settings read by Mornye, and the Compose examples
forward both forms. If both names are set, the `MORNYE_` value takes precedence.
Legacy-name warnings include variable names only, not their values.

For example:

| Old name | New name |
| --- | --- |
| `SPOTIFLAC_REGISTRIES` | `MORNYE_REGISTRIES` |
| `SPOTIFLAC_WEB_TOKEN` | `MORNYE_WEB_TOKEN` |
| `SPOTIFLAC_DATA_DIR` | `MORNYE_DATA_DIR` |
| `SPOTIFLAC_DOWNLOAD_DIR` | `MORNYE_DOWNLOAD_DIR` |
| `SPOTIFLAC_EXT_DIR` | `MORNYE_EXT_DIR` |
| `SPOTIFLAC_DB_PATH` | `MORNYE_DB_PATH` |
| `SPOTIFLAC_CACHE_DIR` | `MORNYE_CACHE_DIR` |

Registry URLs in `.env`-style files also accept the legacy registry key. Prefer
renaming old keys in shell profiles, deployment secrets, and `.env` files to
the corresponding `MORNYE_` name.

## Local data and database

Stop the old application before changing or copying its files. Back up state
first; do not run old and new instances against the same writable data
directory at the same time.

By default, Mornye stores durable state in `~/.mornye` and downloads in
`~/Music/Mornye`. To avoid silently hiding existing user data:

- If `~/.mornye` does not exist but `~/.spotiflac` does, Mornye continues to
  use the existing `~/.spotiflac` directory. It does not move or rename it.
- If both directories exist, `~/.mornye` wins and `~/.spotiflac` is left
  untouched. Do not merge them blindly; choose and copy the files you need
  while the application is stopped.
- The same rule applies to `~/Music/Mornye` and `~/Music/SpotiFLAC`: an
  existing Mornye directory wins, otherwise the old download directory
  continues to be used.
- Within the selected data directory, an existing `spotiflac.db` is used if
  `mornye.db` is absent. If both database files exist, `mornye.db` wins and
  the old database is not modified.
- Selected user-authored files from the old `~/.cache/spotiflac` location are
  copied to the new cache location when no replacement exists. The source
  files are retained.

For a deliberate move, stop Mornye, make a backup, copy the chosen data into
`~/.mornye`, and ensure `mornye.db` is the database you intend to keep before
restarting. If you use `MORNYE_DATA_DIR`, set it explicitly to the copied
directory. When both old and new trees exist, Mornye does not merge them or
delete either one.

Existing browser sessions can continue using the legacy cookie during
migration. Signing out and back in creates a session using the new cookie name.

## Docker

The example bind mount keeps the host directory `.spotiflac_docker` so existing
data stays in place; it is mounted at the new container path
`/home/mornye/.mornye`. The Portainer Compose file likewise keeps the old
named-volume identifiers and defaults its project name to the prior Compose
project name. If your old stack used a custom project name, set
`COMPOSE_PROJECT_NAME` to that same value before redeploying; otherwise Compose
may create new empty volumes.

Both Compose examples accept old `SPOTIFLAC_` variables as fallbacks. Update
the configured values to `MORNYE_` names when convenient. Back up volumes
before changing mounts or project names.
