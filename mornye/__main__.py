"""mornye/__main__.py.

Alias for `python -m mornye`: delegates entirely to launcher.py so that
`python -m mornye ...` and the `mornye ...` command (console script)
run exactly the same code, with no duplicated logic.
"""

from __future__ import annotations

from .launcher import main

if __name__ == "__main__":
    main()
