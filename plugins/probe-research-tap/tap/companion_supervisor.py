"""Keeps the session's Probe daemon worker alive, from inside `tap watch`.

`tap watch` is already the per-session process with a lifecycle on every
harness (spawned at SessionStart, respawned by `ensure-daemon` on each prompt,
stopped at SessionEnd or when the transcript's reader disappears). The worker
rides it as a CHILD PROCESS -- not a thread, because it makes network calls and
must be killable without touching capture.

    tap watch tick ──> poll()
                         state == daemon (or read only in a daemon-profile
                           session: the reader alone) and no child ─> spawn
                           `probe daemon worker` (v2)
                         child died ─> respawn, at most RESPAWNS_PER_MINUTE; after a
                           crash (EXIT_CRASHED, or any other failing exit) not
                           before a growing wait; after EXIT_RESPAWN_LATER (it left
                           over the switch) once the switch reads `daemon`; after
                           EXIT_RETRY_LIBS (no AI libraries) not before
                           LIBS_RETRY_SECONDS, because an update or `probe daemon
                           install` puts them back; and not at all after it exits
                           EXIT_DO_NOT_RESPAWN (no key, the store is newer than
                           the code) until the switch moves
                         state left daemon ─> the child notices and exits itself
                       another live worker holds the session lock (a
                         resumed session's predecessor, one whose exit this
                         never saw) ─> start none; keep its `.handover` ask
                         fresh, so a stopping holder hands over at its next
                         model round, and start one once it lets go
    tap watch exit ──> stop(): SIGTERM and return. The worker is in its OWN
                       process group, finishes its queue (bounded) and exits on
                       its own -- capture's FINALIZE never waits on a model. A
                       worker whose tap watch died without stop() sees its
                       parent gone and does the same.

Hosted by a Probe service that supervises workers itself (`service`: the
`probe.service.workers.Link` on the recorder's controls), the worker is handed
to it once this recorder owns the session (`claimed()`: after the journal's
live claim, or a pass later -- a recorder about to step back for another starts
nothing) -- what
this would spawn, and how to read the switch -- and this only drains the SDK
socket; stop() releases it. One owner, never two: a service that says no (or
an older one, with no link) leaves it here.

While the worker is down its lease lapses and the agent records; nothing here
has to tell anyone.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

from tap import companion_lease as lease
from tap import companion_ledger as ledger_mod
from tap import companion_worker as worker
from tap import config as cfg

log = logging.getLogger("tap.companion.supervisor")

RESPAWNS_PER_MINUTE = 5
#: The worker's exit after a crash (`probe.daemon.worker.EXIT_CRASHED`).
EXIT_CRASHED = 4
#: The worker left because of the switch -- unreadable for a minute, or not `daemon`
#: while it waited for the session lock (`probe.daemon.worker.EXIT_RESPAWN_LATER`).
#: Never "gave up": the switch can read `daemon` again by the time this looks, and a
#: give-up recorded in `daemon` would never respawn. It starts again once the switch
#: reads `daemon`, not before RESPAWN_LATER_SECONDS.
EXIT_RESPAWN_LATER = 5
RESPAWN_LATER_SECONDS = 5
#: Told to every worker this spawns (`probe.daemon.worker.SUPERVISOR_FOLLOWS_INLINE_ENV`):
#: `wanted()` is False while the researcher has Probe inline, so a worker leaving
#: for it exits 0, not EXIT_DO_NOT_RESPAWN (whose hold a quick switch back would pin).
FOLLOWS_INLINE_ENV = "PROBE_TAP_FOLLOWS_INLINE"
#: This supervisor also recognizes a quota handoff that preserves read-only.
READ_ONLY_INLINE_ENV = "PROBE_TAP_READ_ONLY_INLINE"
#: The worker's exit when the daemon's AI libraries are missing
#: (`probe.daemon.worker.EXIT_RETRY_LIBS`), sent only to a supervisor that says it
#: retries (RETRIES_LIBS_ENV, `probe.daemon.worker.SUPERVISOR_RETRIES_LIBS_ENV`).
#: Before tap 0.9.19 that exit was EXIT_DO_NOT_RESPAWN, and a session whose
#: libraries came back (an update restates them) never got its daemon again
#: until the switch moved (2026-10-12).
EXIT_RETRY_LIBS = 6
RETRIES_LIBS_ENV = "PROBE_TAP_RETRIES_LIBS"
LIBS_RETRY_SECONDS = 300
#: After a crash, the next spawn waits this long, doubling per crash in a row.
CRASH_BACKOFF_SECONDS = 30
CRASH_BACKOFF_CAP_SECONDS = 30 * 60
#: One SDK message on the session's socket (a run start / end), at most.
MAX_DATAGRAM_BYTES = 64 * 1024
#: Messages drained per tick, so a flood cannot stall capture.
MAX_DATAGRAMS_PER_POLL = 200


def probe_cli() -> str | None:
    """The researcher's `probe` CLI: the daemon v2 worker lives in its environment."""
    found = shutil.which("probe")
    if found:
        return found
    for candidate in (Path.home() / ".local" / "bin" / "probe", Path("/usr/local/bin/probe")):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def daemon_dir() -> Path:
    return lease.sessions_dir().parent / "daemon"


#: A UNIX socket path is at most 108 bytes (less on macOS: 104).
MAX_SOCKET_PATH = 100


def socket_path(session_id: str) -> Path:
    """`<state>/probe/sessions/<sid>.sock`: the SDK's fire-and-forget channel (R3.1).

    When that path is too long for a UNIX socket, `/tmp/probe-<uid>/<hash>.sock`
    in a folder only this user can open. The SDK computes the same path
    (`probe.sdk.daemon_channel.socket_path`) -- keep the two in step."""
    import hashlib

    path = lease.sessions_dir() / (session_id + ".sock")
    if len(str(path).encode()) <= MAX_SOCKET_PATH:
        return path
    folder = Path("/tmp") / f"probe-{os.getuid()}"
    folder.mkdir(mode=0o700, exist_ok=True)
    st = folder.lstat()
    if st.st_uid != os.getuid() or st.st_mode & 0o077 or not folder.is_dir() or folder.is_symlink():
        raise OSError(f"{folder} is not a private folder of this user")
    return folder / (hashlib.sha256(session_id.encode()).hexdigest()[:24] + ".sock")

#: How long `stop()` waits for the worker before returning; it is not killed.
STOP_WAIT_SECONDS = 2
#: While another worker holds the session lock, its `.handover` ask is
#: re-touched at most this often (the worker trusts one younger than 10 s).
HANDOVER_TOUCH_SECONDS = 2


def _safe_sid(session_id: str) -> str:
    """The session's file stem under the daemon folder (`probe.daemon.store.store_path`)."""
    return "".join(c for c in session_id if c.isalnum() or c in "-_") or "session"


def lock_held(path: Path) -> bool:
    """Does a live worker hold this session lock (`<daemon>/<sid>.lock`, for its
    whole life)? A shared probe released at once. Unknown (no flock here, an
    unreadable file) reads as free: the worker's own wait is bounded."""
    try:
        import fcntl
    except ImportError:
        return False
    try:
        handle = open(path, "rb")  # noqa: SIM115 -- closed below
    except OSError:
        return False
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    else:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        return False
    finally:
        handle.close()


class Supervisor:
    def __init__(self, *, session_id: str, transcript: Path, cwd: Path, service: object = None) -> None:
        self.session_id = session_id
        self.transcript = transcript
        self.cwd = cwd
        self.child: subprocess.Popen | None = None
        self.spawns: list[float] = []
        self.gave_up_in: str | None = None  # the state a do-not-respawn exit happened in
        self.crashes = 0  # in a row
        self.not_before = float("-inf")  # monotonic: no spawn before this, after a crash
        self.sock: socket.socket | None = None
        #: The Probe service's handle on this session's worker, hosted (see above).
        self.service = service
        #: The service took the worker over: nothing is spawned here.
        self.delegated = False
        #: This recorder holds the session's live claim (`claimed()`).
        self._claimed = False
        self._touched = float("-inf")
        try:
            ledger_mod.prune()
        except Exception:  # noqa: BLE001 - housekeeping must never stop capture
            log.exception("companion ledger prune failed")

    def wanted(self) -> bool:
        state = lease.session_state(self.session_id)
        if state != self.gave_up_in:
            self.gave_up_in = None
        else:
            return False
        # Daemon v2 has no shadow mode: it runs in `daemon`, and in `read only
        # (daemon)` for its reader alone (the worker records nothing there). Never
        # in `off`, or while manual/confirmed-quota inline hands work to the agent.
        return state == worker.STATE_DAEMON or lease.reads_only(self.session_id, state)

    def _open_socket(self) -> None:
        """Listen for SDK messages (a UNIX datagram socket): nothing to accept, nothing
        to block on, and a sender with no listener fails at once and floats its run."""
        if self.sock is not None or not hasattr(socket, "AF_UNIX"):
            return  # (Windows: no datagram socket; runs start floating and are filed later)
        path = socket_path(self.session_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                path.unlink()
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.bind(str(path))
            os.chmod(path, 0o600)
            sock.setblocking(False)
            self.sock = sock
        except OSError:
            log.exception("daemon socket unavailable; runs start floating and are filed later")

    def _drain_socket(self) -> None:
        """Append each SDK message to the session's inbox, which the worker reads."""
        if self.sock is None:
            return
        lines = []
        for _ in range(MAX_DATAGRAMS_PER_POLL):
            try:
                data = self.sock.recv(MAX_DATAGRAM_BYTES)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                break
            try:
                msg = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(msg, dict):
                msg["received_at"] = time.time()
                lines.append(json.dumps(msg, separators=(",", ":")))
        if not lines:
            return
        inbox = daemon_dir() / (self.session_id + ".inbox.jsonl")
        try:
            inbox.parent.mkdir(parents=True, exist_ok=True)
            with inbox.open("a", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
        except OSError:
            log.exception("daemon inbox write failed")

    def _switch(self, env: dict) -> tuple[str | None, bool]:
        """The session's switch, and whether it wants a worker: for the service's
        supervisor, on ITS thread, so read under this session's environment."""
        from tap import threadenv

        with threadenv.install().session(env):
            state = lease.session_state(self.session_id)
            return state, state == worker.STATE_DAEMON or lease.reads_only(self.session_id, state)

    def _delegate(self) -> bool:
        """Hand the worker to the service, once. True while it supervises it."""
        if self.delegated or self.service is None:
            return self.delegated
        env = dict(os.environ)  # this session's (hosted: its thread's own)
        spec = {
            "sid": self.session_id, "transcript": str(self.transcript), "cwd": str(self.cwd),
            "source": str(cfg.capture_source()), "daemon_dir": str(daemon_dir()), "env": env,
            "switch": lambda: self._switch(env),
        }
        try:
            self.delegated = bool(self.service.supervise(spec))
        except Exception:  # noqa: BLE001 -- the worker stays here, as before
            log.exception("the Probe service did not take the daemon worker over")
        if not self.delegated:
            self.service = None  # said no: this supervises it, for good
        else:
            log.info("the Probe service supervises this session's daemon worker")
        return self.delegated

    def claimed(self) -> None:
        """The recorder keeps the session (its live claim, or a pass done without
        stepping back): hand the worker over now. Cheap and idempotent."""
        self._claimed = True
        try:
            self._delegate()
        except Exception:  # noqa: BLE001 -- the next poll tries again
            log.exception("companion supervisor hand-over failed")

    def _lock_path(self) -> Path:
        return daemon_dir() / (_safe_sid(self.session_id) + ".lock")

    def _ask_handover(self) -> None:
        """Keep the lock holder's `.handover` ask fresh (one dated ahead stays)."""
        now = time.monotonic()
        if now - self._touched < HANDOVER_TOUCH_SECONDS:
            return
        self._touched = now
        marker = daemon_dir() / (_safe_sid(self.session_id) + ".handover")
        try:
            if marker.stat().st_mtime > time.time():
                return
        except OSError:
            pass
        try:
            marker.touch()
        except OSError:
            pass

    def _withdraw_ask(self) -> None:
        """Leaving while it asked the lock's holder to hand over: the ask goes
        (as a waiting worker's does), or the holder would hand over to nobody
        instead of finishing its queue. One dated ahead is not ours."""
        if self._touched == float("-inf"):
            return
        self._touched = float("-inf")
        marker = daemon_dir() / (_safe_sid(self.session_id) + ".handover")
        try:
            if marker.stat().st_mtime <= time.time():
                marker.unlink()
        except OSError:
            pass

    def _spawn_argv(self) -> list[str] | None:
        """Daemon v2: `probe daemon worker` from the researcher's CLI environment.
        No CLI, or one too old to have it: the worker exits EXIT_DO_NOT_RESPAWN,
        the lease is never taken, and the agent records -- fall back to the
        agent, never to silence. A CLI without the AI libraries exits
        EXIT_RETRY_LIBS and is tried again after LIBS_RETRY_SECONDS."""
        cli = probe_cli()
        if cli is None:
            return None
        return [cli, "daemon", "worker", "--session-id", self.session_id, "--transcript", str(self.transcript),
                "--cwd", str(self.cwd), "--source", cfg.capture_source()]

    def poll(self) -> None:
        """Called every tick. Never raises: capture must not die for the daemon."""
        try:
            if lease.session_state(self.session_id) == worker.STATE_DAEMON:
                self._open_socket()
            self._drain_socket()
        except Exception:  # noqa: BLE001
            log.exception("daemon socket poll failed")
        if self.service is not None and not self._claimed:
            return  # hosted: nothing until this recorder owns the session
        try:
            if self.child is None and self._delegate():
                return  # the service supervises it
        except Exception:  # noqa: BLE001
            log.exception("companion supervisor hand-over failed")
        try:
            if self.child is not None and self.child.poll() is not None:
                code = self.child.returncode
                log.info("companion worker exited (%s)", code)
                # 3: the worker said respawning cannot help (no key, a store newer
                # than the code; no AI libraries on a CLI older than
                # EXIT_RETRY_LIBS). 2: the CLI has no `daemon worker`
                # (too old). Either way: stop until the switch moves; the agent records.
                if code in (worker.EXIT_DO_NOT_RESPAWN, 2):
                    self.gave_up_in = lease.session_state(self.session_id)
                elif code == EXIT_RESPAWN_LATER:
                    self.crashes = 0
                    self.not_before = time.monotonic() + RESPAWN_LATER_SECONDS
                elif code == EXIT_RETRY_LIBS:
                    # Not a crash and not a give-up: the libraries can come back.
                    self.crashes = 0
                    self.not_before = time.monotonic() + LIBS_RETRY_SECONDS
                elif code == 0:
                    self.crashes = 0
                else:
                    # A crash (the worker released the lease with its reason): try
                    # again, but not in a hot loop.
                    self.crashes += 1
                    wait = min(CRASH_BACKOFF_CAP_SECONDS, CRASH_BACKOFF_SECONDS * 2 ** (self.crashes - 1))
                    self.not_before = time.monotonic() + wait
                    log.warning("companion worker crashed (%s, %s in a row); next try in %ss", code, self.crashes, wait)
                self.child = None
            if self.child is None and self.wanted():
                now = time.monotonic()
                if now < self.not_before:
                    return
                if lock_held(self._lock_path()):
                    # Another live worker of this session: a second would only
                    # wait for it. Ask it to hand over; start one once it lets go.
                    self._ask_handover()
                    return
                self.spawns = [t for t in self.spawns if now - t < 60]
                if len(self.spawns) >= RESPAWNS_PER_MINUTE:
                    return
                self.spawns.append(now)
                argv = self._spawn_argv()
                if argv is None:
                    log.warning("no probe CLI found: the daemon cannot run; the agent records")
                    self.gave_up_in = lease.session_state(self.session_id)
                    return
                log_path = daemon_dir() / (self.session_id + ".log")
                log_path.parent.mkdir(parents=True, exist_ok=True)
                # The child keeps its own copy of the handle: ours closes after the spawn,
                # or every respawn would leak one open file into capture.
                with log_path.open("ab") as log_file:
                    self.child = subprocess.Popen(  # noqa: S603 - the researcher's own CLI, fixed argv
                        argv,
                        stdin=subprocess.DEVNULL,
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        cwd=str(self.cwd) if Path(self.cwd).is_dir() else str(Path.home()),
                        env={**os.environ, FOLLOWS_INLINE_ENV: "1", READ_ONLY_INLINE_ENV: "1", RETRIES_LIBS_ENV: "1"},
                        # Its own group: a signal to the capture daemon's group must not
                        # cut the worker's final cycle short.
                        start_new_session=True,
                    )
                self._touched = float("-inf")  # an ask made while it waited is the new worker's to clear
                log.info("companion worker started (pid %s)", self.child.pid)
        except Exception:  # noqa: BLE001
            log.exception("companion supervisor poll failed")

    def stop(self) -> None:
        """Ask the worker to finish, and do not wait: its final cycle is bounded
        by the worker itself (and it exits when it sees this process is gone)."""
        if self.sock is not None:
            try:
                self._drain_socket()
                self.sock.close()
                socket_path(self.session_id).unlink()
            except OSError:
                pass
            self.sock = None
        self._withdraw_ask()
        if self.delegated:
            # The service stops it (SIGTERM, as below) and reaps it.
            self.delegated = False
            try:
                self.service.release()
            except Exception:  # noqa: BLE001
                log.exception("companion supervisor release failed")
            return
        child, self.child = self.child, None
        if child is None or child.poll() is not None:
            return
        try:
            child.send_signal(signal.SIGTERM)
            child.wait(timeout=STOP_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            pass  # still finishing on its own
        except Exception:  # noqa: BLE001
            log.exception("companion supervisor stop failed")
