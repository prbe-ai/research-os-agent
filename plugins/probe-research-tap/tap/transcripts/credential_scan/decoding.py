"""one bounded decoding layer: escapes decoded around a hit, offsets mapped back to the original text."""

from __future__ import annotations

import base64
import binascii
import bisect
import re
from collections.abc import Callable


_ENCODED_CHAR = re.compile(r"%[0-9a-fA-F]{2}|\\u[0-9a-fA-F]{4}|\\x[0-9a-fA-F]{2}|\x1b\[[0-?]*[ -/]*[@-~]|[\u200b-\u200d\ufeff]")
_BASE64 = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/_-]{24,16384}={0,2}(?![A-Za-z0-9+/=_-])")


#: How far a decoded credential can reach from an escape that hid part of it,
#: when an accelerator limits decoding to escapes' neighbourhoods. A credential
#: ENCODED as a whole (a URL-encoded PEM: `%2B`, `%2F`, `%0A` every line) is
#: covered end to end by the merged neighbourhoods; one isolated escape only has
#: to reach across a token, and the longest structured one short of a PEM or
#: JWT is ~250 characters. Accepted: a PEM or JWT longer than this whose ONLY
#: escape sits far from its ends is found by the plain path and not here.
#: Decoding the whole text instead re-indexes every nested view of a binary
#: file (random bytes carry an accidental `%xx` every ~30 KB): 5.6 s became
#: 95 s on a 32 MB checkpoint.
_ESCAPE_REACH = 2_048
#: How far a neighbourhood's edge moves to reach whitespace (`_snap`).
_SNAP = 256
_SPACE = re.compile(r"\s")
_LAST_SPACE = re.compile(r".*\s", re.DOTALL)


def _snap(text: str, lo: int, hi: int) -> tuple[int, int, bool, bool]:
    """`(lo, hi, lo_cut, hi_cut)`: a neighbourhood's edges moved out to the
    nearest whitespace, so `\\b` and lookbehinds at an edge see what they see
    in `text`. Cut in the middle of a word, `AKIA...` inside a longer run reads
    as a key on its own. `*_cut` says an edge found no whitespace in `_SNAP`
    characters and still splits the text."""
    if lo > 0:
        before = text[max(0, lo - _SNAP):lo]
        found = _LAST_SPACE.match(before)
        if found is not None:
            lo -= len(before) - found.end()
    if hi < len(text):
        found = _SPACE.search(text, hi, min(len(text), hi + _SNAP))
        if found is not None:
            hi = found.start()
    lo_cut = lo > 0 and not text[lo - 1].isspace()
    hi_cut = hi < len(text) and not text[hi].isspace()
    return lo, hi, lo_cut, hi_cut


def _encoded_findings(
    text: str, windows: Windows | None = None, accel: Accelerator | None = None
) -> list[Finding]:
    """One bounded decoding layer; offsets always refer to original text.

    Only a positive credential rule on the decoded view permits replacement.
    Opaque blobs, source escapes and hashes alone are never findings.

    Without windows the whole text is decoded and rescanned, as it always was.
    With them only the neighbourhood of each escape is (`_ESCAPE_REACH`): text
    far from every escape decodes to itself, and the plain pass already read it.
    """
    found: list[Finding] = []
    matches = list(_matches(_ENCODED_CHAR, text, windows))
    if matches:
        regions: list[tuple[int, int, list[re.Match[str]]]] = []
        if windows is None:
            regions.append((0, len(text), matches))
        else:
            for match in matches:
                lo = max(0, match.start() - _ESCAPE_REACH)
                hi = min(len(text), match.end() + _ESCAPE_REACH)
                if regions and lo <= regions[-1][1]:
                    start, _, inside = regions[-1]
                    inside.append(match)  # in place: a copy per escape was quadratic
                    regions[-1] = (start, max(regions[-1][1], hi), inside)
                else:
                    regions.append((lo, hi, [match]))
        for lo, hi, inside in regions:
            lo, hi, lo_cut, hi_cut = (lo, hi, False, False) if windows is None else _snap(text, lo, hi)
            decoded, original = _decoded_view(text, inside, lo, hi)
            for finding in scan(decoded, _decode=False, _accel=accel):
                if (lo_cut and finding.start == 0) or (hi_cut and finding.end == len(decoded)):
                    continue  # read against a cut edge, not against the text
                found.append(
                    Finding(finding.rule, original(finding.start)[0], original(finding.end - 1)[1])
                )
    for match in _matches(_BASE64, text, windows):
        value = match.group()
        if low_diversity(value):
            continue
        try:
            decoded = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True).decode('utf-8')
        except (ValueError, UnicodeDecodeError, binascii.Error):
            continue
        if scan(decoded, _decode=False):
            found.append(Finding('encoded-secret', match.start(), match.end()))
    return found


def _decoded_view(
    text: str, matches: list[re.Match[str]], lo: int, hi: int
) -> tuple[str, Callable[[int], tuple[int, int]]]:
    """`text[lo:hi]` with each escape in `matches` decoded, and a map from a
    decoded index back to the original characters behind it.

    Built from slices, one per run of plain text, so the cost follows the number
    of escapes rather than the length of the text.
    """
    parts: list[str] = []
    starts: list[int] = []  # decoded offset where each part begins
    sources: list[tuple[int, int | None]] = []  # (original start, original end | None=plain)
    cursor, length = lo, 0
    for match in matches:
        if match.start() > cursor:
            parts.append(text[cursor:match.start()])
            starts.append(length)
            sources.append((cursor, None))
            length += match.start() - cursor
        value = match.group()
        if value.startswith('%'):
            decoded = chr(int(value[1:], 16))
        elif value.startswith('\\'):
            decoded = chr(int(value[2:], 16))
        else:
            decoded = ''  # ANSI formatting and zero-width separators
        if decoded:
            parts.append(decoded)
            starts.append(length)
            sources.append((match.start(), match.end()))
            length += 1
        cursor = match.end()
    if hi > cursor:
        parts.append(text[cursor:hi])
        starts.append(length)
        sources.append((cursor, None))

    def original(index: int) -> tuple[int, int]:
        part = bisect.bisect_right(starts, index) - 1
        start, end = sources[part]
        if end is None:
            position = start + index - starts[part]
            return position, position + 1
        return start, end

    return "".join(parts), original
from .credential_scan import Accelerator, Finding, Windows, scan  # noqa: E402 -- pieces need each other; the front door loads this one first
from .values import low_diversity  # noqa: E402 -- pieces need each other; the front door loads this one first
from .windows import _matches  # noqa: E402 -- pieces need each other; the front door loads this one first
