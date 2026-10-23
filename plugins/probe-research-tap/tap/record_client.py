"""Ask the Probe service to record this session, instead of a `tap watch` of its own (plan A2).

    python -m tap record  --session-id S --cwd C (--transcript T | --transcript-dir D) [--plugin-root R]
    python -m tap recording --session-id S
    python -m tap finish  --session-id S [--wait SECONDS]

`record` (SessionStart, and `tap start`): ensures the service, then
`session.record` with what `tap watch` would have been started with -- the
session's id, folder and transcript, this plugin folder (the service runs the
newest one it is given), the session's mode, its agent process (`tap owner`'s
record) and the environment variables a recorder reads (TAP_ENV, never the
rest of the agent's environment). Exit 0: the service records it (and
`cfg.hosted_file` names the service's pid), start no `tap watch`. Anything
else (exit 1: no service, an older one, Windows, refused, PROBE_TAP_HOSTED=0):
start `tap watch` as before. Bounded: at most TOTAL_S, inside SessionStart's 10 s.

`recording`: exit 0 when the service records the session now.

`finish` (SessionEnd): the session ended; the service finishes it (its last
pass and FINALIZE, as SIGTERM does to `tap watch`) and this waits up to
`--wait` for that. Exit 0 when it finished, 1 when the service does not
record it, 2 when it did not finish in time. The shutdown sentinel the hook
touches first ends it too: this only adds the wait.

Stdlib only; never raises.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import sys
import time
from pathlib import Path

from tap import __version__
from tap import config as cfg
from tap import owner as session_owner
from tap.service.protocol import Method

#: "0": never ask the service; every session gets its own `tap watch` (the way back).
HOSTED_ENV = "PROBE_TAP_HOSTED"
#: The environment a recorder reads, by name or prefix: where the agent and
#: Probe keep their files, how the network is reached, the tap's own settings.
#: The service keeps the session's copy in memory and, to resume it after a
#: restart, on disk (0600): never the rest of the agent's environment.
TAP_ENV_NAMES = frozenset({
    "HOME", "PATH", "USER", "LOGNAME", "SHELL", "LANG", "TZ", "TMPDIR", "PLUGIN_ROOT", "UV_TOOL_DIR", "UV_TOOL_BIN_DIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    # Windows: where the profile and temp folders are (Path.home() reads
    # USERPROFILE there, not HOME), and what a Python process needs to run.
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "TEMP", "TMP",
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "USERNAME", "USERDOMAIN",
})
TAP_ENV_PREFIXES = ("PROBE_", "PRBE_", "CLAUDE_", "CODEX_", "KIMI_", "PI_", "XDG_", "LC_")
#: All of `record`, a service start and a restart onto this tap included.
TOTAL_S = 6.0
CALL_TIMEOUT_S = 2.0


#: A name like these is a credential: only Probe's own (`PROBE_*`, `PRBE_*`:
#: the tap's token overrides, the daemon's key) go to the service, which keeps
#: them on disk to resume (0600). Never an agent's (CLAUDE_CODE_OAUTH_TOKEN,
#: CODEX_API_KEY, KIMI_API_KEY...): no recorder reads them.
_CREDENTIAL = re.compile(r"(TOKEN|KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.IGNORECASE)


def tap_env(environ=None) -> dict:
    environ = os.environ if environ is None else environ
    return {
        k: v for k, v in environ.items()
        if isinstance(v, str) and (k in TAP_ENV_NAMES or k.startswith(TAP_ENV_PREFIXES))
        and (not _CREDENTIAL.search(k) or k.startswith(("PROBE_", "PRBE_")))
    }


def enabled() -> bool:
    return os.name in ("posix", "nt") and os.environ.get(HOSTED_ENV, "1") != "0"


def _connection(start: bool, deadline: float):
    from tap.service import client

    conn = client.connect(min(CALL_TIMEOUT_S, max(0.1, deadline - time.monotonic())))
    if conn is None and start:
        client.ensure()
        while conn is None and time.monotonic() < deadline:
            time.sleep(0.05)
            conn = client.connect(min(CALL_TIMEOUT_S, max(0.1, deadline - time.monotonic())))
    if conn is None:
        return None
    try:
        from tap.service.protocol import Client

        conn.hello(Client.TAP, __version__)
    except Exception:  # noqa: BLE001
        conn.close()
        return None
    return conn


def _call(method: str, params: dict, *, deadline: float, start: bool = False):
    conn = _connection(start, deadline)
    if conn is None:
        return None
    try:
        conn.sock.settimeout(max(0.1, deadline - time.monotonic()))
        return conn.call(method, params)
    except Exception:  # noqa: BLE001 -- an older service (unknown method), a refusal: `tap watch`
        return None
    finally:
        conn.close()


def _write_hosted(sid: str, pid: object) -> None:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return
    path = cfg.hosted_file(sid)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with contextlib.suppress(OSError):
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(pid))
        os.replace(tmp, path)
    with contextlib.suppress(OSError):
        tmp.unlink()


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        pass
    import subprocess

    try:
        return subprocess.run(  # noqa: S603 - fixed binary, no shell
            ["/bin/ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def hosted_pid(sid: str) -> int | None:
    """The Probe service's pid when it records `sid` (its marker names a live
    service of this user), else None. Never asks the service: a file, a
    `kill -0`, the process's command line."""
    path = cfg.hosted_file(sid)
    try:
        if hasattr(os, "getuid") and path.lstat().st_uid != os.getuid():
            return None
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if pid <= 1:
        return None
    from tap.service import procid

    if not procid.exists(pid):
        return None
    return pid if _is_service(pid) else None


def _is_service(pid: int) -> bool:
    """Is `pid` this user's Probe service? Its command line names it on POSIX;
    on Windows (no other process's command line to read) it is the process
    that named itself in the service's lock file, still running."""
    if os.name != "nt":
        return "probe.service" in _cmdline(pid)
    from tap.service import paths, procid

    try:
        pid_s, _, start = paths.lock_path().read_text(encoding="utf-8").strip().partition(" ")
    except OSError:
        return False
    return pid_s == str(pid) and bool(start) and procid.alive(pid, start) is True


def _params(args) -> dict:
    from tap import service_link

    sid = args.session_id
    record = session_owner._read(sid)
    owner = {"pid": record.pid, "start_time": record.start} if record else service_link._env_owner()
    root = args.plugin_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return {
        "sid": sid,
        "harness": cfg.capture_source(),
        "mode": service_link.session_mode(sid) or "inline",
        "owner": owner,
        "generation": int(time.time() * 1000),
        "state_dir": str(service_link.lease.sessions_dir().parent.resolve()),
        "cwd": os.path.abspath(str(args.cwd)),
        "transcript": os.path.abspath(str(args.transcript)) if args.transcript else None,
        "transcript_dir": os.path.abspath(str(args.transcript_dir)) if args.transcript_dir else None,
        "tap_root": os.path.abspath(str(root)),
        "tap_version": __version__,
        "env": tap_env(),
    }


def try_record(args) -> bool:
    """`record`, for callers in this package (`tap start`). Never raises."""
    if not enabled():
        return False
    try:
        deadline = time.monotonic() + TOTAL_S
        params = _params(args)
        answer = _call(Method.SESSION_RECORD, params, deadline=deadline, start=True)
        while isinstance(answer, dict) and answer.get("restart") and time.monotonic() < deadline:
            # The service ran an older tap and restarts onto this one: ask the next one.
            time.sleep(0.25)
            answer = _call(Method.SESSION_RECORD, params, deadline=deadline, start=True)
        if isinstance(answer, dict) and answer.get("recording") is True:
            _write_hosted(args.session_id, answer.get("pid"))
            return True
    except Exception:  # noqa: BLE001 -- `tap watch` is always the way back
        pass
    return False


def record(args) -> int:
    return 0 if try_record(args) else 1


def recording(args) -> int:
    answer = _call(Method.SESSION_RECORDING, {"sid": args.session_id}, deadline=time.monotonic() + CALL_TIMEOUT_S)
    return 0 if isinstance(answer, dict) and answer.get("recording") is True else 1


def finish(args) -> int:
    wait = max(0.0, min(60.0, float(args.wait or 0)))
    answer = _call(
        Method.SESSION_FINISH, {"sid": args.session_id, "wait": wait}, deadline=time.monotonic() + wait + CALL_TIMEOUT_S
    )
    if not isinstance(answer, dict) or not answer.get("recording"):
        return 1
    return 0 if answer.get("finished") else 2


def main(command: str, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"tap {command}")
    parser.add_argument("--session-id", required=True)
    if command == "record":
        parser.add_argument("--cwd", required=True)
        parser.add_argument("--transcript")
        parser.add_argument("--transcript-dir")
        parser.add_argument("--plugin-root")
    if command == "finish":
        parser.add_argument("--wait", type=float, default=0.0)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 1
    try:
        return {"record": record, "recording": recording, "finish": finish}[command](args)
    except Exception:  # noqa: BLE001 -- `tap watch` is always the way back
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2:]))
