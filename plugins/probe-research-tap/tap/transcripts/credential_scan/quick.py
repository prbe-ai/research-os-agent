"""the quick check (the QuickScanner): the same rules read only around keyword occurrences, no decoding."""

from __future__ import annotations
import functools
import re


# ---------------------------------------------------------------------------
# The quick check -- what the artifact gate runs on a researcher's machine
# ---------------------------------------------------------------------------
# The full scan above decodes escapes and base64 and reads whole texts rule by
# rule; on a 20 MB file that is tens of seconds of pure Python. A researcher's
# upload only needs the OBVIOUS credentials replaced before it leaves the
# machine -- the server reads every upload again in full (Hyperscan) and records
# what it finds. So the quick check runs the same rules and the same filters,
# with no decoding layer, and reads each rule only next to its own keywords,
# found with `str.find`. Standard library only.

#: How far before a keyword a match can start. Every rule's keyword sits within
#: its first ~25 characters (`sk-...T3BlbkFJ`, `https://hooks.slack.com`).
_KEYWORD_LEAD = 64
#: How far past a keyword to read for a rule whose width has no bound -- the
#: anchored values: anchor, a 24-character gap, separator, quote, up to 512
#: characters, and room for some spaces. Longer is not "obvious".
_QUICK_TAIL = 640
#: ASCII-only lowering: `str.lower` changes the length of a few Unicode
#: characters (`İ`), which would misalign keyword offsets.
_ASCII_LOWER = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}


@functools.lru_cache(maxsize=1)
def _quick_reach() -> dict[re.Pattern[str], tuple[tuple[str, ...], int]]:
    """Each quick-check pattern: its keywords and how far past one to read."""
    try:  # Python 3.11+ names
        from re import _constants as sre_constants  # type: ignore[attr-defined]
        from re import _parser as sre_parse  # type: ignore[attr-defined]
    except ImportError:  # Python 3.10 (plan 2.11): only the top-level modules exist
        import sre_constants
        import sre_parse

    def tail(pattern: re.Pattern[str]) -> int:
        width = sre_parse.parse(pattern.pattern, pattern.flags).getwidth()[1]
        return (_QUICK_TAIL if width >= sre_constants.MAXREPEAT else width) + 2

    reach = {rule.pattern: (rule.keywords, tail(rule.pattern)) for rule in _RULES if rule.keywords}
    reach[_PASSWORD_ASSIGNMENT] = (("password", "passwd"), _QUICK_TAIL)
    reach[_ANCHORED] = (_LONG_ANCHOR_KEYWORDS, _QUICK_TAIL)
    reach[_SHORT_ANCHORED] = (_ANCHOR_KEYWORDS, _QUICK_TAIL)
    reach[_ENV_DEFAULT] = (("setdefault", "getenv", ".get("), _QUICK_TAIL)
    reach[_ENV_FALLBACK] = (("getenv", "environ.get", "process.env"), _QUICK_TAIL)
    reach[_CLI_DEFAULT] = (("add_argument", "option("), tail(_CLI_DEFAULT))
    reach[_ENV_PAIR] = (("- name",), _QUICK_TAIL)
    reach[_DOCKERFILE_ENV] = (_DOCKERFILE_ENV_KEYWORDS, tail(_DOCKERFILE_ENV))
    reach[_DSL_PASSWORD] = (("password",), tail(_DSL_PASSWORD))
    reach[_NETRC_PASSWORD] = (_NETRC_KEYWORDS, tail(_NETRC_PASSWORD))
    reach[_XML_PASSWORD] = (("<password>",), tail(_XML_PASSWORD))
    reach[_DOCKER_AUTH] = (_DOCKER_AUTH_KEYWORDS, tail(_DOCKER_AUTH))
    reach[_NPMRC_AUTH] = (("_auth",), tail(_NPMRC_AUTH))
    reach[_DOCKER_IDENTITY_TOKEN] = (("identitytoken",), tail(_DOCKER_IDENTITY_TOKEN))
    # The match holds "key", not "wandb" (the block opens above it).
    reach[_YAML_WANDB_KEY] = (("key",), tail(_YAML_WANDB_KEY))
    return reach


def _keyword_windows(text: str) -> Windows:
    """`Windows` from `str.find`: a stretch around every keyword occurrence.

    A rule's match always contains one of its keywords, so these stretches
    hold every match the rule has. `_QUICK_TAIL` bounds the unbounded ones.
    """
    reach = _quick_reach()
    lowered = text.lower()
    if len(lowered) != len(text):
        lowered = text.translate(_ASCII_LOWER)
    found: dict[str, list[int]] = {}
    cache: dict[re.Pattern[str], list[tuple[int, int]] | None] = {}

    def positions(keyword: str) -> list[int]:
        if keyword not in found:
            hits: list[int] = []
            at = lowered.find(keyword)
            while at != -1:
                hits.append(at)
                at = lowered.find(keyword, at + 1)
            found[keyword] = hits
        return found[keyword]

    def windows(pattern: re.Pattern[str]) -> list[tuple[int, int]] | None:
        if pattern not in cache:
            spec = reach.get(pattern)
            if spec is None:
                cache[pattern] = None
            else:
                keywords, tail = spec
                spans: list[tuple[int, int]] = []
                for lo, hi in sorted(
                    (max(0, at - _KEYWORD_LEAD), min(len(text), at + tail))
                    for keyword in keywords
                    for at in positions(keyword)
                ):
                    if spans and lo <= spans[-1][1]:
                        spans[-1] = (spans[-1][0], max(spans[-1][1], hi))
                    else:
                        spans.append((lo, hi))
                cache[pattern] = spans
        return cache[pattern]

    return windows


def scan_quick(text: str) -> list[Finding]:
    """The researcher-side check: `scan`'s rules and filters, no decoding
    layer, each rule read only near its keywords. What it does not look for --
    escaped or base64-encoded credentials, the key-name flag -- the server's
    full inspection still records."""
    return scan(text, _decode=False, _accel=_keyword_windows)


def redact_quick(text: str) -> tuple[str, list[str]]:
    """`redact`, with `scan_quick`."""
    return _replaced(text, scan_quick(text))
from .credential_scan import Finding, Windows, scan  # noqa: E402 -- pieces need each other; the front door loads this one first
from .events import _replaced  # noqa: E402 -- pieces need each other; the front door loads this one first
from .rules import _RULES  # noqa: E402 -- pieces need each other; the front door loads this one first
from .shapes import (  # noqa: E402 -- pieces need each other; the front door loads this one first
    _ANCHORED,
    _ANCHOR_KEYWORDS,
    _CLI_DEFAULT,
    _DOCKERFILE_ENV,
    _DOCKERFILE_ENV_KEYWORDS,
    _DOCKER_AUTH,
    _DOCKER_AUTH_KEYWORDS,
    _DOCKER_IDENTITY_TOKEN,
    _DSL_PASSWORD,
    _ENV_DEFAULT,
    _ENV_FALLBACK,
    _ENV_PAIR,
    _NETRC_KEYWORDS,
    _NETRC_PASSWORD,
    _NPMRC_AUTH,
    _PASSWORD_ASSIGNMENT,
    _SHORT_ANCHORED,
    _XML_PASSWORD,
    _YAML_WANDB_KEY,
)
from .values import _LONG_ANCHOR_KEYWORDS  # noqa: E402 -- pieces need each other; the front door loads this one first
