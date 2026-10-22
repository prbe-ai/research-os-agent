"""One environment per recording session, inside one process (plan A2, D3).

The tap was written as one `tap watch` process per session, and reads its
session's settings from `os.environ` everywhere (the harness, the plugin
folder, the token, the intervals). The Probe service runs every session's
recorder as a thread of its own process instead, so each thread needs the
environment its `tap watch` would have had.

`install()` replaces `os.environ` (once, in the hosting process) with a
mapping that answers from the CURRENT THREAD's environment when that thread
has one (`session(env)`), else from the process's own. A session thread sees
exactly its session's variables -- reads, writes, deletes, iteration, `copy()`
-- and a write there never reaches the process or another session. Other
threads (the service's own loop) see the process's environment, unchanged.

What does not follow: a child started WITHOUT `env=` inherits the process's
real environment (the C-level `environ`). Every child the recorder starts for
a session passes `env=` built from `os.environ` (the daemon worker:
`companion_supervisor`); a tool it runs only to look (`ps`, `lsof`) needs none.

Stdlib only.
"""

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Iterator, MutableMapping


class ThreadEnviron(MutableMapping):
    def __init__(self, base: MutableMapping) -> None:
        self._base = base
        self._local = threading.local()

    def _env(self) -> MutableMapping:
        own = getattr(self._local, "env", None)
        return self._base if own is None else own

    def __getitem__(self, key: str) -> str:
        return self._env()[key]

    def __setitem__(self, key: str, value: str) -> None:
        self._env()[key] = value

    def __delitem__(self, key: str) -> None:
        del self._env()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(list(self._env()))

    def __len__(self) -> int:
        return len(self._env())

    def __contains__(self, key: object) -> bool:
        return key in self._env()

    def copy(self) -> dict:
        return dict(self._env().items())

    def __repr__(self) -> str:
        return f"ThreadEnviron({'session' if getattr(self._local, 'env', None) is not None else 'process'})"

    @property
    def base(self) -> MutableMapping:
        return self._base

    @contextlib.contextmanager
    def session(self, env: dict):
        """This thread's environment is `env` (a copy) until the block ends."""
        previous = getattr(self._local, "env", None)
        self._local.env = {str(k): str(v) for k, v in env.items()}
        try:
            yield self._local.env
        finally:
            self._local.env = previous


_lock = threading.Lock()


def install() -> ThreadEnviron:
    """Make `os.environ` per-thread for session threads (once per process)."""
    with _lock:
        current = os.environ
        if isinstance(current, ThreadEnviron):
            return current
        wrapped = ThreadEnviron(current)
        os.environ = wrapped  # type: ignore[assignment] -- `os.getenv` reads the same global
        return wrapped
