"""In-process outbox exporter: fork-free delivery beside the training loop.

Parity F2 (docs/2026-08-04-outbox-miles-parity.md): the detached worker is
the default delivery path; this thread serves environments where forking is
hostile (Ray actors) or where delivery must ride THIS client's transport
(custom transports, tests) -- the two things a detached process can never do.

Coordination: the thread holds the same ``.worker.lock`` lease the worker
does, for its whole life. One journal, one drainer: ``maybe_spawn`` sees the
live lease and never forks beside a running exporter, and an exporter that
starts while a detached worker is draining stays passive until the worker
exits, then takes over at the next wake.

Stops: ``close()``, and auth-block -- never retry rejected credentials
(worker exit-3 parity); ops stay queued, and re-login + ``probe outbox
retry`` resumes delivery. A paused journal only idles the loop (``drain``
returns untouched) without killing the thread.
"""

from __future__ import annotations

import threading

from .journal import drain
from .outbox_worker import _lease_path

#: Same floor as the Miles exporter honored for PROBE_EXPORT_INTERVAL_SEC.
MIN_INTERVAL_SECONDS = 0.05


class OutboxExporter:
    def __init__(self, client, interval: float):
        self.client = client
        self.journal = client.journal
        self.interval = max(float(interval), MIN_INTERVAL_SECONDS)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lease_handle = None
        self._thread = threading.Thread(
            target=self._loop,
            name=f"probe-outbox-export-{self.journal.dir.name}",
            daemon=True,
        )
        self._thread.start()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def wake(self) -> None:
        self._wake.set()

    # -- loop ----------------------------------------------------------------
    def _try_lease(self) -> bool:
        """Take (or confirm) the worker lease, non-blocking. False while a
        detached worker owns the journal -- delivery is already happening."""
        if self._lease_handle is not None:
            return True
        import fcntl

        self.journal._ensure()
        handle = open(_lease_path(self.journal), "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            handle.close()
            return False
        self._lease_handle = handle
        return True

    def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                self._wake.wait(self.interval)
                self._wake.clear()
                if self._stop.is_set():
                    return
                if not self._try_lease():
                    continue
                try:
                    report = drain(
                        self.journal,
                        client_factory=self.client._outbox_client_factory(),
                    )
                except Exception:  # noqa: BLE001 -- keep the loop alive; drain
                    continue  # itself records last_error in status.json
                if report.auth_blocked:
                    return
        finally:
            self._release_lease()

    def _release_lease(self) -> None:
        if self._lease_handle is None:
            return
        import fcntl

        try:
            fcntl.flock(self._lease_handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._lease_handle.close()
        self._lease_handle = None

    def close(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=timeout)
