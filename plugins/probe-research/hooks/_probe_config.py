"""`.probe.config`: paths Probe never records, in gitignore syntax.

A `.probe.config` in any folder lists paths under that folder that Probe must
not record: no run opened there, no artifact or note about them, no daemon
event. It beats the session's on / read / off switch, and it never blocks a
read. The rules are git's (`gitignore(5)`), applied the way git applies them:

- a file covers its own folder and everything below it; patterns are relative
  to that folder; a folder with no file inherits its parents' rules;
- a later line beats an earlier one, a deeper file beats a shallower one;
- `!pattern` re-includes, except under a folder that is itself excluded;
- `/` anchors, a trailing `/` matches folders only, `**` spans folders.

One deliberate difference from git: files ABOVE a repository root count too,
so `~/.probe.config` is the machine-wide list and `~/code/.probe.config` with
`/*` then `!/repo-a/` tracks only repo-a.

A file ABOVE the folder an agent session started in is the researcher's to set
aside for that session (`probe session parent ignore`); until they answer it is
followed. Files at or below the launch folder always apply.

THIS MODULE IS STDLIB-ONLY AND IMPORTS NOTHING FROM `probe`. It is copied
verbatim beside the plugin hooks (`_probe_config.py`) and into the tap
(`tap/probe_config.py`), which run under the system python with no package on
their path (`make sync-probe-config`; `tests/test_probe_config_sync.py` fails
when a copy drifts). Matching never uses a backtracking regex: a segment glob
is a dynamic-programming match, so no pattern can make a check slow.

FAILS CLOSED. A `.probe.config` that exists but cannot be read (permissions, a
FIFO, a directory, over `MAX_BYTES`, undecodable) excludes everything under its
folder, with a warning: a privacy list that silently stops applying is worse
than one that over-applies. A pattern git would never match is skipped, as git
skips it.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

CONFIG_NAME = ".probe.config"
#: The retired folder default (`<folder>/.probe/config.json`, #1252). An old one
#: set to `off` or `read` still counts as `*` here until `probe doctor --fix`
#: converts it, so retiring the feature never starts recording anywhere.
LEGACY_DIRNAME = ".probe"
LEGACY_FILENAME = "config.json"
#: Over this, a `.probe.config` is unreadable (fails closed).
MAX_BYTES = 1024 * 1024
#: The parent-config answer beside a session's state file.
PARENT_SUFFIX = ".parent-config"
#: Where a session started, beside its state file (written once, at seed).
LAUNCH_SUFFIX = ".launch-dir"

FOLLOW = "follow"
IGNORE = "ignore"
#: A parent file reaches this session and nobody has asked yet.
UNASKED = "unasked"
#: Asked, not answered: followed.
PENDING = "pending"

#: Case-insensitive file systems by default (macOS, Windows): match without case,
#: which excludes more, never less.
_FOLD_CASE = sys.platform in ("darwin", "win32")

#: Skipped by the session-start scan below the launch folder (never by a check).
SCAN_SKIP_DIRS = frozenset(
    {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache",
     ".ruff_cache", ".tox", ".cache", "site-packages", "wandb", ".hg", ".svn"}
)


# What the SDK and CLI print (agent-facing refusals live in session_marker).
INIT_EXCLUDED = "probe: `{path}` is excluded by `{source}`, so no run was opened. The script runs as usual."
ARTIFACT_EXCLUDED = "probe: `{path}` is excluded by `{source}`, so it was not uploaded."
FOLDER_DEFAULT_RETIRED = (
    "Folder defaults are retired. List paths Probe should never record in a `.probe.config` "
    "(gitignore syntax); `probe ignore check PATH` tests it."
)
DOCTOR_OLD_DEFAULT = (
    "`{path}` is a retired folder default (`{state}`); it still blocks recording, as `*`. "
    "`probe doctor --fix` turns it into `{dir}/.probe.config`."
)
DOCTOR_OLD_DEFAULT_ON = (
    "`{path}` is a retired folder default (`on`) and does nothing now; `probe doctor --fix` "
    "deletes it. To track only some folders: machine default `on`, and `/*` then `!/<folder>/` "
    "in a parent's `.probe.config`."
)


def local_path_of(uri: str | None) -> str | None:
    """The local path a `file://` uri names (`file:///a%20b`, `file://localhost/x`),
    else None."""
    if not isinstance(uri, str) or not uri.lower().startswith("file:"):
        return None
    from urllib.parse import unquote, urlparse

    parsed = urlparse(uri)
    if parsed.netloc not in ("", "localhost"):
        return None
    return unquote(parsed.path) or None


def shown(path: str | os.PathLike) -> str:
    """A path as a message names it (`~` for home)."""
    return _display(os.fspath(path))


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

#: Segment tokens: ("lit", ch) | ("one",) | ("star",) | ("class", negated, items)
#: where items are (lo, hi) code-point ranges or ("cls", name).
_POSIX_CLASSES = {
    "alnum": str.isalnum,
    "alpha": str.isalpha,
    "blank": lambda c: c in " \t",
    "cntrl": lambda c: ord(c) < 32 or ord(c) == 127,
    "digit": lambda c: "0" <= c <= "9",
    "graph": lambda c: c.isprintable() and not c.isspace(),
    "lower": str.islower,
    "print": str.isprintable,
    "punct": lambda c: c.isprintable() and not c.isalnum() and not c.isspace(),
    "space": str.isspace,
    "upper": str.isupper,
    "xdigit": lambda c: c in "0123456789abcdefABCDEF",
}


class _BadPattern(ValueError):
    pass


def _tokenize_segment(seg: str) -> tuple:
    """One `/`-free pattern segment as tokens. Raises `_BadPattern` for what git's
    wildmatch can never match (an unterminated `[`, a trailing lone `\\`)."""
    out: list = []
    i, n = 0, len(seg)
    while i < n:
        c = seg[i]
        if c == "\\":
            if i + 1 >= n:
                raise _BadPattern("trailing backslash")
            out.append(("lit", seg[i + 1]))
            i += 2
        elif c == "*":
            if not out or out[-1] != ("star",):
                out.append(("star",))
            i += 1
        elif c == "?":
            out.append(("one",))
            i += 1
        elif c == "[":
            token, i = _bracket(seg, i)
            out.append(token)
        else:
            out.append(("lit", c))
            i += 1
    return tuple(out)


def _bracket(seg: str, i: int) -> tuple[tuple, int]:
    """Parse `[...]` starting at seg[i] == '['. Returns (token, next index)."""
    j = i + 1
    n = len(seg)
    negated = False
    if j < n and seg[j] in "!^":
        negated = True
        j += 1
    items: list = []
    first = True
    while True:
        if j >= n:
            raise _BadPattern("unterminated [")
        c = seg[j]
        if c == "]" and not first:
            return ("class", negated, tuple(items)), j + 1
        first = False
        if c == "[" and j + 1 < n and seg[j + 1] == ":":
            end = seg.find(":]", j + 2)
            if end < 0:
                raise _BadPattern("unterminated [:")
            name = seg[j + 2 : end]
            if name not in _POSIX_CLASSES:
                raise _BadPattern(f"unknown class [:{name}:]")
            items.append(("cls", name))
            j = end + 2
            continue
        if c == "\\":
            j += 1
            if j >= n:
                raise _BadPattern("unterminated [")
            c = seg[j]
        lo = c
        j += 1
        if j + 1 < n and seg[j] == "-" and seg[j + 1] != "]":
            hi = seg[j + 1]
            if hi == "\\":
                if j + 2 >= n:
                    raise _BadPattern("unterminated [")
                hi = seg[j + 2]
                j += 1
            j += 2
            items.append((lo, hi))
        else:
            items.append((lo, lo))


def _class_has(items: tuple, ch: str) -> bool:
    for item in items:
        if item[0] == "cls":
            if _POSIX_CLASSES[item[1]](ch):
                return True
        elif item[0] <= ch <= item[1]:
            return True
    return False


def _token_eq(token: tuple, ch: str) -> bool:
    kind = token[0]
    if kind == "lit":
        if _FOLD_CASE:
            return token[1].lower() == ch.lower()
        return token[1] == ch
    if kind == "one":
        return ch != "/"
    # class
    if ch == "/":
        return False
    hit = _class_has(token[2], ch) or (_FOLD_CASE and (_class_has(token[2], ch.lower()) or _class_has(token[2], ch.upper())))
    return hit != token[1]


def _match_segment(tokens: tuple, name: str) -> bool:
    """Glob-match one path segment. O(len(tokens) * len(name)), no backtracking."""
    n = len(name)
    # prev[j]: tokens[:i] match name[:j]
    prev = [False] * (n + 1)
    prev[0] = True
    for token in tokens:
        cur = [False] * (n + 1)
        if token == ("star",):
            seen = False
            for j in range(n + 1):
                seen = seen or prev[j]
                cur[j] = seen
        else:
            for j in range(1, n + 1):
                cur[j] = prev[j - 1] and _token_eq(token, name[j - 1])
        prev = cur
        if not any(prev):
            return False
    return prev[n]


#: A segment that is exactly `**`.
_GLOBSTAR = ("globstar",)


@dataclass(frozen=True)
class Rule:
    """One pattern line of one `.probe.config`."""

    base: str  # the folder holding the file
    source: str  # the file (or a synthetic description)
    line: int
    pattern: str  # the line as written (trimmed)
    negated: bool
    dir_only: bool
    anchored: bool
    segments: tuple  # tuple of token-tuples or _GLOBSTAR
    #: From a retired folder default (`.probe/config.json` off/read): it counts
    #: only inside an agent session (`legacy=True`), the only place it ever did.
    legacy: bool = False

    def matches(self, rel: tuple[str, ...], is_dir: bool) -> bool:
        """Does this rule match the path `rel` (segments relative to `base`)?"""
        if self.dir_only and not is_dir:
            return False
        if not rel:
            return False
        if not self.anchored:
            return _match_segment(self.segments[0], rel[-1])
        return _match_segments(self.segments, rel)

    def could_reach(self, rel: tuple[str, ...]) -> bool:
        """Could this rule match `rel` (as a folder), one of its ancestors below
        `base`, or anything inside it? From the pattern alone, no disk."""
        if self.negated:
            return False
        if not self.anchored:
            return True
        for k in range(1, len(rel) + 1):
            if _match_segments(self.segments, rel[:k]):
                return True
        return _extends(self.segments, rel)

    @property
    def where(self) -> str:
        return f"{self.source}:{self.line} ({self.pattern})" if self.line else f"{self.source} ({self.pattern})"


def _match_segments(pat: tuple, path: tuple[str, ...]) -> bool:
    """Match pattern segments against path segments; `**` spans folders.

    A trailing `**` needs at least one segment (`abc/**` is what is INSIDE abc);
    a leading or middle `**` may span none."""
    m, n = len(pat), len(path)
    # dp[i][j]: pat[i:] matches path[j:]
    dp = [[False] * (n + 1) for _ in range(m + 1)]
    dp[m][n] = True
    for i in range(m - 1, -1, -1):
        seg = pat[i]
        last = i == m - 1
        for j in range(n, -1, -1):
            if seg is _GLOBSTAR or seg == _GLOBSTAR:
                if last:
                    dp[i][j] = j < n  # one or more remaining segments
                else:
                    # zero segments, or eat one and stay
                    dp[i][j] = dp[i + 1][j] or (j < n and dp[i][j + 1])
            else:
                dp[i][j] = j < n and _match_segment(seg, path[j]) and dp[i + 1][j + 1]
    return dp[0][0]


def _extends(pat: tuple, path: tuple[str, ...]) -> bool:
    """Can `pat` match some path that STARTS with all of `path` plus at least one
    more segment? (The rule reaches inside the folder `path`.)"""
    m, n = len(pat), len(path)
    # reach[i][j]: pat[:i] consumed path[:j]
    reach = [[False] * (n + 1) for _ in range(m + 1)]
    reach[0][0] = True
    for i in range(m):
        seg = pat[i]
        for j in range(n + 1):
            if not reach[i][j]:
                continue
            if seg is _GLOBSTAR or seg == _GLOBSTAR:
                # A globstar can swallow the rest of `path` and still match more.
                return True
            if j < n and _match_segment(seg, path[j]):
                reach[i + 1][j + 1] = True
    # All of `path` consumed with pattern left over: the rest can match below.
    return any(reach[i][n] for i in range(m))


def parse_line(raw: str, base: str, source: str, line: int) -> Rule | None:
    """One `.probe.config` line as a Rule, None for a blank or comment line.
    Raises `_BadPattern` for a line git would never match."""
    text = raw.rstrip("\r\n")
    if not text or text.startswith("#"):
        return None
    text = _trim_trailing_spaces(text)
    if not text:
        return None
    negated = False
    if text.startswith("!"):
        negated = True
        text = text[1:]
    elif text.startswith("\\!") or text.startswith("\\#"):
        text = text[1:]
    if not text:
        return None
    dir_only = text.endswith("/") and not text.endswith("\\/")
    body = text[:-1] if dir_only else text
    if not body:
        return None
    anchored = "/" in body
    if body.startswith("/"):
        body = body[1:]
    if not body:
        return None
    parts = body.split("/")
    segments: list = []
    for part in parts:
        if part == "**":
            if segments and segments[-1] == _GLOBSTAR:
                continue
            segments.append(_GLOBSTAR)
        elif part == "":
            # `a//b`: git collapses nothing here; an empty segment never matches.
            raise _BadPattern("empty path segment")
        else:
            segments.append(_tokenize_segment(part))
    if not anchored and segments == [_GLOBSTAR]:
        # A lone `**` with no slash is a basename `*`.
        segments = [(("star",),)]
    return Rule(
        base=base,
        source=source,
        line=line,
        pattern=raw.rstrip("\r\n").strip(),
        negated=negated,
        dir_only=dir_only,
        anchored=anchored,
        segments=tuple(segments),
    )


def _trim_trailing_spaces(text: str) -> str:
    """Trailing spaces go unless escaped with a backslash (`gitignore(5)`)."""
    end = len(text)
    while end > 0 and text[end - 1] == " ":
        backslashes = 0
        k = end - 2
        while k >= 0 and text[k] == "\\":
            backslashes += 1
            k -= 1
        if backslashes % 2 == 1:
            break
        end -= 1
    return text[:end]


def _everything(base: str, source: str, why: str, *, legacy: bool = False) -> Rule:
    """A synthetic rule excluding everything under `base` (fail closed)."""
    return Rule(
        base=base, source=source, line=0, pattern=why, negated=False, dir_only=False,
        anchored=False, segments=((("star",),),), legacy=legacy,
    )


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


@dataclass
class _Loaded:
    key: tuple
    rules: tuple
    warnings: tuple
    digest: str


_CACHE: dict[str, _Loaded] = {}
_CACHE_MAX = 4096
_WARNED: set[str] = set()
#: When each folder was last stat'ed (monotonic), for callers that accept a
#: slightly stale answer on a hot path (`ttl`): the SDK's capture asks on every
#: file it walks and every read it records.
_CHECKED: dict[str, float] = {}
#: One loader at a time: a reader in another thread must never see a folder
#: stamped as checked before its answer is cached (threaded data loaders).
_LOCK = threading.RLock()


def _after_fork_in_child() -> None:
    global _LOCK
    _LOCK = threading.RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


def _warn(message: str) -> None:
    """Once per message per process, to stderr. Never raises."""
    if message in _WARNED:
        return
    _WARNED.add(message)
    try:
        print(f"probe: {message}", file=sys.stderr)
    except Exception:  # noqa: BLE001 - a warning never breaks a check
        pass


#: stat errors that mean "there is no such file here" (absent), as opposed to
#: one that exists and cannot be read (fails closed): a path too long to hold
#: one, a component that is a file, a name the file system refuses.
_ABSENT_ERRNOS = frozenset(
    e for e in (errno.ENOENT, errno.ENOTDIR, errno.ENAMETOOLONG, errno.EINVAL, getattr(errno, "EILSEQ", None))
    if e is not None
)


def _stat_key(path: str) -> tuple | None:
    """None when no file is there (one `lstat`: the common case costs one
    syscall); a key that changes with the file otherwise. A dangling symlink
    (dotfiles on an unmounted share) is a file that cannot be read: it fails
    closed, never reads as absent."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        return None if exc.errno in _ABSENT_ERRNOS else ("error", type(exc).__name__)
    except ValueError:  # an embedded NUL: no such file
        return None
    if stat.S_ISLNK(st.st_mode):
        try:
            st = os.stat(path)
        except OSError:
            return ("dangling",)
    return (st.st_mode, st.st_ino, st.st_size, st.st_mtime_ns)


def _read_text(path: str) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        if info.st_size > MAX_BYTES:
            raise OSError(f"over {MAX_BYTES} bytes")
        raw = b""
        while len(raw) <= MAX_BYTES:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                break
            raw += chunk
        if len(raw) > MAX_BYTES:
            raise OSError(f"over {MAX_BYTES} bytes")
    finally:
        os.close(fd)
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    if b"\x00" in raw:
        raise OSError("holds NUL bytes (UTF-16 without a BOM?)")
    return raw.decode("utf-8-sig")


def legacy_default(folder: str) -> str | None:
    """The retired folder default in `<folder>/.probe/config.json`, as the stored
    word (`full` | `read-only` | `off`), or None when there is none or it is not
    one. Never raises."""
    path = os.path.join(folder, LEGACY_DIRNAME, LEGACY_FILENAME)
    try:
        if not os.path.isfile(path) or os.path.getsize(path) > 64 * 1024:
            return None
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    defaults = data.get("defaults") if isinstance(data, dict) else None
    if not isinstance(defaults, dict):
        return None
    for key in ("session_state", "session_tracking"):
        value = defaults.get(key)
        if value is True:
            return "full"
        if value is False:
            return "off"
        if isinstance(value, str):
            word = value.strip().lower()
            if word in ("full", "on", "daemon"):
                return "full"
            if word in ("read-only", "read", "readonly"):
                return "read-only"
            if word == "off":
                return "off"
    return None


def _load(folder: str, ttl: float = 0.0) -> _Loaded | None:
    """The rules a folder contributes, cached by the file's stat. None when the
    folder holds neither a `.probe.config` nor a retired folder default.

    `ttl` > 0 reuses the last answer for a folder stat'ed less than `ttl`
    seconds ago, without touching the disk."""
    return _load_unlocked(folder, ttl)


def _load_unlocked(folder: str, ttl: float) -> _Loaded | None:
    if ttl > 0:
        last = _CHECKED.get(folder)
        if last is not None and time.monotonic() - last < ttl:
            return _CACHE.get(folder)
    checked_at = time.monotonic()
    path = os.path.join(folder, CONFIG_NAME)
    legacy_path = os.path.join(folder, LEGACY_DIRNAME, LEGACY_FILENAME)
    key = (_stat_key(path), _stat_key(legacy_path))
    if key == (None, None):
        with _LOCK:
            _CACHE.pop(folder, None)
            _stamp(folder, checked_at)
        return None
    hit = _CACHE.get(folder)
    if hit is not None and hit.key == key:
        _stamp(folder, checked_at)
        return hit
    rules: list[Rule] = []
    warnings: list[str] = []
    digest = hashlib.sha256()
    if key[1] is not None:
        legacy = legacy_default(folder)
        if legacy in ("off", "read-only"):
            word = "off" if legacy == "off" else "read"
            rules.append(_everything(folder, legacy_path, f"retired folder default `{word}`", legacy=True))
            digest.update(f"legacy:{legacy}".encode())
    if key[0] is not None:
        try:
            text = _read_text(path)
        except (OSError, UnicodeDecodeError) as exc:
            reason = str(exc) or type(exc).__name__
            warnings.append(
                f"`{path}` could not be read ({reason}), so everything under `{folder}` counts as excluded."
            )
            rules.append(_everything(folder, path, "unreadable"))
            digest.update(b"unreadable")
        else:
            digest.update(text.encode("utf-8", "surrogatepass"))
            for number, raw in enumerate(text.splitlines(), start=1):
                try:
                    rule = parse_line(raw, folder, path, number)
                except _BadPattern:
                    warnings.append(f"`{path}:{number}` is not a valid pattern and was skipped.")
                    continue
                if rule is not None:
                    rules.append(rule)
    loaded = _Loaded(key=key, rules=tuple(rules), warnings=tuple(warnings), digest=digest.hexdigest())
    with _LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
            _CHECKED.clear()
        _CACHE[folder] = loaded
        _stamp(folder, checked_at)
    return loaded


def _stamp(folder: str, at: float) -> None:
    """Mark a folder's answer fresh, AFTER it is cached."""
    if len(_CHECKED) >= _CACHE_MAX:
        _CHECKED.clear()
    _CHECKED[folder] = at


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    excluded: bool
    rule: Rule | None = None
    #: The path the verdict was reached on (an excluded parent folder, or the path).
    at: str = ""

    @property
    def source(self) -> str:
        return self.rule.where if self.rule is not None else ""


NOT_EXCLUDED = Verdict(False)


def _norm(path: str | os.PathLike, cwd: str | None = None) -> str:
    raw = os.fspath(path)
    if raw.startswith("~"):
        raw = os.path.expanduser(raw)
    if not os.path.isabs(raw):
        raw = os.path.join(cwd or os.getcwd(), raw)
    return os.path.normpath(raw)


def _parts(path: str) -> list[str]:
    """`/a/b/c` -> ['/', '/a', '/a/b', '/a/b/c']."""
    out = []
    current = path
    while True:
        out.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    out.reverse()
    return out


def _is_strict_ancestor(folder: str, of: str) -> bool:
    if folder == of:
        return False
    prefix = folder if folder.endswith(os.sep) else folder + os.sep
    return of.startswith(prefix)


def _rel(path: str, base: str) -> tuple[str, ...]:
    rel = os.path.relpath(path, base)
    return tuple(p for p in rel.split(os.sep) if p and p != ".")


def _decide(target: str, is_dir: bool, layers: list[tuple[str, tuple]]) -> Rule | None:
    """Last match wins across `layers` (shallow -> deep); the winning rule, or None.
    A winning negated rule returns as-is (the caller reads `negated`)."""
    for base, rules in reversed(layers):
        rel = _rel(target, base)
        if not rel:
            continue
        for rule in reversed(rules):
            if rule.matches(rel, is_dir):
                return rule
    return None


def _check_one(path: str, is_dir: bool, skip: frozenset, legacy: bool, ttl: float = 0.0) -> Verdict:
    chain = _parts(path)
    layers: list[tuple[str, tuple]] = []
    for index, folder in enumerate(chain):
        last = index == len(chain) - 1
        if index > 0:
            # Is THIS path (a parent folder, or the path itself) excluded by the
            # files above it? An excluded folder ends the walk: nothing under it
            # can be re-included (git's rule).
            rule = _decide(folder, True if not last else is_dir, layers)
            if rule is not None and not rule.negated:
                return Verdict(True, rule, folder)
        if last:
            break
        loaded = None if folder in skip else _load(folder, ttl)
        if loaded is not None:
            for message in loaded.warnings:
                _warn(message)
            rules = loaded.rules if legacy else tuple(r for r in loaded.rules if not r.legacy)
            if rules:
                layers.append((folder, rules))
    return NOT_EXCLUDED


def check(
    path: str | os.PathLike,
    *,
    is_dir: bool | None = None,
    cwd: str | None = None,
    skip: "frozenset[str] | set[str] | None" = None,
    legacy: bool = False,
    ttl: float = 0.0,
    resolve: bool = True,
) -> Verdict:
    """Is `path` excluded by a `.probe.config`?

    `is_dir` None asks the disk (a missing path counts as a file). A path that
    goes through a symlink is checked as written AND as resolved (resolved
    BEFORE `..` collapses, so `link/../x` is the file the OS would open):
    excluded if either is. `skip`: folders whose files are set aside (the files
    above a session's launch folder the researcher answered `ignore` for, and
    only those). `legacy`: retired folder defaults count (agent sessions only).
    `resolve=False`: the caller already checks the resolved path itself (the
    SDK's write capture, which resolves every path once). Never raises."""
    try:
        raw = os.fspath(path)
        if raw.startswith("~"):
            raw = os.path.expanduser(raw)
        if not os.path.isabs(raw):
            raw = os.path.join(cwd or os.getcwd(), raw)
        full = os.path.normpath(raw)
        if is_dir is None:
            is_dir = os.path.isdir(full)
        skipped = frozenset(skip or ())
        verdict = _check_one(full, is_dir, skipped, legacy, ttl)
        if verdict.excluded or not resolve:
            return verdict
        real = _real(raw, ttl)
        if real != full:
            return _check_one(real, is_dir, skipped, legacy, ttl)
        return verdict
    except Exception:  # noqa: BLE001 - a check never breaks the caller
        return NOT_EXCLUDED


_REAL_DIRS: dict[str, tuple[float, str]] = {}


def _real(full: str, ttl: float) -> str:
    """`os.path.realpath(full)`; on a hot path (`ttl` > 0) the folder's real path
    comes from a per-folder cache and only the last component is checked."""
    if ttl <= 0 or ".." in full.split(os.sep):
        return os.path.realpath(full)
    folder, name = os.path.split(full)
    now = time.monotonic()
    hit = _REAL_DIRS.get(folder)
    if hit is None or now - hit[0] >= ttl:
        if len(_REAL_DIRS) >= _CACHE_MAX:
            _REAL_DIRS.clear()
        hit = (now, os.path.realpath(folder))
        _REAL_DIRS[folder] = hit
    joined = os.path.join(hit[1], name)
    return os.path.realpath(joined) if os.path.islink(joined) else joined


def _concrete(tokens: tuple) -> str | None:
    """One name a segment glob matches (`*` and `?` as `x`), or None."""
    out = []
    for token in tokens:
        kind = token[0]
        if kind == "lit":
            out.append(token[1])
        elif kind in ("one", "star"):
            out.append("x")
        else:
            for ch in "xya0_Z9":
                if _token_eq(token, ch):
                    out.append(ch)
                    break
            else:
                for item in token[2]:
                    if item[0] != "cls" and _token_eq(token, item[0]):
                        out.append(item[0])
                        break
                else:
                    return None
    name = "".join(out)
    return name if name and "/" not in name else None


def _witness(rule: Rule, rel_full: tuple[str, ...]) -> list[str] | None:
    """Segments, relative to `full`, of one path INSIDE it that `rule`'s pattern
    matches, or None when the pattern can only match `full`'s ancestors or
    places beside it."""
    if not rule.anchored:
        name = _concrete(rule.segments[0])
        return [name] if name else None
    segs = rule.segments
    k = len(rel_full)

    def rec(i: int, j: int) -> list[str] | None:
        if j == k:
            rest: list[str] = []
            for t in range(i, len(segs)):
                seg = segs[t]
                if seg == _GLOBSTAR:
                    if t == len(segs) - 1:
                        rest.append("x")
                    continue
                name = _concrete(seg)
                if name is None:
                    return None
                rest.append(name)
            return rest or ["x"]  # the pattern names `full` itself: try an entry in it
        if i == len(segs):
            return None
        seg = segs[i]
        if seg == _GLOBSTAR:
            return rec(i + 1, j) or rec(i, j + 1)
        return rec(i + 1, j + 1) if _match_segment(seg, rel_full[j]) else None

    return rec(0, 0)


def _reincluded_inside(full: str, skip: frozenset, legacy: bool, ttl: float) -> bool:
    """Is anything inside `full` actually re-included by a `!` rule? For each `!`
    rule that applies (the folder's own file or one above), build one path
    inside `full` it matches and run the whole check on it: only a path that
    comes back NOT excluded proves it (order, depth and excluded parents all
    count, as for any path)."""
    for folder in _parts(full):
        if folder in skip:
            continue
        loaded = _load(folder, ttl)
        if loaded is None:
            continue
        rel = _rel(full, folder)
        for rule in loaded.rules:
            if not rule.negated or (rule.legacy and not legacy):
                continue
            inside = _witness(rule, rel)
            if not inside:
                continue
            path = os.path.join(full, *inside)
            as_dir = rule.dir_only
            if not _check_one(path, as_dir, skip, legacy, ttl).excluded:
                if not as_dir or not _check_one(os.path.join(path, "x"), False, skip, legacy, ttl).excluded:
                    return True
    return False


def check_cwd(folder: str | os.PathLike, **kw) -> Verdict:
    """Is work run IN `folder` excluded? The folder itself is excluded, or every
    entry in it would be (its own `*`, or a parent's `repo-b/**`)."""
    verdict = check(folder, is_dir=True, **kw)
    if verdict.excluded:
        return verdict
    try:
        full = _norm(folder, kw.get("cwd"))
        skipped = frozenset(kw.get("skip") or ())
        legacy = bool(kw.get("legacy", False))
        ttl = float(kw.get("ttl", 0.0))
        probe_names = ("\u2063" * 8, "\u2063" * 9 + ".x")
        first = None
        for name in probe_names:
            for as_dir in (False, True):
                child = _check_one(os.path.join(full, name), as_dir, skipped, legacy, ttl)
                if not child.excluded:
                    return NOT_EXCLUDED
                first = first or child
        if first is None or _reincluded_inside(full, skipped, legacy, ttl):
            return NOT_EXCLUDED
        return Verdict(True, first.rule, full)
    except Exception:  # noqa: BLE001
        return NOT_EXCLUDED


# ---------------------------------------------------------------------------
# Files above the launch folder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParentFile:
    path: str  # the `.probe.config` (or retired default) path
    folder: str
    digest: str


def parents_reaching(launch_dir: str | os.PathLike) -> list[ParentFile]:
    """The files strictly above `launch_dir` whose rules can exclude the launch
    folder or anything inside it. Shallow first. Never raises."""
    try:
        launch = _norm(launch_dir)
    except Exception:  # noqa: BLE001
        return []
    out: list[ParentFile] = []
    for folder in _parts(launch)[:-1]:
        try:
            loaded = _load(folder)
        except Exception:  # noqa: BLE001
            continue
        if loaded is None or not loaded.rules:
            continue
        rel = _rel(launch, folder)
        if any(rule.could_reach(rel) for rule in loaded.rules):
            name = os.path.join(folder, CONFIG_NAME)
            if not os.path.lexists(name):
                name = os.path.join(folder, LEGACY_DIRNAME, LEGACY_FILENAME)
            out.append(ParentFile(path=name, folder=folder, digest=loaded.digest))
    return out


def parents_key(parents: Iterable[ParentFile]) -> dict[str, str]:
    return {p.path: p.digest for p in parents}


# ---------------------------------------------------------------------------
# A session's view: where it started, and the researcher's answer
# ---------------------------------------------------------------------------


def _session_file(sessions_dir: str | os.PathLike, session_id: str, suffix: str) -> Path:
    return Path(sessions_dir) / (session_id + suffix)


def _publish(path: Path, payload: str) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def launch_dir(sessions_dir: str | os.PathLike, session_id: str) -> str | None:
    try:
        text = _session_file(sessions_dir, session_id, LAUNCH_SUFFIX).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    return text if text and os.path.isabs(text) else None


def record_launch_dir(sessions_dir: str | os.PathLike, session_id: str, cwd: str) -> bool:
    """Write where a session started, once. A resume keeps the first."""
    if not cwd or not session_id:
        return False
    path = _session_file(sessions_dir, session_id, LAUNCH_SUFFIX)
    if path.exists():
        return False
    try:
        folder = _norm(cwd)
    except Exception:  # noqa: BLE001
        return False
    return _publish(path, folder + "\n")


def _read_answer(sessions_dir: str | os.PathLike, session_id: str) -> dict:
    try:
        data = json.loads(_session_file(sessions_dir, session_id, PARENT_SUFFIX).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def parent_status(sessions_dir: str | os.PathLike, session_id: str) -> tuple[str, list[ParentFile]]:
    """(`UNASKED` | `PENDING` | `FOLLOW` | `IGNORE` | "", files) for a session.

    "" when no file above the launch folder reaches it (nothing to ask). An
    answer given for other files (one changed or appeared since) is void:
    `UNASKED` again."""
    start = launch_dir(sessions_dir, session_id)
    if not start:
        return "", []
    parents = parents_reaching(start)
    if not parents:
        return "", []
    data = _read_answer(sessions_dir, session_id)
    if data.get("files") != parents_key(parents):
        return UNASKED, parents
    decision = data.get("decision")
    if decision in (FOLLOW, IGNORE):
        return decision, parents
    return PENDING, parents


def mark_asked(sessions_dir: str | os.PathLike, session_id: str, parents: list[ParentFile]) -> bool:
    """Record that the question went out for exactly these files."""
    return _publish(
        _session_file(sessions_dir, session_id, PARENT_SUFFIX),
        json.dumps({"files": parents_key(parents), "asked_at": time.time()}),
    )


def record_answer(sessions_dir: str | os.PathLike, session_id: str, decision: str) -> tuple[bool, list[ParentFile]]:
    """Store the researcher's answer for the files that reach this session now."""
    if decision not in (FOLLOW, IGNORE):
        raise ValueError(decision)
    start = launch_dir(sessions_dir, session_id)
    parents = parents_reaching(start) if start else []
    ok = _publish(
        _session_file(sessions_dir, session_id, PARENT_SUFFIX),
        json.dumps({"files": parents_key(parents), "decision": decision, "answered_at": time.time()}),
    )
    return ok, parents


def session_view(sessions_dir: str | os.PathLike | None, session_id: str | None) -> tuple[str | None, frozenset]:
    """(launch folder, folders set aside) for a session: the folders of the
    files above its launch folder the researcher answered `ignore` for, while
    exactly those files still reach it."""
    if sessions_dir is None or not session_id:
        return None, frozenset()
    start = launch_dir(sessions_dir, session_id)
    if not start:
        return None, frozenset()
    status, parents = parent_status(sessions_dir, session_id)
    if status != IGNORE:
        return start, frozenset()
    folders = {p.folder for p in parents}
    # A path checked through its real location (`/home` a symlink on a cluster)
    # must find the same folders set aside.
    for folder in list(folders):
        try:
            folders.add(os.path.realpath(folder))
        except (OSError, ValueError):
            pass
    return start, frozenset(folders)


def session_check(
    path: str | os.PathLike,
    sessions_dir: str | os.PathLike | None,
    session_id: str | None,
    *,
    is_dir: bool | None = None,
    cwd: str | None = None,
    as_cwd: bool = False,
) -> Verdict:
    """`check` as this session sees it. Inside an agent session the retired
    folder defaults count and an `ignore` answer sets aside the files it was
    about; with no session, every file applies and retired defaults do not."""
    _start, skip = session_view(sessions_dir, session_id)
    legacy = bool(session_id)
    if as_cwd:
        return check_cwd(path, cwd=cwd, skip=skip, legacy=legacy)
    return check(path, is_dir=is_dir, cwd=cwd, skip=skip, legacy=legacy)


# ---------------------------------------------------------------------------
# What a session is told at start
# ---------------------------------------------------------------------------


@dataclass
class Summary:
    entries: list[str] = field(default_factory=list)
    truncated: bool = False


def _display(path: str) -> str:
    home = os.path.expanduser("~")
    if home and home != "/" and (path == home or path.startswith(home + os.sep)):
        return "~" + path[len(home):]
    return path


def describe_rule(rule: Rule) -> str:
    """A rule as a place: `~/code/repo-a/data/`, or `*.ckpt under ~/code`."""
    if rule.line == 0:
        return _display(rule.base) + "/ (all of it)"
    body = rule.pattern[1:] if rule.negated else rule.pattern
    if rule.anchored:
        return _display(os.path.join(rule.base, body.lstrip("/")))
    return f"{body} under {_display(rule.base)}"


def summarize(
    launch: str | os.PathLike,
    *,
    skip: "frozenset[str] | None" = None,
    legacy: bool = True,
    budget_s: float = 0.2,
    max_entries: int = 12,
    max_dirs: int = 20000,
) -> Summary:
    """What reaches a session started in `launch`: the excluding rules of the
    files above it (unless set aside) and of the files below it, found by a scan
    capped at `budget_s` and `max_dirs`. Never raises."""
    out = Summary()
    try:
        start = _norm(launch)
    except Exception:  # noqa: BLE001
        return out
    seen: set[str] = set()

    def add(rule: Rule) -> None:
        if rule.negated:
            return
        text = describe_rule(rule)
        if text in seen:
            return
        if len(out.entries) >= max_entries:
            out.truncated = True
            return
        seen.add(text)
        out.entries.append(text)

    skipped = frozenset(skip or ())
    verdict = check_cwd(start, skip=skipped, legacy=legacy)
    if verdict.excluded and verdict.rule is not None:
        out.entries.append(f"this folder ({_display(start)})")
        seen.add(out.entries[-1])
    for parent in parents_reaching(start):
        if parent.folder in skipped:
            continue
        loaded = _load(parent.folder)
        rel = _rel(start, parent.folder)
        for rule in loaded.rules if loaded else ():
            if (legacy or not rule.legacy) and rule.could_reach(rel):
                add(rule)
    deadline = time.monotonic() + budget_s
    stack = [start]
    visited = 0
    while stack:
        if time.monotonic() > deadline or visited >= max_dirs:
            out.truncated = True
            break
        folder = stack.pop()
        visited += 1
        loaded = _load(folder)
        if loaded is not None:
            for rule in loaded.rules:
                if legacy or not rule.legacy:
                    add(rule)
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    if entry.name in SCAN_SKIP_DIRS:
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue
    return out
