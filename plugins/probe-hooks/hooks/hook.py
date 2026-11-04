"""Probe's hook shim, v1: hands one hook event to this machine's Probe service.

FROZEN. A harness trusts this folder by its bytes (Codex hashes every file), so
it is never edited: a change ships as a new plugin, `probe-hooks-2`. Builtin
modules only, run as `python3 -S -B`, so nothing is ever written here. The wire
is v1 of agent/src/probe/service/hooks/proxy/DESIGN.md.

    hook.py --harness <id> --event <Event> --timeout <s> [--wait reads]

The service runs the event when it takes it. Else the CLI's runner named by
`hook-entry.json` runs it here, else it is queued for the service. Fails open:
exit 0 unless an answer asks for another code.
"""

import os
import sys
import time

#: Seconds kept back from the harness's own timeout, for printing the answer.
MARGIN_S = 0.3
#: The runner is not started with less time than this left.
RUNNER_MIN_S = 0.5
#: Claude Code ends a session's hooks at this, unless the variable says otherwise.
CLAUDE_SESSION_END_S = 1.5
#: One `hooks.run` waits at most this long (the service's cap).
MAX_WAIT_MS = 60000
#: A UNIX socket path longer than this moves to /tmp (the service's rule).
MAX_SOCKET_PATH = 100
MAX_ENV = 200
MAX_ENV_VALUE = 8192
#: What of the environment the service is handed, by name and by prefix.
ENV_NAMES = frozenset((
    "HOME", "PATH", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LANGUAGE", "TZ", "TERM",
    "VIRTUAL_ENV", "CONDA_PREFIX", "PLUGIN_ROOT", "PLUGIN_DATA",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "GIT_SSL_CAINFO", "UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "UV_CACHE_DIR", "PIPX_HOME",
))
ENV_PREFIXES = ("PROBE_", "PRBE_", "CLAUDE", "CODEX_", "KIMI_", "PI_", "XDG_", "LC_")
#: Events never queued: gates, with no record-keeping route to run late.
NO_QUEUE_EVENTS = frozenset(("PreToolUse", "PermissionRequest"))
#: A queue file is on disk: no name that looks like a secret but Probe's own key.
QUEUE_SECRET_WORDS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "COOKIE", "AUTH")
QUEUE_CREDENTIAL = "PROBE_TOKEN"

_K = (
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
)
_M = 0xFFFFFFFF


def _rotr(x, n):
    return ((x >> n) | (x << (32 - n))) & _M


def sha256_hex(data):
    """SHA-256 of `data` (bytes), hex: `hashlib` is not builtin everywhere."""
    h = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]
    data = bytes(data) + b"\x80" + b"\x00" * ((55 - len(data)) % 64) + (len(data) * 8).to_bytes(8, "big")
    for start in range(0, len(data), 64):
        w = [int.from_bytes(data[start + i:start + i + 4], "big") for i in range(0, 64, 4)]
        for t in range(16, 64):
            s0 = _rotr(w[t - 15], 7) ^ _rotr(w[t - 15], 18) ^ (w[t - 15] >> 3)
            s1 = _rotr(w[t - 2], 17) ^ _rotr(w[t - 2], 19) ^ (w[t - 2] >> 10)
            w.append((w[t - 16] + s0 + w[t - 7] + s1) & _M)
        a, b, c, d, e, f, g, k = h
        for t in range(64):
            t1 = (k + (_rotr(e, 6) ^ _rotr(e, 11) ^ _rotr(e, 25)) + ((e & f) ^ (~e & g)) + _K[t] + w[t]) & _M
            t2 = ((_rotr(a, 2) ^ _rotr(a, 13) ^ _rotr(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) & _M
            a, b, c, d, e, f, g, k = (t1 + t2) & _M, a, b, c, (d + t1) & _M, e, f, g
        h = [(x + y) & _M for x, y in zip(h, (a, b, c, d, e, f, g, k))]
    return "".join("%08x" % x for x in h)


# -- where the service is (`_service_paths.py`'s rules, frozen here) -----------

def service_root():
    """`~/.local/state/probe/service`, from $HOME as the service reads it."""
    home = os.path.expanduser("~")
    parts = [p for p in home.split("/") if p and p != "."]
    return "/" + "/".join(parts + [".local", "state", "probe", "service"])


def _read_text(path):
    try:
        with open(path, "rb") as handle:
            return handle.read().decode("ascii", "replace").strip()
    except OSError:
        return ""


def _darwin_uuid():
    """The Mac's IOPlatformUUID, from `ioreg` (the service's fallback for it)."""
    try:
        read_end, write_end = os.pipe()
        pid = os.fork()
    except OSError:
        return None
    if pid == 0:
        try:
            null = os.open(os.devnull, os.O_RDWR)
            os.dup2(null, 0)
            os.dup2(write_end, 1)
            os.dup2(null, 2)
            os.execv("/usr/sbin/ioreg", ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"])
        finally:
            os._exit(127)
    os.close(write_end)
    chunks = []
    while True:
        chunk = os.read(read_end, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    os.close(read_end)
    os.waitpid(pid, 0)
    for line in b"".join(chunks).decode("utf-8", "replace").splitlines():
        if '"IOPlatformUUID"' in line and "=" in line:
            return line.split("=", 1)[1].strip().strip('"').upper() or None
    return None


def machine_id():
    """The OS's machine id (Linux machine-id, the Mac's hardware UUID), else the host name."""
    if sys.platform == "darwin":
        found = _darwin_uuid()
        if found:
            return found
    else:
        for name in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            found = _read_text(name)
            if found:
                return found
    return os.uname().nodename or "localhost"


def machine_dir(root):
    """This machine's service folder: `<root>/<sha256("probe-service\\0" + machine id)[:16]>`.
    On a Mac with one such folder holding a hook entry, that one (no `ioreg` per event)."""
    if sys.platform == "darwin":
        try:
            found = [n for n in os.listdir(root) if len(n) == 16 and os.path.isfile(os.path.join(root, n, "hook-entry.json"))]
        except OSError:
            found = []
        if len(found) == 1:
            return os.path.join(root, found[0])
    key = sha256_hex(b"probe-service\0" + machine_id().encode("utf-8", "replace"))[:16]
    return os.path.join(root, key)


def socket_in(folder):
    """`<folder>/service.sock`, or `/tmp/probe-<uid>/service-<hash>.sock` when too long."""
    path = os.path.join(folder, "service.sock")
    if len(os.fsencode(path)) <= MAX_SOCKET_PATH:
        return path
    return "/tmp/probe-%d/service-%s.sock" % (os.getuid(), sha256_hex(os.fsencode(folder))[:16])


# -- JSON, without `json` (an extension module: tens of ms per event) -----------

_ESCAPES = {0x22: '\\"', 0x5C: "\\\\"}
_ESCAPES.update((c, "\\u%04x" % c) for c in range(32))
_UNESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def quote(text):
    """A JSON string (a lone surrogate is escaped when the line is encoded)."""
    return '"' + text.translate(_ESCAPES) + '"'


def to_wire(text):
    return text.encode("utf-8", "backslashreplace")


def parse(text):
    """One JSON value from `text`. Raises ValueError (or IndexError) on anything else."""
    value, end = _value(text, _skip(text, 0))
    if _skip(text, end) != len(text):
        raise ValueError("trailing data")
    return value


def _skip(s, i):
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def _value(s, i):
    c = s[i]
    if c == "{":
        out = {}
        i = _skip(s, i + 1)
        if s[i] == "}":
            return out, i + 1
        while True:
            key, i = _string(s, _skip(s, i))
            i = _skip(s, i)
            if s[i] != ":":
                raise ValueError("expected :")
            out[key], i = _value(s, _skip(s, i + 1))
            i = _skip(s, i)
            if s[i] == "}":
                return out, i + 1
            if s[i] != ",":
                raise ValueError("expected ,")
            i += 1
    if c == "[":
        items = []
        i = _skip(s, i + 1)
        if s[i] == "]":
            return items, i + 1
        while True:
            item, i = _value(s, _skip(s, i))
            items.append(item)
            i = _skip(s, i)
            if s[i] == "]":
                return items, i + 1
            if s[i] != ",":
                raise ValueError("expected ,")
            i += 1
    if c == '"':
        return _string(s, i)
    for word, value in (("true", True), ("false", False), ("null", None)):
        if s.startswith(word, i):
            return value, i + len(word)
    j = i
    while j < len(s) and s[j] in "+-0123456789.eE":
        j += 1
    number = s[i:j]
    if not number:
        raise ValueError("unexpected character")
    return (float(number) if any(ch in number for ch in ".eE") else int(number)), j


def _string(s, i):
    if s[i] != '"':
        raise ValueError("expected a string")
    out = []
    i += 1
    while True:
        end, slash = s.find('"', i), s.find("\\", i)
        if end < 0:
            raise ValueError("unterminated string")
        if slash < 0 or end < slash:
            out.append(s[i:end])
            return "".join(out), end + 1
        out.append(s[i:slash])
        kind = s[slash + 1]
        if kind != "u":
            out.append(_UNESCAPES[kind])
            i = slash + 2
            continue
        code, i = int(s[slash + 2:slash + 6], 16), slash + 6
        if 0xD800 <= code < 0xDC00 and s.startswith("\\u", i):
            low = int(s[i + 2:i + 6], 16)
            if 0xDC00 <= low < 0xE000:
                code, i = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00), i + 6
        out.append(chr(code))


# -- the event --------------------------------------------------------------

def parse_args(argv):
    """`--harness`, `--event`, `--timeout`, `--wait` -> dict, or None when unusable."""
    found = {}
    for i in range(0, len(argv) - 1, 2):
        if argv[i] in ("--harness", "--event", "--timeout", "--wait"):
            found[argv[i][2:]] = argv[i + 1]
    harness, event = found.get("harness", ""), found.get("event", "")
    if not harness or not event or not event.isalpha() or not all(c.islower() or c.isdigit() or c == "_" for c in harness):
        return None
    try:
        found["timeout"] = float(found.get("timeout") or 5)
    except ValueError:
        found["timeout"] = 5.0
    return found


def budget_s(harness, event, timeout, environ):
    """The event's own limit: its entry's timeout, or the harness's shorter session-end limit."""
    if event == "SessionEnd" and harness == "claude_code":
        try:
            return min(timeout, float(environ.get("CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS") or "") / 1000.0)
        except ValueError:
            return min(timeout, CLAUDE_SESSION_END_S)
    if event == "SessionEnd" and harness == "codex":
        return min(timeout, 3.0)
    return timeout


def pick_env(environ):
    """The environment the service is handed: ENV_NAMES and ENV_PREFIXES, within its caps."""
    kept = {}
    for name in sorted(environ):
        value = environ[name]
        if (name in ENV_NAMES or name.startswith(ENV_PREFIXES)) and "=" not in name and len(name) <= 256 \
                and len(value) <= MAX_ENV_VALUE and "\0" not in value:
            kept[name] = value
            if len(kept) >= MAX_ENV:
                break
    return kept


def queue_env(env):
    """`env` as a queue file may hold it: no secret-looking name, no proxy with a user in it."""
    kept = {}
    for name, value in env.items():
        if name.upper().endswith("_PROXY") and "@" in value.split("://", 1)[-1].split("/", 1)[0]:
            continue
        if name != QUEUE_CREDENTIAL and any(word in name.upper() for word in QUEUE_SECRET_WORDS):
            continue
        kept[name] = value
    return kept


def env_json(env):
    return "{" + ",".join(quote(k) + ":" + quote(v) for k, v in env.items()) + "}"


def make_params(args, event_id, payload, env, cwd, timeout_ms):
    """`hooks.run`'s v1 params, as JSON text."""
    wait = ',"wait":' + quote(args["wait"]) if args.get("wait") else ""
    return ('{"routes":"service","v":1,"harness":%s,"event":%s,"event_id":"%s"%s,"payload":%s,"env":%s,'
            '"cwd":%s,"timeout_ms":%d}') % (quote(args["harness"]), quote(args["event"]), event_id, wait,
                                            quote(payload), env_json(env), quote(cwd), timeout_ms)


def ask_service(path, params, deadline):
    """`service.hello`, then `hooks.run`: the result object, or None (no answer, or an error)."""
    try:
        import _socket as sockets
    except ImportError:
        import socket as sockets
    hello = '{"jsonrpc":"2.0","id":0,"method":"service.hello","params":{"client":"hook","client_version":"1","api":1}}\n'
    run = '{"jsonrpc":"2.0","id":1,"method":"hooks.run","params":' + params + "}\n"
    sock = sockets.socket(sockets.AF_UNIX, sockets.SOCK_STREAM)
    try:
        sock.settimeout(max(deadline - time.monotonic(), 0.01))
        sock.connect(path)
        sock.sendall(to_wire(hello + run))
        data = b""
        while data.count(b"\n") < 2:
            sock.settimeout(max(deadline - time.monotonic(), 0.01))
            chunk = sock.recv(65536)
            if not chunk:
                return None
            data += chunk
    except OSError:
        return None
    finally:
        sock.close()
    try:
        reply = parse(data.split(b"\n")[1].decode("utf-8", "replace"))
    except (ValueError, IndexError, KeyError):
        return None
    result = reply.get("result") if isinstance(reply, dict) else None
    return result if isinstance(result, dict) else None


def is_taken(result):
    """Did the service take the event (an answer, not a refusal)?"""
    return isinstance(result, dict) and "refused" not in result


def run_runner(argv, params, deadline):
    """The CLI's local runner (D19): the params on stdin, a v1 answer on stdout. None: none came."""
    import subprocess

    try:
        done = subprocess.run(argv, input=to_wire(params + "\n"), stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=max(deadline - time.monotonic(), 0.01))
        answer = parse(done.stdout.decode("utf-8", "replace").strip())
    except (OSError, ValueError, IndexError, KeyError, subprocess.SubprocessError):
        return None
    return answer if isinstance(answer, dict) and "refused" not in answer else None


def write_queue(folder, event_id, args, payload, env, cwd):
    """The event as a v1 queue file for the next service (DESIGN §5), written whole."""
    item = ('{"v":1,"id":"%s","routes":"service","harness":%s,"event":%s,"payload":%s,"env":%s,"cwd":%s,'
            '"at":%r}') % (event_id, quote(args["harness"]), quote(args["event"]), quote(payload),
                           env_json(queue_env(env)), quote(cwd), time.time())
    temporary = os.path.join(folder, ".%s.%d.tmp" % (event_id, os.getpid()))
    try:
        os.makedirs(folder, 0o700, exist_ok=True)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            data = to_wire(item)
            while data:
                data = data[os.write(fd, data):]
        finally:
            os.close(fd)
        os.rename(temporary, os.path.join(folder, event_id + ".json"))
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _write_all(fd, text):
    if not isinstance(text, str) or not text:
        return
    try:
        data = text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        data = text.encode("utf-8", "replace")
    try:
        while data:
            data = data[os.write(fd, data):]
    except OSError:
        pass


def emit(answer):
    """Print an answer's stdout and stderr; its exit code (0 when it names none)."""
    _write_all(1, answer.get("stdout"))
    _write_all(2, answer.get("stderr"))
    code = answer.get("exit_code")
    return code if isinstance(code, int) and not isinstance(code, bool) and 0 <= code < 256 else 0


def main(argv, environ):
    started = time.monotonic()
    args = parse_args(argv)
    if args is None or os.name != "posix":
        return 0
    deadline = started + budget_s(args["harness"], args["event"], args["timeout"], environ) - MARGIN_S
    try:
        payload = sys.stdin.buffer.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        payload = ""
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = "/"
    env = pick_env(environ)
    folder = machine_dir(service_root())
    try:
        with open(os.path.join(folder, "hook-entry.json"), "rb") as handle:
            entry = parse(handle.read().decode("utf-8", "replace"))
    except (OSError, ValueError, IndexError, KeyError):
        entry = None
    entry = entry if isinstance(entry, dict) else {}
    path = entry.get("socket") if isinstance(entry.get("socket"), str) else socket_in(folder)
    while True:
        event_id = os.urandom(16).hex()
        left_ms = int((deadline - time.monotonic()) * 1000)
        params = make_params(args, event_id, payload, env, cwd, max(1, min(left_ms, MAX_WAIT_MS)))
        answer = ask_service(path, params, deadline) if left_ms > 0 else None
        # A wait (`--wait reads`) asks again while the service holds it and says
        # `timed_out`, and after a lost connection (its update restart); a refusal ends it.
        if args.get("wait") != "reads" or time.monotonic() >= deadline:
            break
        if answer is not None and not (is_taken(answer) and answer.get("timed_out")):
            break
        if answer is None:
            time.sleep(min(2.0, max(deadline - time.monotonic(), 0.0)))
    if is_taken(answer):
        return emit(answer)
    if args.get("wait") == "reads":
        return 0
    runner = entry.get("runner")
    if isinstance(runner, list) and runner and all(isinstance(part, str) for part in runner) \
            and deadline - time.monotonic() >= RUNNER_MIN_S:
        answer = run_runner(runner, params, deadline)
        if answer is not None:
            return emit(answer)
    if args["event"] not in NO_QUEUE_EVENTS:
        queue = entry.get("queue") if isinstance(entry.get("queue"), str) else os.path.join(folder, "hooks-queue")
        write_queue(queue, event_id, args, payload, env, cwd)
    return 0


if __name__ == "__main__":
    try:
        code = main(sys.argv[1:], os.environ)
    except Exception:  # noqa: BLE001 -- a broken shim never blocks the agent
        code = 0
    sys.exit(code)
