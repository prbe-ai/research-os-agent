"""Module-level convenience: ``probe.init()`` / ``probe.log()`` / ``probe.finish()``.

W&B's defining ergonomic is that ``wandb.init()`` stashes a run somewhere global,
so ``wandb.log()`` works from anywhere without threading a handle down through
call frames. For a training script whose logging happens three libraries deep,
that is genuinely the right shape, and asking people to pass a ``run`` into code
they do not own is how instrumentation does not get added.

It is also W&B's worst failure mode. One process-wide global means two concurrent
runs clobber each other, a re-executed notebook cell logs into the previous run,
and a worker thread writes into whatever run some other thread started last.

So: the same ergonomic, with the binding in a :mod:`contextvars` variable backed
by a process default.

* :func:`init` sets both, so :func:`log` reaches the run from any thread — the
  common case, and what makes this a drop-in for the W&B shape (a plain
  contextvar would not: threads start with an empty context, so a DataLoader
  worker would silently find no run);
* an :func:`init` inside a thread, task, or block sets its own context too, and
  the context is consulted FIRST — so it shadows the default for that scope
  rather than replacing it everywhere. Two concurrent runs in one process stop
  being a silent-corruption bug and become a scoping question with an answer.

The process default is last-init-wins, matching W&B. If you genuinely run several
at once, scope them (or just use the explicit API, which has no ambient state at
all).

Nothing here is a second implementation: every function holds a real
:class:`~probe.sdk.client.Client` and :class:`~probe.sdk.run.Run` and forwards.
"""

from __future__ import annotations

import atexit
import contextvars
import sys
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import errors
from .client import Client

if TYPE_CHECKING:
    from .run import Run, SpanHandle


@dataclass
class _Binding:
    run: "Run"
    #: The client to close on finish, set only when :func:`init` built it. A
    #: caller-supplied client is theirs to close; closing it here would kill a
    #: transport (and any other run's heartbeat) they are still using.
    close_client: Client | None
    #: Set by :func:`finish`. A binding can be observed by more contexts than the
    #: one that closes it — a worker thread calling finish() cannot reach into the
    #: main thread's contextvar — so "is this run still open" has to live on the
    #: binding itself. Without it, the initiating thread keeps logging into a
    #: completed run.
    closed: bool = False


_current: contextvars.ContextVar["_Binding | None"] = contextvars.ContextVar(
    "probe_active_binding", default=None
)

_process_default: "_Binding | None" = None
_default_lock = threading.Lock()


def _binding(*, required: bool = True) -> "_Binding | None":
    found = _current.get() or _process_default
    if found is not None and found.closed:
        # Finished from some other context. Falling through to the process
        # default would be wrong too (finish() cleared it), so this is simply
        # "nothing active" — same as before any init.
        found = None
    if found is None and required:
        raise errors.RosError(
            "no active run — call probe.init() first, or use the explicit API: "
            "probe.Client().run(experiment=...)."
        )
    return found


def active_run() -> "Run | None":
    """The run bound for this context, or None.

    Named ``active_run`` and not ``probe.run``: ``probe.run`` is already the
    compatibility module for :mod:`probe.sdk.run`, and shadowing a module with a
    value is how import order becomes load-bearing."""
    found = _binding(required=False)
    return found.run if found else None


def init(*, client: Client | None = None, **kw: Any) -> "Run":
    """Open a run and bind it as the active one. Returns the run.

    Takes the same arguments as :meth:`~probe.sdk.client.Client.run`
    (``experiment``, ``hypothesis``, ``name``, ``project``, ``source``, ``tags``,
    …). Pass ``client=`` to reuse a configured one; otherwise a
    :class:`~probe.sdk.client.Client` is built from env / ``probe login`` and
    closed by :func:`finish`.

    The returned run is an ordinary handle, so ``with probe.init(...) as run:``
    works and closes on the way out."""
    global _process_default, _exit_status
    owned: Client | None = None
    if client is None:
        client = Client()
        owned = client
    try:
        run = client.run(**kw)
    except BaseException:
        # A failed init must not leak the transport (and its heartbeat threads)
        # that only exists because init built it.
        if owned is not None:
            owned.close()
        raise
    binding = _Binding(run=run, close_client=owned)
    _current.set(binding)
    with _default_lock:
        _process_default = binding
        # Per-run, not per-process: a new run must not inherit the exit status a
        # previous one's exception set.
        _exit_status = "completed"
    _install_exit_hooks()
    return run


def finish(status: str = "completed", **kw: Any):
    """Close the active run, flush its spool, and release the client init built.

    A no-op when nothing is active, so calling it twice (or in a ``finally``
    beside a ``with``) is safe."""
    global _process_default
    binding = _binding(required=False)
    if binding is None:
        return None
    try:
        return binding.run.finish(status, **kw)
    finally:
        # Mark the binding first: every context that can still see it (including
        # threads whose contextvar we cannot reach) reads this flag.
        binding.closed = True
        with _default_lock:
            if _process_default is binding:
                _process_default = None
        if _current.get() is binding:
            _current.set(None)
        if binding.close_client is not None:
            binding.close_client.close()


# -- forwarding surface --------------------------------------------------------
# Deliberately small. These are the calls that happen deep inside a training loop
# where threading a handle through is the actual friction; anything rarer is one
# `probe.active_run().<verb>()` away and does not need ambient state.
def log(metrics: dict[str, Any], **kw: Any):
    """Append metric points to the active run. See :meth:`probe.sdk.run.Run.log`."""
    return _binding().run.log(metrics, **kw)


def log_hw(metrics: dict[str, Any], **kw: Any):
    """Hardware metrics with dimensions. See :meth:`probe.sdk.run.Run.log_hw`."""
    return _binding().run.log_hw(metrics, **kw)


def log_artifact(name: str, **kw: Any):
    """Record an artifact on the active run. See :meth:`probe.sdk.run.Run.log_artifact`."""
    return _binding().run.log_artifact(name, **kw)


def span(span_type: str, **kw: Any) -> "SpanHandle":
    """Open a span on the active run. See :meth:`probe.sdk.run.Run.span` — the
    returned handle is a context manager."""
    return _binding().run.span(span_type, **kw)


# -- exit hooks ----------------------------------------------------------------
_hooks_installed = False
_previous_excepthook: Any = None
_exit_status = "completed"


def _excepthook(exc_type, exc, tb) -> None:
    global _exit_status
    # KeyboardInterrupt is a real lifecycle outcome in this vocabulary, and it is
    # the common way a training run ends early. Calling it 'failed' would lose
    # the distinction between "I stopped it" and "it broke".
    _exit_status = "canceled" if issubclass(exc_type, KeyboardInterrupt) else "failed"
    _previous_excepthook(exc_type, exc, tb)


def _finish_at_exit() -> None:
    """Close a run the script never closed.

    Without this, a script that simply ends leaves its run ``running`` until the
    server's reaper marks it ``crashed`` — the wrong answer for one that
    succeeded. atexit still runs after an unhandled exception (the traceback
    prints first), so guessing ``completed`` for every exit would be a lie;
    :func:`_excepthook` is what tells the two apart."""
    if _binding(required=False) is None:
        return
    try:
        finish(_exit_status)
    except Exception:
        # Interpreter teardown. Nothing useful can be reported from here, and
        # raising would only obscure whatever is actually ending the process.
        pass


def _install_exit_hooks() -> None:
    """Install once per process, under the lock.

    Unsynchronised, two threads calling :func:`init` concurrently — which this
    module explicitly supports — can both pass the check; the second then
    captures ``sys.excepthook`` AFTER the first replaced it, so
    ``_previous_excepthook`` becomes :func:`_excepthook` itself and any later
    uncaught exception recurses until the stack blows."""
    global _hooks_installed, _previous_excepthook
    with _default_lock:
        if _hooks_installed:
            return
        _previous_excepthook = sys.excepthook
        sys.excepthook = _excepthook
        atexit.register(_finish_at_exit)
        _hooks_installed = True
