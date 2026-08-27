#!/usr/bin/env python3
"""Standalone command-line tool for REGISTRY MAINTAINERS: generate an
Ed25519 signing key and sign registry entries, for users who choose to
trust your key via extensions/trust.py.

This tool has nothing to do with running SpotiFLAC or installing
extensions — it's for the other side of the trust relationship: producing
a signature to put in your own registry.json's entries, so that anyone who
has added your public key as trusted (see extensions/trust.py /
`--trust-key-add`) gets a "signed" badge instead of "checksum-only" for
your extensions.

Usage:
    # One-time: generate a keypair. Keep the private key secret; publish
    # the public key however you publish your registry (README, website,
    # in person, etc.) — verifying it came from you is on your users.
    python -m SpotiFLAC.tools.registry_signing_cli keygen

    # Per release: sign one entry's (id, version, sha256, download_url).
    python -m SpotiFLAC.tools.registry_signing_cli sign \\
        --private-key <base64> \\
        --id tidal-web --version 1.2.0 \\
        --sha256 <hex> --download-url https://example.com/tidal-web.spotiflac-ext

    # Prints the "signature" value to copy into that entry in your
    # registry.json.
"""

from __future__ import annotations

import argparse
import base64
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from SpotiFLAC.extensions.trust import canonical_message


def _cmd_keygen(_args: argparse.Namespace) -> int:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    priv_b64 = base64.b64encode(
        private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
    ).decode()
    pub_b64 = base64.b64encode(
        public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode()

    print("Private key (KEEP SECRET — needed to sign future releases):")
    print(f"  {priv_b64}")
    print()
    print("Public key (publish this — your users add it with --trust-key-add):")
    print(f"  {pub_b64}")
    return 0


def _cmd_sign(args: argparse.Namespace) -> int:
    try:
        raw = base64.b64decode(args.private_key, validate=True)
        private_key = Ed25519PrivateKey.from_private_bytes(raw)
    except Exception as exc:
        print(f"Error: invalid --private-key: {exc}", file=sys.stderr)
        return 1

    message = canonical_message(args.id, args.version, args.sha256, args.download_url)
    signature = private_key.sign(message)
    signature_b64 = base64.b64encode(signature).decode()

    print("Add this to the entry in your registry.json:")
    print(f'  "signature": "{signature_b64}"')
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("keygen", help="Generate a new Ed25519 signing keypair")

    sign_parser = subparsers.add_parser("sign", help="Sign one registry entry")
    sign_parser.add_argument("--private-key", required=True, metavar="BASE64")
    sign_parser.add_argument("--id", required=True, help="Extension id")
    sign_parser.add_argument("--version", required=True)
    sign_parser.add_argument("--sha256", required=True, help="Package sha256 (hex)")
    sign_parser.add_argument("--download-url", required=True)

    args = parser.parse_args(argv)
    if args.command == "keygen":
        return _cmd_keygen(args)
    return _cmd_sign(args)


if __name__ == "__main__":
    sys.exit(main())
