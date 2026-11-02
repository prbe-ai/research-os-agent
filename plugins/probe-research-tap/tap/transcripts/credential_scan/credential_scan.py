"""Credential detection and span redaction for captured transcripts.

Runs on the researcher's machine at the journal reservation boundary and in
the legacy batch builder. Both apply the same scanner before serialized
transcript content can leave the host.

WHY THIS EXISTS AND WHY IT LOOKS LIKE THIS
------------------------------------------
The previous gate (`app/ingestion/redaction.py`, removed in 0.104.9.0) dropped
645 batches across 206 sessions and 615 of those — 95% — were false positives.
Two heuristics did all the damage:

  * a BARE 40-char base64 window, which any 40-character file path satisfied
    (`/OdysseyPrivate/odyssey/experiments/casp`, H=3.85), and
  * a three-line `NAME=value` window, which ordinary pipeline stdout satisfied.

So this module does NOT run a bare high-entropy sweep. Entropy is only ever
consulted when something ELSE already says "a credential lives here":

  ANCHORED  a credential-shaped KEY NAME immediately precedes the value
            (`aws_secret_access_key = …`, `AWS Secret Access Key [None]: …`)
  PAIRED    a structured credential matched nearby, and the formless half of
            the same credential sits within `_PAIR_WINDOW` characters
            (an AWS access key id and its 40-char secret)

That is the whole design. An AWS secret access key has no format — it is 40
base64 characters and nothing distinguishes it from a checkpoint path — so the
only safe handles on it are its key name and its partner.

PIPELINE
--------
    text
      |
      v
  [1] keyword prefilter       lower(text) scanned ONCE; a rule is considered
      |                       only when one of its keywords is present
      v
  [2] structured rules        ~17 owned patterns, all linear (no nested
      |                       unbounded quantifiers — see test_secrets.py)
      v
  [3] anchored entropy        key-name anchor + high-entropy value
      |
      v
  [4] pair promotion          entropy token near a structured hit
      |
      v
  [5] indirect / placeholder  drop `os.environ[...]`, `${VAR}`, `<your-key>`
      |                       — these are references, not credentials
      v
    [Finding(rule, start, end)]

Every finding is replaced in place with `<redacted:{rule}>`. Nothing is ever
dropped: not the value's event, not its batch, not its session. One false
positive costs a mangled string, never a transcript.
"""

from __future__ import annotations
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass

#: Hard cap on text handed to one scan. A `time.monotonic()` check cannot
#: interrupt a running `re.search` — CPython holds the GIL inside a single
#: match — so the only real bound on worst-case scan time is the input length.
#: Every rule below is additionally reviewed for nested unbounded quantifiers
#: and pinned by a pathological-input test.
MAX_SCAN_CHARS = 64_000

#: Where in one text a pattern is worth running: the `(lo, hi)` stretches that
#: may hold a match, `[]` for "nowhere", or None for "no idea, read it all".
#:
#: THE CONTRACT an accelerator must keep, because correctness rests on it: every
#: match the pattern has in the WHOLE text lies inside one returned stretch, with
#: `hi` strictly past the match's end unless `hi` is the end of the text. `lo`
#: needs no margin -- a search starting at `lo` still lets lookbehinds and `\b`
#: read the characters before it. Stretches are sorted and do not overlap.
Windows = Callable[[re.Pattern[str]], "list[tuple[int, int]] | None"]
#: Given a text, its `Windows` -- or None when this accelerator cannot index it,
#: in which case the text is scanned exactly as if there were no accelerator.
#: The server supplies a Hyperscan-backed one (`app/security/fast_scan.py`);
#: nothing on a researcher's machine does, so there every pattern reads every
#: character, as it always has.
Accelerator = Callable[[str], "Windows | None"]

#: Below this length an accelerator costs more than it saves.
_ACCEL_MIN_CHARS = 4_096


@dataclass(frozen=True)
class Finding:
    """One credential-shaped span. `value` never leaves this process."""

    rule: str
    start: int
    end: int


#: What `redact` writes in place of a finding. A marker names its rule, and rule
#: names contain anchor words (`<redacted:anchored-secret>`, `probe-token`).
_MARKER = re.compile(r"<redacted:[a-z0-9_-]+>")


def _unmarked(
    pattern: re.Pattern[str], text: str, windows: Windows | None = None
) -> Iterator[re.Match[str]]:
    """`_matches` minus any match touching a redaction marker. See there."""
    return _matches(pattern, text, windows, skip_markers=True)


def _is_indirect(value: str) -> bool:
    """A reference or a placeholder rather than a live credential."""
    return bool(_INDIRECT.match(value))


def scan(
    text: str, *, _decode: bool = True, _accel: Accelerator | None = None
) -> list[Finding]:
    """Every credential-shaped span in `text`, ordered by position.

    Never raises on ordinary input: a non-string, an empty string and a
    multi-megabyte blob all return a list.

    Long input is scanned in OVERLAPPING windows rather than truncated.
    Truncating would bound the runtime by creating a blind spot — a credential
    at offset 64_001 would simply not be looked at — which is the wrong trade
    for a redactor. The overlap is `_WINDOW_OVERLAP`, comfortably wider than
    the longest span any rule can match, so nothing is missed at a seam.

    `_accel` is the server's: it names the stretches of `text` each pattern is
    worth reading (see `Windows`). Given one that can index the text, the whole
    text is scanned in one pass, reading only those stretches; the answer is the
    same set of credentials, found without reading the rest.
    """
    if not isinstance(text, str) or not text:
        return []
    windows = _accel(text) if _accel is not None and len(text) >= _ACCEL_MIN_CHARS else None
    if windows is None and len(text) > MAX_SCAN_CHARS:
        return _scan_windowed(text, _decode=_decode)

    lowered = ""
    findings: list[Finding] = []
    pair_anchors: list[tuple[int, int]] = []

    def present(keywords: tuple[str, ...], *patterns: re.Pattern[str]) -> bool:
        # An accelerator that found no candidate for any of these patterns has
        # already answered; skip the keyword sweep of the whole text.
        nonlocal lowered
        if windows is not None and all(windows(p) == [] for p in patterns):
            return False
        if not lowered:
            # ASCII-only when `str.lower` would change the length (`İ`):
            # `lowered` offsets are used as `text` offsets below.
            lowered = text.lower()
            if len(lowered) != len(text):
                lowered = text.translate(_ASCII_LOWER)
        return any(k in lowered for k in keywords)

    # [1] + [2] keyword prefilter, then the structured rules that survive it.
    for rule in _RULES:
        if rule.keywords and not present(rule.keywords, rule.pattern):
            continue
        if windows is not None and windows(rule.pattern) == []:
            continue
        for match in _matches(rule.pattern, text, windows):
            findings.append(Finding(rule.name, match.start(), match.end()))
            if rule.pairs_with_entropy:
                pair_anchors.append((match.start(), match.end()))

    if present(("password", "passwd"), _PASSWORD_ASSIGNMENT):
        for match in _unmarked(_PASSWORD_ASSIGNMENT, text, windows):
            group = next(name for name in ("double", "single", "bare") if match.group(name) is not None)
            value = match.group(group).rstrip()
            if not value or _is_indirect(value):
                continue
            if group == "bare" and match.group("gap")[:1] in ("\"", "'") and _NUMBER_LITERAL.fullmatch(value):
                continue
            if _is_word_like(value) and ("/" in value or value.startswith("--")):
                continue
            if group != "bare" and _is_template(text, match.start(group), value):
                continue
            start = match.start(group)
            if group != "bare" and _raw_regex(text, start, value):
                continue
            if _after_hump(text, match.start()):
                # A camelCase key: not `form.password.value`,
                # `_messages.StringField(1)` or `see 1Password`, but
                # `a8Kd93jLm2Qx`, `e3b0c44298fc1c14`, `"S3cr3tPassw0rd"`.
                camel = _camel_value(text, match.start("gap"), match.group("gap"), start, value, group != "bare")
                if camel is None:
                    continue
                start, value = camel
            findings.append(Finding("anchored-secret", start, start + len(value)))

    # [3] anchored entropy: a credential-shaped key name introduces the value.
    if present(_LONG_ANCHOR_KEYWORDS, _ANCHORED):
        for match in _unmarked(_ANCHORED, text, windows):
            if _model_token_anchor(text, match.start()):
                continue
            value = match.group("value")
            if _is_indirect(value) or _is_word_like(value):
                continue
            if shannon_entropy(value) < _ENTROPY_MIN:
                continue
            if text[match.end("value"):match.end("value") + 1] == "(":
                continue  # a function call: `cliToken = readProbeConfigMcpToken(env)`
            # The two widened anchors -- a camelCase hump and a `*_KEY` name --
            # also meet identifiers and header names (`ENGINE_INTERNAL_KEY:
            # X-Internal-Knowledge-Key`); a key value carries a digit.
            camel = _after_hump(text, match.start())
            widened = camel or match.group("keyname") or match.group("vendorkey")
            if widened and (not any(c.isdigit() for c in value) or _UUID.fullmatch(value)):
                continue
            if match.group("vendorkey") and not _owned_key_name(
                _identifier_at(text, match.start(), match.end("vendorkey"))
            ):
                continue  # `cache_key`, `sortKey`: nobody issued this key
            if widened:
                name = _identifier_at(text, match.start(), match.start("gap"))
                words = [w.lower() for w in _NAME_WORDS.findall(name)]
                if "public" in words or "publishable" in words:
                    continue  # `LANGFUSE_PUBLIC_KEY`: published by design
                if camel and words[-1:] == ["token"] and len(words) > 1 and words[-2] in _NOT_AUTH_TOKEN_WORDS:
                    continue
            start = match.start("value")
            if _raw_regex(text, start, value):
                continue
            if camel and _camel_value(
                text, match.start("gap"), match.group("gap"), start, value, text[start - 1] in "\"'"
            ) is None:
                continue
            if value.startswith(_PUBLISHABLE_KEY_PREFIXES):
                continue  # published by design: PostHog project keys, Stripe publishable keys
            findings.append(
                Finding("anchored-secret", match.start("value"), match.end("value"))
            )
    if present(_ANCHOR_KEYWORDS, _SHORT_ANCHORED):
        for match in _unmarked(_SHORT_ANCHORED, text, windows):
            if _model_token_anchor(text, match.start()):
                continue
            group = next(name for name in ("double", "single", "bare") if match.group(name) is not None)
            value = match.group(group)
            if _is_indirect(value):
                continue
            # Not key material: an escape is SERIALIZATION and a non-ASCII
            # character is TEXT. Every credential shape is plain ASCII drawn
            # from base64/hex, so both prove the value is content.
            #
            # This rule is the crude half of the anchored pass -- it accepts
            # SHORT values, so it leans on character classes and a 2.5 entropy
            # floor instead of length, and a tokenizer vocabulary dump walks
            # straight through both. `{"token": "\u0120the"}` is GPT-2's space
            # marker U+0120, and the escape alone supplies the digits and the
            # second character class; decoded, `\u0120cookie` becomes `Gcookie`
            # (with U+0120) whose entropy is 2.52 against a floor of 2.50. Both
            # halves matched, so every vocabulary row was a credential.
            #
            # A credential genuinely written with escapes is still caught:
            # `_encoded_findings` re-scans the decoded view and maps offsets
            # back, which is the entire purpose of that pass.
            if "\\" in value or not value.isascii():
                continue
            if group != "bare" and _is_template(text, match.start(group), value):
                continue
            start = match.start(group)
            if group != "bare" and _raw_regex(text, start, value):
                continue
            if _after_hump(text, match.start()):
                # After a camelCase hump (`jwtSecret`): never a bare `...Token`
                # (`nextPageToken`, `csrfToken`, `cancelToken` are code), and
                # the value must look like a secret (`_camel_value`).
                if match.group("anchor").lower() == "token":
                    continue
                camel = _camel_value(text, match.start("gap"), match.group("gap"), start, value, group != "bare")
                if camel is None:
                    continue
                start, value = camel
                if "\\" in value or not value.isascii():
                    continue
            if (_is_word_like(value) or _character_classes(value) < 2
                    or shannon_entropy(value) < 2.5):
                continue
            findings.append(Finding("anchored-secret", start, start + len(value)))

    # [3b] field shapes with no `key = value` separator. Each names its
    # credential (`_credential_name`) or has a fixed shape, and every value
    # passes `_loose_value_ok` or a decode check.
    def named(pattern: re.Pattern[str], rule: str, *names: str) -> None:
        for match in _unmarked(pattern, text, windows):
            name = next((match.group(n) for n in names if match.group(n)), "")
            # `--wandb-api-key` is the option for `wandb_api_key`.
            name = name.lstrip("-").replace("-", "_")
            if _credential_name(name) and _loose_value_ok(match.group("value")):
                findings.append(Finding(rule, match.start("value"), match.end("value")))

    if present(("setdefault", "getenv", ".get("), _ENV_DEFAULT):
        named(_ENV_DEFAULT, "env-default", "name")
    if present(("getenv", "environ.get", "process.env"), _ENV_FALLBACK):
        named(_ENV_FALLBACK, "env-default", "name", "jsname", "jsquoted")
    if present(("default",), _CLI_DEFAULT):
        named(_CLI_DEFAULT, "cli-default", "name")
    if present(("- name",), _ENV_PAIR):
        named(_ENV_PAIR, "env-pair", "name")
    if present(_DOCKERFILE_ENV_KEYWORDS, _DOCKERFILE_ENV):
        named(_DOCKERFILE_ENV, "dockerfile-env", "name")
    if present(("password",), _DSL_PASSWORD):
        for match in _unmarked(_DSL_PASSWORD, text, windows):
            value = match.group("value")
            if not _is_indirect(value) and not _is_slot(value) and not _is_template(text, match.start("value"), value):
                findings.append(Finding("anchored-secret", match.start("value"), match.end("value")))
    if present(_NETRC_KEYWORDS, _NETRC_PASSWORD):
        for match in _unmarked(_NETRC_PASSWORD, text, windows):
            # Written by a `printf`/`echo`, the entry ends at the closing
            # quote or a `\n` escape: `password $GH_TOKEN\n" > ~/.netrc`.
            value = _NETRC_VALUE_END.split(match.group("value"), maxsplit=1)[0]
            if not value or _is_indirect(value) or _is_slot(value):
                continue
            if not _NETRC_LOGIN.search(text, match.start(), match.start("value")) and not _generated(
                value.rstrip(_VALUE_EDGES + "!?")
            ):
                continue  # prose: "the machine gpu01 password reset flow."
            findings.append(Finding("netrc-password", match.start("value"), match.start("value") + len(value)))
    if present(("<password>",), _XML_PASSWORD):
        for match in _unmarked(_XML_PASSWORD, text, windows):
            value = match.group("value").rstrip()
            if value and not _is_indirect(value) and not _is_slot(value):
                findings.append(
                    Finding("xml-password", match.start("value"), match.start("value") + len(value))
                )
    for pattern, keywords in ((_DOCKER_AUTH, _DOCKER_AUTH_KEYWORDS), (_NPMRC_AUTH, ("_auth",))):
        if present(keywords, pattern):
            for match in _unmarked(pattern, text, windows):
                if _docker_auth_decodes(match.group("value")):
                    findings.append(Finding("docker-auth", match.start("value"), match.end("value")))
    if present(("identitytoken",), _DOCKER_IDENTITY_TOKEN):
        for match in _unmarked(_DOCKER_IDENTITY_TOKEN, text, windows):
            if shannon_entropy(match.group("value")) >= _ENTROPY_MIN:
                findings.append(Finding("docker-auth", match.start("value"), match.end("value")))
    if present(("wandb",), _YAML_WANDB_KEY):
        for match in _unmarked(_YAML_WANDB_KEY, text, windows):
            start = match.start()
            if lowered.rfind("wandb", max(0, start - _WANDB_BLOCK_REACH), start) >= 0:
                findings.append(Finding("wandb-key", match.start("value"), match.end("value")))

    # [4] pair promotion: the formless half of a structured credential.
    for anchor_start, anchor_end in pair_anchors:
        lo = max(0, anchor_start - _PAIR_WINDOW)
        hi = min(len(text), anchor_end + _PAIR_WINDOW)
        for match in _TOKEN.finditer(text, lo, hi):
            if match.start() >= anchor_start and match.end() <= anchor_end:
                continue  # the structured half itself
            value = match.group(0)
            if _is_indirect(value) or _is_word_like(value):
                continue
            if shannon_entropy(value) < _PAIR_ENTROPY_MIN:
                continue
            if _character_classes(value) < _PAIR_MIN_CLASSES:
                continue
            findings.append(Finding("paired-secret", match.start(), match.end()))

    if _decode:
        findings.extend(_encoded_findings(text, windows, _accel))
    return _dedupe(findings)


def redact(text: str) -> tuple[str, list[str]]:
    """`text` with every credential span replaced, plus the rules that fired.

    Returns the input unchanged when nothing matched, so callers can test
    identity cheaply.
    """
    return _replaced(text, scan(text))
from .decoding import _encoded_findings  # noqa: E402 -- pieces need each other; the front door loads this one first
from .events import _replaced  # noqa: E402 -- pieces need each other; the front door loads this one first
from .quick import _ASCII_LOWER  # noqa: E402 -- pieces need each other; the front door loads this one first
from .rules import (  # noqa: E402 -- pieces need each other; the front door loads this one first
    _ENTROPY_MIN,
    _PAIR_ENTROPY_MIN,
    _PAIR_MIN_CLASSES,
    _PAIR_WINDOW,
    _RULES,
)
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
    _INDIRECT,
    _NETRC_KEYWORDS,
    _NETRC_LOGIN,
    _NETRC_PASSWORD,
    _NETRC_VALUE_END,
    _NPMRC_AUTH,
    _NUMBER_LITERAL,
    _PASSWORD_ASSIGNMENT,
    _SHORT_ANCHORED,
    _TOKEN,
    _WANDB_BLOCK_REACH,
    _XML_PASSWORD,
    _YAML_WANDB_KEY,
    _docker_auth_decodes,
)
from .values import (  # noqa: E402 -- pieces need each other; the front door loads this one first
    _LONG_ANCHOR_KEYWORDS,
    _NAME_WORDS,
    _NOT_AUTH_TOKEN_WORDS,
    _PUBLISHABLE_KEY_PREFIXES,
    _UUID,
    _VALUE_EDGES,
    _after_hump,
    _camel_value,
    _character_classes,
    _credential_name,
    _generated,
    _identifier_at,
    _is_slot,
    _is_template,
    _is_word_like,
    _loose_value_ok,
    _model_token_anchor,
    _owned_key_name,
    _raw_regex,
    shannon_entropy,
)
from .windows import _dedupe, _matches, _scan_windowed  # noqa: E402 -- pieces need each other; the front door loads this one first
