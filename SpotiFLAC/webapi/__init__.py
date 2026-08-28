"""SpotiFLAC/webapi — the versioned REST API served under `/api/v1`.

Why this exists alongside the existing `/api/<method>` bridge
------------------------------------------------------------
`--web` already exposes every allowlisted `SpotiFLAC_API` method as
`POST /api/<method_name>` (see webapp.py). That is the right design for the
frontend it was built for: the browser calls the same object the desktop
window does, so one implementation serves both, and adding a button never
means adding an endpoint.

It is the wrong design for anything *else* calling it. There is no schema, no
declared request or response shape, and no separation between "this method is
part of the interface" and "this method exists". The names are GUI internals,
so renaming a method to make the desktop code clearer silently breaks every
bot pointed at the server. For a project whose stated purpose is to be the
building block other people's bots and services are built on (see the README),
that is the gap worth closing.

So: a small, explicit, versioned surface with Pydantic request and response
models, which FastAPI renders as OpenAPI at `/docs`. It is additive — the RPC
bridge is untouched and the frontend still uses it — and it deliberately
covers only what an integrator actually needs (resolve a URL, queue a
download, read progress and history, manage subscriptions, inspect
extensions) rather than mirroring all fifty methods.

Auth is inherited, not reimplemented: every path here starts with `/api/`, so
the `--web-token` middleware and the multi-user session gate in webapp.py
apply to it exactly as they do to the RPC bridge.
"""

from __future__ import annotations

from .routes import ApiDeps, build_v1_router

__all__ = ["ApiDeps", "build_v1_router"]
