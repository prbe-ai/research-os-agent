"""Keeps the session's Probe daemon worker alive, from inside `tap watch`.

`tap watch` is already the per-session process with a lifecycle on every
harness (spawned at SessionStart, respawned by `ensure-daemon` on each prompt,
stopped at SessionEnd or when the transcript's reader disappears). The worker
rides it as a CHILD PROCESS -- not a thread, because it makes network calls and
must be killable without touching capture.

    tap watch tick ──> poll()
                         state == daemon and no child ─> spawn `probe daemon worker` (v2)
                         child died ─> respawn, at most RESPAWNS_PER_MINUTE; after a
                           crash (EXIT_CRASHED, or any other failing exit) not
                           before a growing wait; and not at all after it exits
                           EXIT_DO_NOT_RESPAWN (no AI libraries, no key, the store
                           is newer than the code) until the switch moves
                         state left daemon ─> the child notices and exits itself
    tap watch exit ──> stop(): SIGTERM and return. The worker is in its OWN
                       process group, finishes its queue (bounded) and exits on
                       its own -- capture's FINALIZE never waits on a model. A
                       worker whose tap watch died without stop() sees its
                       parent gone and does the same.

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



class Supervisor:
    def __init__(self, *, session_id: str, transcript: Path, cwd: Path) -> None:
        self.session_id = session_id
        self.transcript = transcript
        self.cwd = cwd
        self.child: subprocess.Popen | None = None
        self.spawns: list[float] = []
        self.gave_up_in: str | None = None  # the state a do-not-respawn exit happened in
        self.crashes = 0  # in a row
        self.not_before = float("-inf")  # monotonic: no spawn before this, after a crash
        self.sock: socket.socket | None = None
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
        # Daemon v2 has no shadow mode: it runs only in `daemon`.
        return state == worker.STATE_DAEMON

    def _open_socket(self) -> None:
        """Listen for SDK messages (a UNIX datagram socket): nothing to accept, nothing
        to block on, and a sender with no listener fails at once and floats its run."""
        if self.sock is not None:
            return
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

    def _spawn_argv(self) -> list[str] | None:
        """Daemon v2: `probe daemon worker` from the researcher's CLI environment.
        No CLI (or one too old to have it, or without the AI libraries): the
        worker exits EXIT_DO_NOT_RESPAWN, the lease is never taken, and the agent
        records -- fall back to the agent, never to silence."""
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
        try:
            if self.child is not None and self.child.poll() is not None:
                code = self.child.returncode
                log.info("companion worker exited (%s)", code)
                # 3: the worker said respawning cannot help (no AI libraries, no key,
                # a store newer than the code). 2: the CLI has no `daemon worker`
                # (too old). Either way: stop until the switch moves; the agent records.
                if code in (worker.EXIT_DO_NOT_RESPAWN, 2):
                    self.gave_up_in = lease.session_state(self.session_id)
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
                        env={**os.environ},
                        # Its own group: a signal to the capture daemon's group must not
                        # cut the worker's final cycle short.
                        start_new_session=True,
                    )
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
