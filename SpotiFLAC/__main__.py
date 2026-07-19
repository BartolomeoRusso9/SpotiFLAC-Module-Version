"""
SpotiFLAC/__main__.py

Alias per `python -m SpotiFLAC`: delega interamente a launcher.py in modo che
`python -m SpotiFLAC ...` e il comando `spotiflac ...` (console_script)
eseguano esattamente lo stesso codice, senza logica duplicata.
"""

from __future__ import annotations

from .launcher import main

if __name__ == "__main__":
    main()
