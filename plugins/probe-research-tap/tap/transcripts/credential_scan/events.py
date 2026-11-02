"""redact a sanitized transcript event, including credentials split across adjacent blocks and ATIF fragments."""

from __future__ import annotations
from typing import Any


def _replaced(text: str, findings: list[Finding]) -> tuple[str, list[str]]:
    if not findings:
        return text, []
    out: list[str] = []
    cursor = 0
    for finding in findings:
        out.append(text[cursor:finding.start])
        out.append(f"<redacted:{finding.rule}>")
        cursor = finding.end
    out.append(text[cursor:])
    return "".join(out), [f.rule for f in findings]


def redact_event(event: Any) -> tuple[Any, list[str]]:
    """Redact every string anywhere in one sanitized transcript event.

    Walks the whole structure rather than an allowlist of fields: the tap ships
    prompts, assistant text, thinking, and the FULL Bash command, and a new
    field carrying a credential must not need a code change here to be covered.
    String leaves and dictionary keys are scrubbed. Sensitive field names
    supply context for opaque values; colliding redacted keys are disambiguated
    so their values are retained. Adjacent text fragments are also inspected.
    """
    fired: list[str] = []
    # What each adjacency join replaced, by (id(container), key): the text a
    # fragment's `line.text` still holds after the join changed its piece.
    joined: dict[tuple[int, Any], str] = {}
    fragments: list[dict] = []

    def walk(node: Any, context: str = '') -> Any:
        if isinstance(node, str):
            redacted, rules = redact(node)
            fired.extend(rules)
            if context and not context.startswith('<redacted:') and not rules:
                prefix = context + '='
                findings = scan(prefix + node)
                spans = [Finding(f.rule, max(0, f.start-len(prefix)), f.end-len(prefix))
                         for f in findings if f.end > len(prefix)]
                for finding in reversed(spans):
                    redacted = redacted[:finding.start] + f'<redacted:{finding.rule}>' + redacted[finding.end:]
                    fired.append(finding.rule)
            return redacted
        if isinstance(node, dict):
            out = {}
            reserved = set(node)
            for key, value in node.items():
                base_key = walk(key) if isinstance(key, str) else key
                new_key = base_key
                # Different sensitive keys may redact to the same marker;
                # reserve original keys too, including keys encountered later,
                # so generated suffixes cannot overwrite a benign sibling.
                suffix = 1
                while new_key in out or (base_key != key and new_key in reserved):
                    new_key = f'{base_key}:{suffix}'
                    suffix += 1
                out[new_key] = walk(value, key if isinstance(key, str) else '')
            if _is_atif_fragment(out):
                fragments.append(out)
            return out
        if isinstance(node, list):
            out = [walk(v) for v in node]
            _redact_adjacent_text(out, fired, joined)
            return out
        return node

    result = walk(event)
    for fragment in fragments:
        _mirror_into_line(fragment, joined)
    return result, fired


#: List items that are a text block, by `type`.
_TEXT_BLOCKS = ('text', 'input_text', 'output_text')
#: Blocks of an event's message that are prose, by `type`: the key of their text.
_PROSE_BLOCKS = {'text': 'text', 'input_text': 'text', 'output_text': 'text', 'thinking': 'thinking'}
#: Blocks that end a run of prose: a tool call or its result is not text.
_TOOL_BLOCKS = ('tool_use', 'tool_result')
#: An ATIF fragment (session upload protocol 3) is one event's share of its
#: session's trajectory (prbe-knowledge `engine/ingest/atif/fragment.py`), known
#: by its `kind` and its `line` object.
_ATIF_KINDS = ('user', 'assistant', 'system', 'other', 'none')
#: The `parts` of a fragment that hold prose, by type.
_ATIF_PROSE = ('text', 'thinking')
#: The labels prbe-knowledge `engine/shared/transcript_render.py` writes before
#: prose in a Line; `SYSTEM (<subtype>)` too, and any label of an `other`
#: event (its type). Tool calls, results and stop reasons have others.
_PROSE_LABELS = ('USER', 'COMPACTION SUMMARY', 'ASSISTANT', 'ASSISTANT (thinking)', 'SYSTEM')


def _redact_adjacent_text(items: list, fired: list[str], joined: dict | None = None) -> None:
    """Catch credentials fragmented across adjacent text blocks/messages.

    Only contiguous homogeneous text-bearing items participate; never join
    unrelated metadata fields or carry raw fragments across requests. The
    prose of consecutive items is read in order (`_prose`) and scanned as one
    text; a tool call or result, or an item with no prose, ends the run.
    """
    refs: list[tuple[Any, Any, str]] = []
    for index, item in enumerate(items):
        for piece in _prose(items, index, item):
            if piece is None:
                _redact_fragment_run(refs, fired, joined)
                refs = []
            else:
                refs.append((piece[0], piece[1], piece[0][piece[1]]))
    _redact_fragment_run(refs, fired, joined)


def _prose(items: list, index: int, item: Any) -> list[tuple[Any, Any] | None]:
    """The prose one list item adds to a run, in order, as (container, key);
    None where the run ends.

    - a bare string, or a text block;
    - an event (`{line_no, raw}` or a bare one): its message's string content,
      or the text and thinking blocks of its block list -- a tool call or
      result ends the run, any other block (an image) adds nothing; a wrapped
      event without a message (system, other) adds its string `content`;
    - an ATIF fragment: its `message`, then its text and thinking `parts`.

    An event and the fragment built from it read the same prose in the same
    order, so a split that one catches the other catches too (canary batches
    carry both).
    """
    if isinstance(item, str):
        return [(items, index)]
    if not isinstance(item, dict):
        return [None]
    if item.get('type') in _TEXT_BLOCKS and isinstance(item.get('text'), str):
        return [(item, 'text')]
    if _is_atif_fragment(item):
        return _fragment_prose(item) or [None]
    raw = item.get('raw', item)
    if not isinstance(raw, dict):
        return [None]
    message = raw.get('message')
    content = message.get('content') if isinstance(message, dict) else None
    if isinstance(content, str):
        return [(message, 'content')]
    if isinstance(content, list):
        pieces: list[tuple[Any, Any] | None] = []
        for block in content:
            kind = block.get('type') if isinstance(block, dict) else None
            key = _PROSE_BLOCKS.get(kind) if isinstance(kind, str) else None
            if key and isinstance(block.get(key), str):
                pieces.append((block, key))
            elif kind in _TOOL_BLOCKS:
                pieces.append(None)
        return pieces or [None]
    if (
        isinstance(item.get('raw'), dict) and not isinstance(message, dict)
        and raw.get('type') not in ('user', 'assistant') and isinstance(raw.get('content'), str)
    ):
        return [(raw, 'content')]
    return [None]


def _is_atif_fragment(item: Any) -> bool:
    return isinstance(item, dict) and item.get('kind') in _ATIF_KINDS and isinstance(item.get('line'), dict)


def _fragment_prose(fragment: dict) -> list[tuple[Any, Any] | None]:
    """A fragment's prose in order: `message`, then each text or thinking
    part; None for a part that is not prose (a tool call or result)."""
    pieces: list[tuple[Any, Any] | None] = []
    if isinstance(fragment.get('message'), str):
        pieces.append((fragment, 'message'))
    parts = fragment.get('parts')
    for part in parts if isinstance(parts, list) else ():
        prose = isinstance(part, dict) and part.get('type') in _ATIF_PROSE and isinstance(part.get('text'), str)
        pieces.append((part, 'text') if prose else None)
    return pieces


def _mirror_into_line(fragment: dict, joined: dict) -> None:
    """Redact in a fragment's `line.text` what the joins redacted in its prose.

    `line.text` repeats each prose piece once, in order, as the renderer writes
    it: one `LABEL: piece` segment that starts the text or follows a newline.
    Lines are never joined to each other (the label sits exactly where a split
    credential's halves meet, and a line also renders tool metadata), so a
    split is found in the prose and copied here by position: each piece is
    located after the one before it and exactly that span is replaced. A line
    that does not hold every piece where expected is replaced whole by the
    marker, never left holding a half.
    """
    edits = [(joined.get((id(c), k), c[k]), c[k]) for c, k in filter(None, _fragment_prose(fragment))]
    changed = [new for old, new in edits if new != old]
    line = fragment['line']
    text = line.get('text')
    if not changed or not isinstance(text, str) or not text:
        return  # nothing rendered (e.g. the renderer failed): nothing to hold a half
    out: list[str] = []
    cursor = 0
    for old, new in edits:
        if not old:
            continue  # an empty piece renders nothing
        at = _rendered_at(text, old, cursor, fragment.get('kind') == 'other')
        if at is None:
            marker = _MARKER.search(new if new != old else changed[0])
            line['text'] = marker.group(0) if marker else '<redacted:split-secret>'
            return
        out += (text[cursor:at], new)
        cursor = at + len(old)
    out.append(text[cursor:])
    line['text'] = ''.join(out)


def _rendered_at(text: str, piece: str, start: int, any_label: bool = False) -> int | None:
    """Where `piece` is rendered in `text` at or after `start`: a whole
    segment `LABEL: piece` that starts the text or follows a newline and ends
    at a newline or the end, behind a prose label (`_PROSE_LABELS`)."""
    at = text.find(piece, start)
    while at != -1:
        end = at + len(piece)
        if at >= 2 and text[at - 2:at] == ': ' and (end == len(text) or text[end] == '\n'):
            begin = text.rfind('\n', 0, at - 2) + 1
            label = text[begin:at - 2]
            prose = label in _PROSE_LABELS or label.startswith('SYSTEM (') or (any_label and ': ' not in label)
            if begin >= start and label and prose:
                return at
        at = text.find(piece, at + 1)
    return None


def _redact_fragment_run(
    refs: list[tuple[Any, Any, str]], fired: list[str], joined: dict | None = None,
) -> None:
    if len(refs) < 2:
        return
    # Stream adjacent fragments through overlapping scan windows. Skipping an
    # oversized pair creates a blind spot precisely at the join; tail/head
    # pairs alone also miss a token split over three or more small blocks.
    # Keep only one window in the temporary buffer, with absolute offsets for
    # mapping detections back to each original fragment below.
    findings: list[Finding] = []
    window = ''
    base = 0
    step = MAX_SCAN_CHARS - _WINDOW_OVERLAP
    for _, _, text in refs:
        offset = 0
        while offset < len(text):
            take = min(MAX_SCAN_CHARS - len(window), len(text) - offset)
            window += text[offset:offset + take]
            offset += take
            if len(window) == MAX_SCAN_CHARS:
                findings.extend(Finding(f.rule, base + f.start, base + f.end) for f in scan(window))
                window = window[step:]
                base += step
    if window:
        findings.extend(Finding(f.rule, base + f.start, base + f.end) for f in scan(window))
    findings = _dedupe(findings)
    cursor = 0
    for container, key, text in refs:
        end = cursor + len(text)
        local = [f for f in findings if f.start < end and f.end > cursor]
        original = text
        for finding in reversed(local):
            text = text[:max(0, finding.start-cursor)] + f'<redacted:{finding.rule}>' + text[min(len(text), finding.end-cursor):]
            fired.append(finding.rule)
        container[key] = text
        if joined is not None and text != original:
            joined.setdefault((id(container), key), original)
        cursor = end
from .credential_scan import Finding, MAX_SCAN_CHARS, _MARKER, redact, scan  # noqa: E402 -- pieces need each other; the front door loads this one first
from .windows import _WINDOW_OVERLAP, _dedupe  # noqa: E402 -- pieces need each other; the front door loads this one first
