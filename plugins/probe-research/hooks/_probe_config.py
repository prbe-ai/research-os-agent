"""`.probe.config`: how much of a path Probe may touch, in gitignore syntax.

A `.probe.config` in any folder sets a LEVEL for paths under that folder:

- `off`: Probe never reads them and records nothing about them -- no run
  opened there, no artifact or note about them, no daemon event;
- `read`: the Probe daemon may see the agent's work on them, as context; Probe
  records nothing about them (to the SDK, the CLI and the agent this is `off`);
- `hash`: the SDK records each file's IDENTITY and nothing else -- its path,
  sha256 (or the >1 GiB fingerprint), size and row count -- so a step over the
  data still opens a run (metrics, config, these identities) and its arrows
  appear; never bytes, previews, uploads, output capture or text from the
  file. Below 64 bytes not even the hash. To the daemon this is `off`;
- `on`: recorded as usual (what `!pattern` means).

`read` and `hash` do not combine: one path has one winning level, and where
two verdicts meet (a symlink and its target) one at `read` and one at `hash`
make `off`. The guarantee is about what Probe RECORDS: text an agent prints
into its own session transcript still uploads with the transcript, as under
`off`. A client from before `hash` existed reads it as `off`.

A plain line is `off`. `@probe <level>` on a line of its own sets the level of
the lines after it (written first, it is the file's default), and
`<pattern> @probe <level>` sets one line's. A level only narrows what the
session's on / read / off switch allows; it never blocks the agent's own
Probe lookups. The rules are git's (`gitignore(5)`), applied the way git
applies them:

- a file covers its own folder and everything below it; patterns are relative
  to that folder; a folder with no file inherits its parents' rules;
- a later line beats an earlier one, a deeper file beats a shallower one;
- a folder at `off` is closed, as git closes an ignored folder: nothing inside
  it is looked at (its own `.probe.config` included) and no line turns
  anything inside it back on. `secret/*` closes nothing, so
  `secret/important.txt @probe on` after it works;
- a folder at `read`, `hash` or `on` stays open: what is inside it takes the
  folder's level unless a line matches it directly; a deeper `.probe.config`
  inside a `read` or `hash` folder may narrow it to `off`, never loosen it;
- `/` anchors, a trailing `/` matches folders only, `**` spans folders.

One deliberate difference from git: files ABOVE a repository root count too,
so `~/.probe.config` is the machine-wide list and `~/code/.probe.config` with
`/*` then `!/repo-a/` tracks only repo-a.

A file ABOVE the folder an agent session started in is the researcher's to set
aside for that session (`probe session parent ignore`); until they answer it is
followed. Files at or below the launch folder always apply.

THIS MODULE IS STDLIB-ONLY AND IMPORTS NOTHING FROM `probe`. It is copied
verbatim beside both plugins' hooks (`_probe_config.py`), which run under the
system python with no package on their path (`make sync-probe-config`; `tests/test_probe_config_sync.py` fails
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
import functools
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

#: The levels, least restrictive first. `on` is recorded; `read`, `hash` and
#: `off` are not recorded as usual (`Verdict.excluded`): `hash` records only a
#: file's identity (`Verdict.identity_only`), and `off` is not even read
#: (`Verdict.hidden`). `read` and `hash` are not ordered against each other
#: (`read` lets the daemon see, `hash` lets the SDK record identity): where both
#: meet, the answer is `off` (`stricter`). The rank orders the rest, and makes
#: `read` the looser of the two where only a loosest is asked (`check_cwd`).
ON = "on"
READ = "read"
HASH = "hash"
OFF = "off"
LEVELS = (ON, READ, HASH, OFF)
_RANK = {ON: 0, READ: 1, HASH: 2, OFF: 3}
#: The levels inside which a deeper `.probe.config` may narrow (to `off`) but
#: never loosen (`_ceiling`).
_CEILINGS = (READ, HASH)
#: The word that sets a level: `@probe <level>` alone on a line sets the lines
#: after it, `<pattern> @probe <level>` one line.
LEVEL_WORD = "@probe"

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
INIT_HASH_ONLY = (
    "probe: `{path}` is at `hash` by `{source}`: the run records metrics, config and the identity "
    "(path, hash, size, rows) of the files it reads and writes there, never their contents or its console log."
)
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
    #: What a match sets: `on`, `read` or `off`.
    level: str = OFF
    #: The pattern alone (no `!`, no `@probe` suffix), for describing the place.
    text: str = ""

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
        `base`, or anything inside it? From the pattern alone, no disk. A rule
        that sets `on` restricts nothing, so it reaches nothing."""
        if self.level == ON:
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


def level_of(word: str) -> str | None:
    """`on` / `read` / `hash` / `off` from a level word (any case), else None."""
    low = word.lower()
    return low if low in _RANK else None


def setting_line(raw: str) -> str | None:
    """The level word of an `@probe <level>` line, else None (a pattern line)."""
    parts = raw.strip(" \t\r\n").split()
    if len(parts) == 2 and parts[0].lower() == LEVEL_WORD:
        return parts[1]
    return None


def _split_level(text: str) -> tuple[str, str | None]:
    """`(pattern, level word)` for `<pattern> @probe <word>`, else `(text, None)`.
    Plain scans, no regex: the suffix is the last two words, `@probe` and the
    level, after whitespace. A pattern that really ends in ` @probe x` escapes
    it (`\\@probe`)."""
    end = len(text)
    while end and text[end - 1] in " \t":
        end -= 1
    j = end
    while j and text[j - 1] not in " \t":
        j -= 1
    word = text[j:end]
    k = j
    while k and text[k - 1] in " \t":
        k -= 1
    m = k
    while m and text[m - 1] not in " \t":
        m -= 1
    if not word or k == j or text[m:k].lower() != LEVEL_WORD:
        return text, None
    n = m
    while n and text[n - 1] in " \t":
        n -= 1
    if n == m or n == 0:
        return text, None
    body = text[:n]
    if body.endswith("\\") and not body.endswith("\\\\"):
        body += text[n]  # an escaped space just before the suffix stays
    return body, word


def parse_line(raw: str, base: str, source: str, line: int, default: str = OFF) -> Rule | None:
    """One `.probe.config` line as a Rule, None for a blank, comment or `@probe`
    setting line. `default` is the level a plain line sets (the last `@probe
    <level>` line above it). Raises `_BadPattern` for a line git would never match."""
    return _parse(raw, base, source, line, default)[0]


def _parse(raw: str, base: str, source: str, line: int, default: str = OFF) -> tuple[Rule | None, str | None]:
    """`parse_line`, plus a warning about the line (an unknown level counts as `off`)."""
    text = raw.rstrip("\r\n")
    if not text or text.startswith("#"):
        return None, None
    if setting_line(text) is not None:
        return None, None
    text, word = _split_level(text)
    note = None
    level = default
    if word is not None:
        level = level_of(word) or OFF
        if level_of(word) is None:
            note = f"`{word}` is not a level (on, read, hash, off), so the line counts as `off`."
    text = _trim_trailing_spaces(text)
    if not text:
        return None, None
    negated = False
    if text.startswith("!"):
        negated = True
        text = text[1:]
        if word is None:
            level = ON
    elif text.startswith("\\!") or text.startswith("\\#"):
        text = text[1:]
    if not text:
        return None, None
    shown_text = text
    dir_only = text.endswith("/") and not text.endswith("\\/")
    body = text[:-1] if dir_only else text
    if not body:
        return None, None
    anchored = "/" in body
    if body.startswith("/"):
        body = body[1:]
    if not body:
        return None, None
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
        level=level,
        text=shown_text,
    ), note


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
        anchored=False, segments=((("star",),),), legacy=legacy, level=OFF, text="*",
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
            default = OFF
            for number, raw in enumerate(text.splitlines(), start=1):
                word = setting_line(raw)
                if word is not None:
                    default = level_of(word) or OFF
                    if level_of(word) is None:
                        warnings.append(
                            f"`{path}:{number}`: `{word}` is not a level (on, read, hash, off), so the lines "
                            "after it count as `off`."
                        )
                    continue
                try:
                    rule, note = _parse(raw, folder, path, number, default)
                except _BadPattern:
                    warnings.append(f"`{path}:{number}` is not a valid pattern and was skipped.")
                    continue
                if note:
                    warnings.append(f"`{path}:{number}`: {note}")
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
    #: Probe records nothing about the path as usual (`read`, `hash` or `off`);
    #: at `hash` the SDK still records its identity (`identity_only`).
    excluded: bool
    rule: Rule | None = None
    #: The path the verdict was reached on (a closed parent folder, or the path).
    at: str = ""
    level: str = ON

    @property
    def source(self) -> str:
        return self.rule.where if self.rule is not None else ""

    @property
    def hidden(self) -> bool:
        """Probe never even reads the path (`off`)."""
        return self.level == OFF

    @property
    def identity_only(self) -> bool:
        """The SDK records the path's identity and nothing else (`hash`): path,
        sha256 or fingerprint, size and row count; never bytes or text."""
        return self.level == HASH

    @property
    def daemon_hidden(self) -> bool:
        """The daemon treats the path as `off`: `off` itself, and `hash`."""
        return self.level in (OFF, HASH)


NOT_EXCLUDED = Verdict(False)
#: What a check that broke answers: `off` (fail closed, as for an unreadable file).
CHECK_FAILED = Verdict(True, None, "", OFF)


def _verdict(rule: Rule | None, at: str) -> Verdict:
    if rule is None or rule.level == ON:
        return Verdict(False, rule, at, ON) if rule is not None else NOT_EXCLUDED
    return Verdict(True, rule, at, rule.level)


def daemon_view(verdict: Verdict) -> Verdict:
    """A verdict as the Probe daemon applies it: `hash` is `off` to the daemon
    (it never sees the path); every other level is itself."""
    if verdict.level == HASH:
        return Verdict(True, verdict.rule, verdict.at, OFF)
    return verdict


def stricter(a: Verdict, b: Verdict) -> Verdict:
    """The more restrictive of two verdicts (`a` on a tie). `read` and `hash`
    each allow what the other withholds, so where they meet the answer is
    `off`: never looser than either."""
    if a.level != b.level and {a.level, b.level} == {READ, HASH}:
        return Verdict(True, a.rule, a.at, OFF)
    return b if _RANK[b.level] > _RANK[a.level] else a


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
    if path == base:
        return ()
    prefix = base if base.endswith(os.sep) else base + os.sep
    if path.startswith(prefix):
        return tuple(p for p in path[len(prefix):].split(os.sep) if p and p != ".")
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


@dataclass(frozen=True)
class _Frame:
    """What a folder contributes to the paths inside it, worked out from its
    parent's frame: git's per-directory exclude stack (`dir.c` `prep_exclude`)."""

    folder: str
    #: The rules that apply inside this folder, shallow file first: (base, rules).
    layers: tuple
    #: The `off` rule that closed this folder or one above it: nothing inside is
    #: looked at, and nothing inside is turned back on.
    closed: Rule | None = None
    closed_at: str = ""
    #: The nearest rule (on this folder or one above) that set `read` or `on`:
    #: what an entry inside takes when no line matches it directly.
    inherited: Rule | None = None
    inherited_at: str = ""


class _Walk:
    """One session view (`skip`, `legacy`, `ttl`) applied to paths."""

    __slots__ = ("skip", "legacy", "ttl")

    def __init__(self, skip: frozenset, legacy: bool, ttl: float) -> None:
        self.skip, self.legacy, self.ttl = skip, legacy, ttl

    def root(self, folder: str) -> _Frame:
        return _Frame(folder, self._layers(folder, ()))

    def child(self, parent: _Frame, folder: str) -> _Frame:
        """The frame of `folder`, a folder directly inside `parent.folder`."""
        if parent.closed is not None:
            return _Frame(folder, parent.layers, parent.closed, parent.closed_at, parent.inherited, parent.inherited_at)
        found = _decide(folder, True, parent.layers)
        rule = _ceiling(found, parent)
        if rule is not None and rule.level == OFF:
            return _Frame(folder, parent.layers, rule, folder, parent.inherited, parent.inherited_at)
        if rule is None or (rule is parent.inherited and found is not rule):
            # No line, or a deeper `on` capped: the `read` folder stays the ceiling's place.
            inherited, inherited_at = parent.inherited, parent.inherited_at
        else:
            inherited, inherited_at = rule, folder
        return _Frame(folder, self._layers(folder, parent.layers), None, "", inherited, inherited_at)

    def entry(self, parent: _Frame, path: str, is_dir: bool) -> Verdict:
        """The verdict on `path`, an entry directly inside `parent.folder`."""
        if parent.closed is not None:
            return Verdict(True, parent.closed, parent.closed_at, OFF)
        rule = _ceiling(_decide(path, is_dir, parent.layers), parent)
        if rule is not None:
            return _verdict(rule, path)
        if parent.inherited is not None:
            return _verdict(parent.inherited, parent.inherited_at)
        return NOT_EXCLUDED

    def _layers(self, folder: str, layers: tuple) -> tuple:
        if folder in self.skip:
            return layers
        loaded = _load(folder, self.ttl)
        if loaded is None:
            return layers
        for message in loaded.warnings:
            _warn(message)
        rules = loaded.rules if self.legacy else tuple(r for r in loaded.rules if not r.legacy)
        return layers + ((folder, rules),) if rules else layers

    def one(self, stack: list, path: str, is_dir: bool) -> Verdict:
        """The verdict on `path`, reusing the frames already on `stack` for the
        folders it shares with the last path, and leaving its own there."""
        chain = _parts(path)
        if len(chain) < 2:
            return NOT_EXCLUDED
        folders = chain[:-1]
        keep = 0
        while keep < len(stack) and keep < len(folders) and stack[keep].folder == folders[keep]:
            keep += 1
        del stack[keep:]
        while len(stack) < len(folders):
            folder = folders[len(stack)]
            stack.append(self.root(folder) if not stack else self.child(stack[-1], folder))
        return self.entry(stack[-1], path, is_dir)


def _ceiling(rule: Rule | None, parent: _Frame) -> Rule | None:
    """A `read` or `hash` folder is a ceiling for the files inside it: a deeper
    `.probe.config` (one in that folder or below it) may narrow it to `off` but
    never set another level (`on`, or `hash` under `read` and `read` under
    `hash`, each of which lets something see more). Lines of the file that set
    the level may (`notebooks/ @probe read` then `notebooks/public.md @probe on`).
    Such a line from a deeper file is replaced by the rule it would override."""
    held = parent.inherited
    if rule is None or held is None or held.level not in _CEILINGS or rule.level in (OFF, held.level):
        return rule
    if rule.base != held.base and (rule.base == parent.inherited_at or _is_strict_ancestor(parent.inherited_at, rule.base)):
        return held
    return rule


def _check_one(path: str, is_dir: bool, skip: frozenset, legacy: bool, ttl: float = 0.0) -> Verdict:
    return _Walk(skip, legacy, ttl).one([], path, is_dir)


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
    except Exception:  # noqa: BLE001 - a check never breaks the caller, and fails closed
        return CHECK_FAILED
    if verdict.hidden or not resolve:
        return verdict
    try:
        real = _real(raw, ttl)
        if real != full:
            return stricter(verdict, _check_one(real, is_dir, skipped, legacy, ttl))
    except Exception:  # noqa: BLE001 - the path as given still decides
        pass
    return verdict


class Checker:
    """Many checks in a row, the way git checks them (`dir.c` `prep_exclude`):
    the folders of the last path stay on a stack, so the next path in the same
    folder costs one match instead of a walk to `/`. The answers are `check`'s
    exactly. It keeps nothing beyond itself: make one per batch (one snapshot,
    one walk, one tool output) and drop it."""

    def __init__(
        self,
        *,
        cwd: str | None = None,
        skip: "frozenset[str] | set[str] | None" = None,
        legacy: bool = False,
        ttl: float = 0.0,
        resolve: bool = True,
        daemon: bool = False,
    ) -> None:
        self.cwd = cwd
        self.resolve = resolve
        #: Answer as the daemon applies them (`daemon_view`: `hash` is `off`).
        self.daemon = daemon
        self._walk = _Walk(frozenset(skip or ()), legacy, ttl)
        self._given: list = []
        self._real: list = []
        #: Each folder's real path, once per batch (the folders of a batch repeat).
        self._real_dirs: dict[str, str] = {}

    def _resolved(self, raw: str) -> str:
        """`os.path.realpath(raw)`, with each folder's real path worked out once."""
        if ".." in raw.split(os.sep):
            return os.path.realpath(raw)
        folder, name = os.path.split(os.path.normpath(raw))
        real_dir = self._real_dirs.get(folder)
        if real_dir is None:
            if len(self._real_dirs) >= _CACHE_MAX:
                self._real_dirs.clear()
            real_dir = self._real_dirs[folder] = os.path.realpath(folder)
        joined = os.path.join(real_dir, name)
        return os.path.realpath(joined) if os.path.islink(joined) else joined

    def check(self, path: str | os.PathLike, *, is_dir: bool | None = None) -> Verdict:
        verdict = self._check(path, is_dir=is_dir)
        return daemon_view(verdict) if self.daemon else verdict

    def _check(self, path: str | os.PathLike, *, is_dir: bool | None = None) -> Verdict:
        try:
            raw = os.fspath(path)
            if raw.startswith("~"):
                raw = os.path.expanduser(raw)
            if not os.path.isabs(raw):
                raw = os.path.join(self.cwd or os.getcwd(), raw)
            full = os.path.normpath(raw)
            if is_dir is None:
                is_dir = os.path.isdir(full)
            verdict = self._walk.one(self._given, full, is_dir)
        except Exception:  # noqa: BLE001 - a check never breaks the caller, and fails closed
            return CHECK_FAILED
        if verdict.hidden or not self.resolve:
            return verdict
        try:
            real = self._resolved(raw)
            if real != full:
                return stricter(verdict, self._walk.one(self._real, real, is_dir))
        except Exception:  # noqa: BLE001 - the path as given still decides
            pass
        return verdict

    def closed(self, folder: str | os.PathLike) -> bool:
        """For a walk: is `folder` closed (`off`), so it is not entered at all?"""
        return self.check(folder, is_dir=True).hidden


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

    @functools.lru_cache(maxsize=None)
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

    found = rec(0, 0)
    return list(found) if found is not None else None


def _looser_inside(full: str, than: Verdict, skip: frozenset, legacy: bool, ttl: float) -> Verdict:
    """The least restrictive verdict some path INSIDE `full` actually gets, if
    looser than `than`. For each rule that sets a looser level (the folder's own
    file or one above), build one path inside `full` it matches and run the
    whole check on it: only a path that comes back looser proves it (order,
    depth and closed parents all count, as for any path)."""
    best = than
    for folder in _parts(full):
        if folder in skip:
            continue
        loaded = _load(folder, ttl)
        if loaded is None:
            continue
        rel = _rel(full, folder)
        for rule in loaded.rules:
            if _RANK[rule.level] >= _RANK[best.level] or (rule.legacy and not legacy):
                continue
            inside = _witness(rule, rel)
            if not inside:
                continue
            path = os.path.join(full, *inside)
            as_dir = rule.dir_only
            found = _check_one(path, as_dir, skip, legacy, ttl)
            if as_dir and _RANK[found.level] < _RANK[best.level]:
                found = stricter(found, _check_one(os.path.join(path, "x"), False, skip, legacy, ttl))
            if _RANK[found.level] < _RANK[best.level]:
                best = found
                if best.level == ON:
                    return best
    return best


def check_cwd(folder: str | os.PathLike, **kw) -> Verdict:
    """Is work run IN `folder` excluded? The folder itself is, or every entry in
    it would be (its own `*`, or a parent's `repo-b/**`). The level is the
    loosest any entry gets: an event in the folder may be about any of them."""
    verdict = check(folder, is_dir=True, **kw)
    if verdict.excluded:
        return verdict
    try:
        full = _norm(folder, kw.get("cwd"))
        skipped = frozenset(kw.get("skip") or ())
        legacy = bool(kw.get("legacy", False))
        ttl = float(kw.get("ttl", 0.0))
        probe_names = ("\u2063" * 8, "\u2063" * 9 + ".x")
        loosest: Verdict | None = None
        for name in probe_names:
            for as_dir in (False, True):
                child = _check_one(os.path.join(full, name), as_dir, skipped, legacy, ttl)
                if not child.excluded:
                    return NOT_EXCLUDED
                if loosest is None or _RANK[child.level] < _RANK[loosest.level]:
                    loosest = child
        if loosest is None:
            return NOT_EXCLUDED
        found = _looser_inside(full, loosest, skipped, legacy, ttl)
        if not found.excluded:
            return NOT_EXCLUDED
        return Verdict(True, found.rule, full, found.level)
    except Exception:  # noqa: BLE001
        return NOT_EXCLUDED


def off_inside(
    folder: str | os.PathLike,
    *,
    skip: "frozenset[str] | set[str] | None" = None,
    legacy: bool = False,
    ttl: float = 0.0,
    budget_s: float = 1.0,
    max_entries: int = 1_000_000,
) -> bool:
    """Is `folder`, or anything below it, at `off` -- or `hash`, which the
    daemon treats as `off` (the daemon's question: its only caller)? For a
    command that reads a whole tree (`rg`, `find`, `grep -r`, `git diff`): its
    output cannot be trusted to name every file it read, so such a command is
    refused, or its event dropped, when this is True. Skips what a search skips by default
    (hidden folders, `SCAN_SKIP_DIRS`). Exact when it finishes; True (fail
    closed) when it cannot finish in `budget_s` or `max_entries`. Never raises.

    Cheap when nothing can apply: the per-file walk runs only when an `off`
    rule above (or in) `folder` could reach inside it, or a `.probe.config`
    sits below it."""
    try:
        full = _norm(folder)
        real = os.path.realpath(full)
        if real != full and _off_inside_one(real, skip, legacy, ttl, budget_s, max_entries):
            return True
        return _off_inside_one(full, skip, legacy, ttl, budget_s, max_entries)
    except Exception:  # noqa: BLE001 - fail closed
        return True


def _off_inside_one(full: str, skip, legacy: bool, ttl: float, budget_s: float, max_entries: int) -> bool:
    """`off_inside` for one spelling of the folder."""
    checker = Checker(skip=skip, legacy=legacy, ttl=ttl, daemon=True)
    if checker.check(full, is_dir=True).hidden:
        return True
    deadline = time.monotonic() + budget_s
    stack: list = []
    checker._walk.one(stack, os.path.join(full, "x"), False)
    frame = stack[-1] if stack else None
    reaches = frame is not None and any(
        rule.level in (OFF, HASH) and (legacy or not rule.legacy) and rule.could_reach(_rel(full, base))
        for base, rules in frame.layers
        for rule in rules
    )
    if not reaches:
        # Only a `.probe.config` below `folder` could set something inside it.
        pending, seen = [full], 0
        while pending and not reaches:
            current = pending.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        seen += 1
                        if seen > max_entries or time.monotonic() > deadline:
                            return True
                        if current != full and (
                            entry.name == CONFIG_NAME
                            or (legacy and entry.name == LEGACY_DIRNAME and legacy_default(current) in ("off", "read-only"))
                        ):
                            reaches = True
                            break
                        if entry.name in SCAN_SKIP_DIRS:  # where nobody keeps a `.probe.config`
                            continue
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                pending.append(entry.path)
                        except OSError:
                            continue
            except OSError:
                continue
        if not reaches:
            return False
    pending, seen = [full], 0
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    seen += 1
                    if seen > max_entries or time.monotonic() > deadline:
                        return True
                    if entry.name == ".git":  # objects, never a file's name or text
                        continue
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    if checker.check(entry.path, is_dir=is_dir).hidden:
                        return True
                    if is_dir:
                        pending.append(entry.path)
        except OSError:
            continue
    return False


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
    walk = _Walk(frozenset(), True, 0.0)
    frame = None
    for folder in _parts(launch)[:-1]:
        try:
            frame = walk.root(folder) if frame is None else walk.child(frame, folder)
        except Exception:  # noqa: BLE001
            break
        if frame.closed is not None:
            break  # a closed folder: its file, and every file below it, is never read
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


#: Beside a `hash` place in a summary: Probe records only its files' identity.
HASH_ONLY_SUFFIX = " (hash only: identity, never content)"


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
    body = rule.text or (rule.pattern[1:] if rule.negated else rule.pattern)
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
        if rule.level == ON:
            return
        text = describe_rule(rule) + (HASH_ONLY_SUFFIX if rule.level == HASH else "")
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
        out.entries.append(f"this folder ({_display(start)})" + (HASH_ONLY_SUFFIX if verdict.identity_only else ""))
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
    walk = _Walk(skipped, legacy, 0.0)
    frames: list = []
    walk.one(frames, os.path.join(start, "x"), False)  # the frames down to `start`
    stack = [frames[-1]] if frames and frames[-1].closed is None else []
    visited = 0
    while stack:
        if time.monotonic() > deadline or visited >= max_dirs:
            out.truncated = True
            break
        frame = stack.pop()
        visited += 1
        loaded = _load(frame.folder)
        if loaded is not None and frame.folder not in skipped:
            for rule in loaded.rules:
                if legacy or not rule.legacy:
                    add(rule)
        try:
            with os.scandir(frame.folder) as it:
                for entry in it:
                    if entry.name in SCAN_SKIP_DIRS:
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            child = walk.child(frame, entry.path)
                            if child.closed is None:  # a closed folder is never entered
                                stack.append(child)
                    except OSError:
                        continue
        except OSError:
            continue
    return out
