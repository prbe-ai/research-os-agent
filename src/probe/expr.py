"""Compatibility import for ``probe.sdk.expr``.

Lets an agent write ``from probe import expr`` — the shape every example uses —
without reaching into ``probe.sdk``.
"""

from .sdk.expr import (
    Expr,
    const,
    ema,
    exp,
    log,
    log10,
    neg,
    series,
    sma,
    spec,
    sqrt,
)

__all__ = [
    "Expr",
    "const",
    "ema",
    "exp",
    "log",
    "log10",
    "neg",
    "series",
    "sma",
    "spec",
    "sqrt",
]
