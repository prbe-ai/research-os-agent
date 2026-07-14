"""The ``probe`` command-line adapter over :mod:`probe.sdk`."""

from ..sdk.client import Client
from . import main as _implementation

app = _implementation.app


def main(argv: list[str] | None = None) -> int:
    # Preserve the original public monkeypatch seam while keeping implementation
    # code in its own submodule.
    _implementation.Client = Client
    return _implementation.main(argv)


__all__ = ["Client", "app", "main"]
