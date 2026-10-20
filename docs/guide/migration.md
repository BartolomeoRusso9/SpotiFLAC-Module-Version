# Configuration and state

Mornye uses the `mornye` Python import namespace, the `mornye` command, and
environment variables prefixed with `MORNYE_`. Previous configuration names
and state locations are not read automatically.

## Install and import

Install from the repository using the same Python environment that runs
Mornye:

```bash
python -m pip install .
```

Python applications can import the public API with:

```python
from mornye import AsyncMornye, Mornye
```

Update scripts, service units, shortcuts, and container commands to use the
current import namespace and command.

## Environment variables

Use the `MORNYE_` prefix for configuration. For example:

| Setting | Environment variable |
| --- | --- |
| Extension registries | `MORNYE_REGISTRIES` |
| Web authentication token | `MORNYE_WEB_TOKEN` |
| Durable state directory | `MORNYE_DATA_DIR` |
| Download directory | `MORNYE_DOWNLOAD_DIR` |
| Extension directory | `MORNYE_EXT_DIR` |
| Database path | `MORNYE_DB_PATH` |
| Cache directory | `MORNYE_CACHE_DIR` |

Update deployment environments, shell profiles, secret stores, and `.env`
files to use the current variable names before starting Mornye.

## State and downloads

By default, durable state is stored in `~/.mornye`, regenerable cache data in
`~/.mornye/.cache`, and downloads in `~/Music/Mornye`. Set `MORNYE_DATA_DIR`,
`MORNYE_CACHE_DIR`, `MORNYE_DB_PATH`, or `MORNYE_DOWNLOAD_DIR` to use
different locations.

Mornye does not search for, copy, rename, or merge state from previous
locations. If you need existing profiles, settings, history, or database
contents, stop the application, make a backup, and copy only the required
data into the configured current locations. Never run two instances against
the same writable database at once.

## Docker

Use the current `MORNYE_` variables and mount persistent data at
`/home/mornye/.mornye` (or at the path configured by `MORNYE_DATA_DIR`).
Mount downloads at the path configured by `MORNYE_DOWNLOAD_DIR`. Back up
volumes before changing mount paths or Compose project names.
