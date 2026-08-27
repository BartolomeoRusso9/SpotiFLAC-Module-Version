"""SpotiFLAC/__main__.py.

Alias for `python -m SpotiFLAC`: delegates entirely to launcher.py so that
`python -m SpotiFLAC ...` and the `spotiflac ...` command (console_script)
run exactly the same code, with no duplicated logic.
"""

from __future__ import annotations

from .launcher import main

if __name__ == "__main__":
    main()
