"""read a pattern only where the windows say, scan long texts in overlapping windows, keep the outermost spans."""

from __future__ import annotations
import re
from collections.abc import Iterator

#: Overlap between consecutive scan windows. Must exceed the longest span any
#: rule can match (the private-key block, at ~33K) plus the pair window, so a
#: credential straddling a seam is still seen whole in one window.
_WINDOW_OVERLAP = 34_000


def _matches(
    pattern: re.Pattern[str],
    text: str,
    windows: Windows | None = None,
    *,
    skip_markers: bool = False,
) -> Iterator[re.Match[str]]:
    """`pattern.finditer(text)`, read only where `windows` says to look.

    With no windows (and no markers to skip) this IS `finditer`. With windows,
    each stretch is searched separately. A match that reaches a stretch's end
    before the text's end may be a truncation (`endpos` reads as end-of-text to
    lookaheads and `\\b`), so its start is matched again against the whole
    text and that answer is the one kept. Under the `Windows` contract every
    real match is strictly inside a stretch and this never fires; the quick
    check's keyword windows are not that exact, and a long bearer token is
    exactly what it would otherwise cut. Like `finditer`, a stretch resumes
    after the previous match, never inside it.

    `skip_markers` drops any match that touches a redaction marker. Without it,
    scanning already-redacted text is not stable: the "secret" in
    `<redacted:anchored-secret>` anchors the next value, whose own marker then
    anchors the one after, one more value per pass. A skipped match resumes the
    search just past the marker, so a real key name that the skipped match's
    gap covered is still seen.
    """
    spans = windows(pattern) if windows is not None else None
    if spans and sum(hi - lo for lo, hi in spans) * 2 >= len(text):
        # Stretches covering half the text are cheaper read as one: a search
        # per stretch costs more than the C loop they would skip.
        spans = None
    markers = (
        [(m.start(), m.end()) for m in _MARKER.finditer(text)]
        if skip_markers and "<redacted:" in text
        else []
    )
    if spans is None and not markers:
        yield from pattern.finditer(text)
        return
    end = len(text)
    resume = 0
    for lo, hi in spans if spans is not None else [(0, end)]:
        pos = max(lo, resume)
        while pos <= hi:
            match = pattern.search(text, pos, hi)
            if match is None:
                break
            if hi < end and match.end() >= hi:
                start = match.start()
                match = pattern.match(text, start)
                if match is None:
                    pos = start + 1
                    continue
            crossed = next(
                (stop for start, stop in markers if start < match.end() and stop > match.start()),
                None,
            )
            if crossed is None:
                yield match
                pos = max(match.end(), match.start() + 1)
            else:
                pos = max(crossed, match.start() + 1)
            resume = pos


def _scan_windowed(text: str, *, _decode: bool = True) -> list[Finding]:
    """`scan` over overlapping windows, with spans mapped back to absolute
    offsets. Linear in input length; one 64K window costs a few milliseconds.

    `_decode` passes through: a decoded view longer than one window used to be
    decoded AGAIN here, so a doubly-escaped credential was found or missed
    depending on whether its decoded neighbourhood crossed 64K characters."""
    findings: list[Finding] = []
    step = MAX_SCAN_CHARS - _WINDOW_OVERLAP
    for base in range(0, len(text), step):
        window = text[base:base + MAX_SCAN_CHARS]
        findings.extend(
            Finding(f.rule, f.start + base, f.end + base) for f in scan(window, _decode=_decode)
        )
        if base + MAX_SCAN_CHARS >= len(text):
            break
    return _dedupe(findings)


def _dedupe(findings: list[Finding]) -> list[Finding]:
    """Drop spans fully contained in another, keeping the outermost.

    Two rules legitimately match the same credential (a structured rule and
    pair promotion, say). Redacting both would corrupt the replacement.
    """
    if not findings:
        return []
    ordered = sorted(findings, key=lambda f: (f.start, -(f.end - f.start)))
    kept: list[Finding] = []
    for finding in ordered:
        if kept and finding.start >= kept[-1].start and finding.end <= kept[-1].end:
            continue
        if kept and finding.start < kept[-1].end:
            # Keep the union: first-wins can expose a suffix when two rules
            # recognize overlapping credential representations.
            previous = kept[-1]
            kept[-1] = Finding(previous.rule, previous.start, max(previous.end, finding.end))
            continue
        kept.append(finding)
    return kept
from .credential_scan import Finding, MAX_SCAN_CHARS, Windows, _MARKER, scan  # noqa: E402 -- pieces need each other; the front door loads this one first
