from __future__ import annotations

import os
import shlex
import subprocess
from typing import List, Dict


def _has_rg() -> bool:
    try:
        subprocess.run(["rg", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def _run_rg(query: str, path: str = ".", limit: int = 200) -> List[Dict]:
    # Use fixed-string, case-insensitive search for substring match
    cmd = [
        "rg",
        "-n",            # show line numbers
        "--no-heading",  # no file headers
        "-S",            # smart-case
        "-i",            # case-insensitive
        "-F",            # fixed strings (no regex)
        "-m", str(limit),
        query,
        path,
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
        out = proc.stdout.splitlines()
        results: List[Dict] = []
        for line in out:
            # format: path:line:content
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            fpath, lineno, content = parts[0], parts[1], parts[2]
            results.append({"path": fpath, "line": int(lineno), "snippet": content.strip()})
            if len(results) >= limit:
                break
        return results
    except Exception:
        return []


def _fallback_search(query: str, path: str = ".", limit: int = 200) -> List[Dict]:
    results: List[Dict] = []
    q = query.lower()
    for root, dirs, files in os.walk(path):
        # skip hidden dirs like .git
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for fname in files:
            if fname.startswith('.'):
                continue
            fpath = os.path.join(root, fname)
            try:
                if os.path.getsize(fpath) > 2_000_000:  # skip very large files
                    continue
                with open(fpath, 'r', encoding='utf-8', errors='replace') as fh:
                    for i, line in enumerate(fh, start=1):
                        if q in line.lower():
                            results.append({"path": fpath, "line": i, "snippet": line.strip()})
                            if len(results) >= limit:
                                return results
            except Exception:
                continue
    return results


def search_code(query: str, path: str = '.', limit: int = 200) -> List[Dict]:
    """Search the repository for case-insensitive substring matches.

    Uses ripgrep (`rg`) when available for speed, otherwise falls back to a
    simple Python scanner. Returns a list of dicts: {path, line, snippet}.
    """
    if not query:
        return []
    if _has_rg():
        res = _run_rg(query, path, limit)
        if res:
            return res
    return _fallback_search(query, path, limit)
