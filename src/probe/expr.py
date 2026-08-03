"""Compatibility import for ``probe.sdk.expr``.

Lets an agent write ``from probe import expr`` — the shape every example uses —
without reaching into ``probe.sdk``.

Re-exports by ``__all__`` rather than by a hand-written list: the operator set
grows, and a literal list here silently omitted every new one until someone
noticed at a call site.
"""

from .sdk.expr import *  # noqa: F401,F403
from .sdk.expr import __all__ as __all__
