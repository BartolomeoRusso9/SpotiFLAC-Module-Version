#!/usr/bin/env python3
"""Standalone command-line tool for the MAINTAINER: read and rewrite the
encrypted endpoint registry that core/__init__.py fetches from its gist.

The payload in that gist is AES-GCM encrypted (see core/__init__.py's
_decrypt_base64_payload), so a value cannot be edited by hand: the whole
document has to be decrypted, changed, and re-encrypted. This tool does
that round trip.

It has nothing to do with running SpotiFLAC. Nobody but whoever owns the
gist has any use for it.

Usage:
    # Look at what is published right now.
    python -m SpotiFLAC.tools.cloud_config_cli get -o registry.json

    # Change or add one value. Dotted paths address nested keys, and
    # intermediate objects are created as needed.
    python -m SpotiFLAC.tools.cloud_config_cli set acoustid.client bDjK6RXxPL \\
        -o updated.b64

    # Or edit registry.json by hand, then re-encrypt the whole file.
    python -m SpotiFLAC.tools.cloud_config_cli encrypt registry.json -o updated.b64

Then paste the contents of the .b64 file into the gist as its entire body.

A note on what the encryption is for: the key is derived from constants in
this package's own source, so anyone reading the code can decrypt the
payload. That is by design — it raises the bar against scrapers trawling
public repositories for endpoints and tokens, which is the realistic
threat, and it is not a claim that the contents are secret from a
determined reader. Do not put anything in here that would actually hurt to
lose.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from SpotiFLAC.core import _AAD, _CLOUD_URL, _SEED_PARTS


def _key() -> bytes:
    """The AES key, derived exactly the way core/__init__.py derives it."""
    hasher = hashlib.sha256()
    for part in _SEED_PARTS:
        hasher.update(part)
    return hasher.digest()


def decrypt(b64_string: str) -> dict:
    clean = "".join(b64_string.split()).replace("-", "+").replace("_", "/")
    clean += "=" * (-len(clean) % 4)
    raw = base64.b64decode(clean)
    return json.loads(AESGCM(_key()).decrypt(raw[:12], raw[12:], _AAD).decode("utf-8"))


def encrypt(payload: dict) -> str:
    """Encrypts `payload` into the base64 blob the gist should contain.

    A fresh random nonce every time: reusing one across two documents under
    the same key is the one mistake AES-GCM does not forgive.
    """
    nonce = os.urandom(12)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return base64.b64encode(nonce + AESGCM(_key()).encrypt(nonce, body, _AAD)).decode()


def fetch_published() -> dict:
    """Downloads and decrypts whatever the gist is serving right now."""
    from SpotiFLAC.core.http import httpx

    import time

    resp = httpx.get(
        f"{_CLOUD_URL}?t={int(time.time())}",
        headers={"User-Agent": "SpotiFLAC-Agent"},
        timeout=15.0,
    )
    resp.raise_for_status()
    return decrypt(resp.text)


def _set_path(payload: dict, dotted: str, value: str) -> None:
    """Sets `dotted` (e.g. "acoustid.client") to `value`, creating any
    missing intermediate objects. Refuses to overwrite a non-object on the
    way down rather than silently discarding it.
    """
    parts = dotted.split(".")
    node: Any = payload
    for part in parts[:-1]:
        nxt = node.get(part)
        if nxt is None:
            nxt = node[part] = {}
        elif not isinstance(nxt, dict):
            raise SystemExit(
                f"refusing to descend into '{part}': it holds a "
                f"{type(nxt).__name__}, not an object"
            )
        node = nxt
    node[parts[-1]] = value


def _write(text: str, out: str | None, *, what: str) -> None:
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"{what} written to {out} ({len(text)} chars)", file=sys.stderr)
    else:
        print(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m SpotiFLAC.tools.cloud_config_cli",
        description="Read and rewrite the encrypted endpoint registry gist.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_get = sub.add_parser("get", help="fetch and decrypt what is published")
    p_get.add_argument("-o", "--out", help="write JSON here instead of stdout")

    p_set = sub.add_parser("set", help="change one value and re-encrypt")
    p_set.add_argument("path", help="dotted key path, e.g. acoustid.client")
    p_set.add_argument("value")
    p_set.add_argument(
        "-i",
        "--input",
        help="start from this JSON file instead of what the gist serves",
    )
    p_set.add_argument("-o", "--out", help="write the base64 blob here")

    p_enc = sub.add_parser("encrypt", help="encrypt a whole JSON file")
    p_enc.add_argument("file")
    p_enc.add_argument("-o", "--out", help="write the base64 blob here")

    args = parser.parse_args(argv)

    if args.command == "get":
        _write(
            json.dumps(fetch_published(), indent=4, ensure_ascii=False),
            args.out,
            what="registry",
        )
        return 0

    if args.command == "set":
        if args.input:
            with open(args.input, encoding="utf-8") as fh:
                payload = json.load(fh)
        else:
            payload = fetch_published()
        _set_path(payload, args.path, args.value)
        blob = encrypt(payload)
        # Prove the blob decodes back to what we meant before it is published:
        # a bad paste is far cheaper to catch here than in every client.
        if decrypt(blob) != payload:
            raise SystemExit("re-encryption did not round-trip; refusing to output")
        _write(blob, args.out, what="encrypted payload")
        return 0

    if args.command == "encrypt":
        with open(args.file, encoding="utf-8") as fh:
            payload = json.load(fh)
        blob = encrypt(payload)
        if decrypt(blob) != payload:
            raise SystemExit("re-encryption did not round-trip; refusing to output")
        _write(blob, args.out, what="encrypted payload")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
