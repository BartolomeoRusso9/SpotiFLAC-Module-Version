"""Mixins that make up SpotiFLAC_API (see app.py).

`SpotiFLAC_API` is the single object both GUI transports bind every exposed
method to — pywebview's `js_api` for the desktop window, and webapp.py's
`ALLOWED_METHODS` dispatcher for `--web` mode — so splitting it into several
top-level objects isn't an option without changing both entry points and the
frontend's calling convention (`pywebview.api.<method>`).

What *can* move without touching either transport is *where a method's body
lives*: Python composes `class SpotiFLAC_API(SomeMixin, ...):` exactly the
same as if the methods were written directly in the class, and `self.<attr>`
inside a mixin's methods resolves against the final composed instance
regardless of which mixin (or the main class itself) actually defines that
attribute. That's what these mixins are: the same methods, same names, same
behavior, moved out of app.py's 2000+-line single class body into smaller,
independently-readable files grouped by what they do.

This is a first, intentionally partial pass — see the "God object" note in
app.py's own module docstring for what's still there and why.
"""
