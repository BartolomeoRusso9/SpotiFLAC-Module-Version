"""Host runtime features requested by extension manifests.

Extensions declare features in ``requiredRuntimeFeatures``.  Provider code only
depends on this module; protocol implementations are intentionally kept behind
the feature boundary so they can be replaced or versioned without changing a
provider adapter.
"""

from __future__ import annotations

from typing import Any

from SpotiFLAC.core.signed_session_mobile import (
    SignedSessionClient,
    client_from_manifest,
    perform_signed_fetch,
)


def supports(manifest: dict[str, Any], feature: str) -> bool:
    return any(
        value == feature or value.startswith(feature + "@")
        for value in manifest.get("requiredRuntimeFeatures", [])
    )


def signed_session_client(manifest: dict[str, Any]) -> SignedSessionClient | None:
    """Build the versioned signed-session host feature requested by a package."""
    config = manifest.get("signedSession")
    if not config or not supports(manifest, "signedSession"):
        return None
    return client_from_manifest(config)


async def signed_fetch(manifest: dict[str, Any], method: str, path: str, body: Any, headers: dict) -> dict:
    client = signed_session_client(manifest)
    if client is None:
        return {"error": "extension did not declare signedSession@1"}
    try:
        return await perform_signed_fetch(client, method, path, body, headers)
    finally:
        await client.aclose()
