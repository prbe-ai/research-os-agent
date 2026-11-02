"""judge whether a matched value is a live secret: slots and templates out, generated-looking, enough entropy and variety."""

from __future__ import annotations
import math
import re
from collections import Counter
from .shapes import _ANCHOR_KEYWORDS, _CREDENTIAL_NAME, _STRING_PREFIX


#: Keys a vendor documents as safe to publish (they ship inside client code).
_PUBLISHABLE_KEY_PREFIXES = ("phc_", "pk_live_", "pk_test_")
#: An f-string's interpolation. A quoted value that holds one is a template
#: (`f"Bearer {key}"`), never a literal credential.
_INTERPOLATION = re.compile(r"\{[^{}\n]*\}")
#: An unquoted value that is CODE: a dotted name (`form.password.value`), maybe
#: called, or a call (`getPassword(`, `_messages.StringField(1`). NOT a plain
#: word: `a8Kd93jLm2Qx` is how a generated password looks.
_DOTTED_CODE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+(?:\(.*)?|[A-Za-z_][\w.]*\(.*")
#: A value that is a template slot, not a value (the code snapshot's
#: `_PLACEHOLDER`, plus Jinja/Helm `{{ }}`): `{password}`, `{{ .Values.pw }}`,
#: `${VAR}`, `$GH_TOKEN`, `$1`, `%s`, `%(name)s`, `<TOKEN>`, `***`, `...`,
#: `:name`. A lower-case variable too (Gradle `password "$mavenPassword"`, a
#: shell `password $github_token`), but only in letters and `_`: a password
#: that starts with `$` (`$ecretPa55`) has a digit or a symbol.
_SLOT = re.compile(
    r"\{\{[^{}]*\}\}|\{[^{}]*\}|\$\{[^{}]*\}|\$\d+(?:::\w+)?|\$[A-Z_][A-Z0-9_]*|\$[a-z_][A-Za-z_]*"
    r"|\$\([^()]*\)|%s|%\(\w+\)s|<[^<>]*>|\*{3,}|\.{3}|\u2026|:[A-Za-z_]\w*"
)
#: Punctuation around a value that is not part of it.
_VALUE_EDGES = "\"'`.,;:)]}"
#: The same, minus `}`, which closes a slot (`{password}"]`).
_SLOT_EDGES = "\"'`.,;:)]"
#: A UUID. A widened anchor meets identifiers far more often than credentials:
#: `"ClientRequestToken": "<uuid>"`, `WS_KEY = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"`.
_UUID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
#: Who issues a key, for `_owned_key_name`: whole words of the name...
_KEY_OWNER_WORDS = frozenset({
    "api", "aws", "gcp", "hf", "xai", "ngc", "exa", "jwt", "hmac", "secret", "private", "access",
    "auth", "master", "admin", "license", "consumer", "client", "service", "account", "signing",
    "encryption", "webhook", "app", "bot", "openai", "azure", "cohere", "groq", "jina",
})
#: ...or a vendor anywhere in it (`azureOpenAIKey` splits into `open`, `ai`).
_KEY_OWNERS = (
    "openai", "azure", "anthropic", "claude", "cohere", "mistral", "gemini", "google", "stripe",
    "twilio", "sendgrid", "mailgun", "github", "gitlab", "slack", "discord", "telegram",
    "huggingface", "wandb", "firebase", "supabase", "pinecone", "replicate", "fireworks",
    "deepseek", "perplexity", "elevenlabs", "voyage", "tavily", "serper", "serpapi", "openrouter",
    "nvidia", "databricks", "cloudflare", "sentry", "datadog", "mapbox", "runpod", "langsmith",
    "langchain", "together", "anyscale",
)
_NAME_WORDS = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")
#: `_ANCHORED` also reads a camelCase `...Key` (`openaiKey`), which counts only
#: when its name holds an owner (`_owned_key_name`). The prefilter looks for the
#: most common VENDORS a few characters before `Key` -- not `key` itself, which
#: nearly every source file has (reading around each doubled the scan), nor
#: every owner (each keyword is one more pass over the text). Any other owner
#: (`masterKey`, `cloudflareKey`) is read where another anchor keyword is near.
_CAMEL_KEY_KEYWORDS = (
    "openai", "azure", "anthropic", "cohere", "mistral", "gemini", "google", "stripe",
    "wandb", "huggingface", "groq", "deepseek",
)
_LONG_ANCHOR_KEYWORDS = (*_ANCHOR_KEYWORDS, *_CAMEL_KEY_KEYWORDS)
#: A camelCase `...Token` that pages, deduplicates or orders requests, not one
#: that authenticates: `NextToken`, `nextPageToken`, `NextContinuationToken`,
#: `ClientRequestToken`, `PurchaseToken`. Their values are opaque base64, the
#: same shape as a credential, so only the name tells them apart.
_NOT_AUTH_TOKEN_WORDS = frozenset({
    "next", "page", "continuation", "pagination", "cursor", "resume", "sync", "purchase",
    "request", "idempotency", "cancel", "cancellation", "marker", "verification", "change",
    "batch", "query", "result", "results", "start", "end", "last", "prev", "previous",
    "iterator", "scroll", "grant", "upload", "part", "stream", "shard", "offset", "lock",
    "lease", "fencing", "watermark", "seek", "position",
})


def _is_slot(value: str) -> bool:
    """Whether a value is a template slot or a shell variable (see `_SLOT`),
    with or without the punctuation around it (`$GH_TOKEN"`, `{password},`)."""
    value = value.strip()
    return bool(_SLOT.fullmatch(value) or _SLOT.fullmatch(value.strip(_SLOT_EDGES)))


def _identifier_at(text: str, start: int, end: int) -> str:
    """The whole identifier that ends at ``end`` and holds ``start``."""
    while start and (text[start - 1].isalnum() or text[start - 1] == "_"):
        start -= 1
    return text[start:end]


def _owned_key_name(name: str) -> bool:
    """Whether a `*_key` / `...Key` name says who issued the key
    (`azure_openai_key`, `openaiKey`), which `cache_key` and `sortKey` do not."""
    lowered = name.lower()
    if any(owner in lowered for owner in _KEY_OWNERS):
        return True
    return any(word.lower() in _KEY_OWNER_WORDS for word in _NAME_WORDS.findall(name)[:-1])


def _credential_name(name: str) -> bool:
    """A name whose value is a credential (see `_CREDENTIAL_NAME`)."""
    match = _CREDENTIAL_NAME.search(name)
    if match is None:
        return False
    return not match.group("vendorkey") or _owned_key_name(name)


def _loose_value_ok(value: str) -> bool:
    """A value for a shape with no strong anchor of its own: not a slot, a
    reference or a UUID, and it passes the short-value checks."""
    return not _is_slot(value) and not _UUID.fullmatch(value) and _short_value_ok(value)


def _generated(value: str) -> bool:
    """A digit or a symbol in it (`a8Kd93jLm2Qx`, `Tr0ub4dor&3xQ`), as a
    generated secret has and a name (`master_user_password`) does not. A value
    with a space in it needs a digit: its punctuation is a sentence's
    (`"Forgot password?"`, `"Passwort vergessen?"`, `"Must match!"` are UI
    labels), and a space is not a symbol (`"Confirm password"`)."""
    if any(c.isspace() for c in value):
        return any(c.isdigit() for c in value)
    return any(c.isdigit() or not (c.isalnum() or c == "_") for c in value)


def _bare_token(value: str, after: str) -> str | None:
    """An unquoted value as ONE token that looks like a generated secret, or
    None. ``after`` is the rest of its line past ``value``. The token must end
    the line (only punctuation or a comment after it): after a camelCase key the
    rest of a line is often prose (`postgresPassword: see 1Password`,
    `oauth2ClientSecret: OAuth2 client secret to use`). Never code
    (`_DOTTED_CODE`) or a slot."""
    words = value.split()
    if not words:
        return None
    first, tail = words[0], (value[value.index(words[0]) + len(words[0]):] + after)
    tail = tail.lstrip(_VALUE_EDGES + " \t")
    if tail and not tail.startswith(("#", "//")):
        return None
    if _is_slot(first) or first.startswith(("{", "<", "%", "${", "$(")):
        return None  # template syntax, cut by the value's delimiters: `{password` of `{password}"]`
    token = first.rstrip(_VALUE_EDGES)
    if not token or _DOTTED_CODE.fullmatch(token) or _is_slot(token):
        return None
    if len(token) < _CAMEL_MIN_LEN or not _generated(token):
        return None  # `retryPassword = 3`, `userPassword = hunter` are not generated
    return token


def _after_hump(text: str, start: int) -> bool:
    """Whether a match at ``start`` began at a camelCase hump (an ASCII letter
    or digit before it), not after a separator. `\u0120password` in a GPT-2
    vocabulary starts after a separator: `_ANCHOR_START` is ASCII-only."""
    return start > 0 and text[start - 1].isascii() and text[start - 1].isalnum()


#: The gap between a camelCase key and its separator: a closing quote or
#: bracket, or a type (`const openaiKey: string =`). Anything else and the
#: separator is someone else's: `challengePassword['type'] =`,
#: `X25519PrivateKey(metaclass=`, `getBearerToken(): Promise`.
_CAMEL_GAP = re.compile(r"[\"'\]]?\s*(?::\s*[A-Za-z_][\w.\[\], |<>]{0,30}?\s*)?")
#: A typed field's literal later on the line: `string = "..."` after
#: `dbPassword:`.
_TYPED_LITERAL = re.compile(
    r"[A-Za-z_][\w.\[\]|<>, ]{0,30}?\s*=\s*" + _STRING_PREFIX + r"[\"'](?P<lit>[^\"'\n]{1,512})[\"']"
)
#: A raw string that is a regular expression, not a value:
#: `_SECRET_NAME_PARTIAL = r'(?P<secret>[a-zA-Z0-9-_]{1,255})'`.
_REGEX_SHAPE = re.compile(r"\(\?[:P<=!]|\[\^|\\[dswDSW]|\{\d+,\d*\}|\[[^\]\n]{0,40}[a-zA-Z0-9]-[a-zA-Z0-9]")


def _raw_regex(text: str, value_start: int, value: str) -> bool:
    return "r" in text[max(0, value_start - 3):value_start - 1].lower() and bool(_REGEX_SHAPE.search(value))


#: How much of a line past a camelCase key's value `_camel_value` reads: enough
#: for a typed field's literal (`: string = "..."`) or to see that a bare token
#: ends its line. Reading to the line's real end cost one pass over the rest of
#: the line PER MATCH: a 2 MB one-line file of `dbPassword=a8Kd93jLm2Qx,` took
#: 37 s in code capture. Past the reach the line counts as ended, so a token
#: followed by a KB of spaces and then more text counts (the safe direction).
_CAMEL_LINE_REACH = 1024
#: The word before `Secret` in a camelCase key whose value NAMES a secret object
#: rather than holding one: Helm's `existingSecret: pg-auth-v2`,
#: `imagePullSecret: regcred-v1`, `tlsSecret`, `certSecret`.
_SECRET_REFERENCE_WORDS = frozenset({"existing", "pull", "tls", "cert"})
#: Shortest value after a camelCase key that counts, quoted or not. A generated
#: secret is longer; `"n/a"`, `"-"`, `"TBD"` are not secrets.
_CAMEL_MIN_LEN = 6


def _names_a_secret(text: str, key_end: int) -> bool:
    """Whether the camelCase key ending at ``key_end`` names a secret object
    (see `_SECRET_REFERENCE_WORDS`). Reads at most 64 characters back."""
    if text[max(0, key_end - 6):key_end].lower() != "secret":
        return False
    lo, floor = key_end, max(0, key_end - 64)
    while lo > floor and (text[lo - 1].isalnum() or text[lo - 1] == "_"):
        lo -= 1
    words = [w.lower() for w in _NAME_WORDS.findall(text[lo:key_end])]
    return len(words) > 1 and words[-1] == "secret" and words[-2] in _SECRET_REFERENCE_WORDS


def _camel_value(
    text: str, key_end: int, gap: str, start: int, value: str, quoted: bool
) -> tuple[int, str] | None:
    """``(start, value)`` to report for a value after a camelCase key, or None.

    A camelCase key (`postgresPassword`, `jwtSecret`) is a field in code, a
    generated API client or prose far more often than a credential's name, so
    its value must look like one: on the key's line, after the key's own
    separator (`_CAMEL_GAP`); quoted, 6+ characters with a digit or a symbol
    (a digit if it has a space) and fewer than three words (`"S3cr3tPassw0rd"`,
    not `"dataStoreTestQuery"`, `"Forgot password?"` or `"Private key password
    1"`); unquoted, a `_bare_token`. Slots, references, UUIDs and the name of a
    secret object (`existingSecret: pg-auth-v2`) never count.
    """
    if not _CAMEL_GAP.fullmatch(gap) or text.find("\n", key_end, start) != -1:
        return None
    found = _camel_literal(text, start, value, quoted)
    return None if found is None or _names_a_secret(text, key_end) else found


def _camel_literal(text: str, start: int, value: str, quoted: bool) -> tuple[int, str] | None:
    """`_camel_value` past the key checks: the value, if it looks generated."""

    def literal(at: int, lit: str) -> tuple[int, str] | None:
        if _is_slot(lit) or _is_indirect(lit) or _UUID.fullmatch(lit) or len(lit.split()) >= 3:
            return None
        return (at, lit) if len(lit) >= _CAMEL_MIN_LEN and _generated(lit) else None

    if quoted:
        return literal(start, value)
    reach = start + len(value) + _CAMEL_LINE_REACH
    line_end = text.find("\n", start, reach)
    line_end = min(len(text), reach) if line_end < 0 else line_end
    token = _bare_token(value, text[start + len(value):line_end])
    if token is not None:
        return None if _is_indirect(token) or _UUID.fullmatch(token) else (start, token)
    typed = _TYPED_LITERAL.match(text, start, line_end)
    return literal(typed.start("lit"), typed.group("lit")) if typed else None


def _is_template(text: str, value_start: int, value: str) -> bool:
    """Whether a quoted value is an f-string template: an `f` prefix before its
    quote and an interpolation inside it."""
    prefix = text[max(0, value_start - 3):value_start - 1].lower()
    return "f" in prefix and bool(_INTERPOLATION.search(value))


def _short_value_ok(value: str) -> bool:
    """The short-value checks `_SHORT_ANCHORED` applies (see there)."""
    if not value or _is_indirect(value) or "\\" in value or not value.isascii():
        return False
    return not (_is_word_like(value) or _character_classes(value) < 2 or shannon_entropy(value) < 2.5)


_MODEL_TOKEN_KEYS = frozenset({
    "bos_token", "cls_token", "eos_token", "mask_token", "pad_token",
    "sep_token", "stop_token", "unk_token",
})


def _model_token_anchor(text: str, start: int) -> bool:
    """A token suffix in a tokenizer field is vocabulary, not auth context."""
    left = start
    right = start
    while left and (text[left - 1].isalnum() or text[left - 1] in "_-."):
        left -= 1
    while right < len(text) and (text[right].isalnum() or text[right] in "_-."):
        right += 1
    return text[left:right].lower().replace('-', '_').replace('.', '_') in _MODEL_TOKEN_KEYS


def shannon_entropy(value: str) -> float:
    """Bits per character. 40 random base64 chars land near 5.3; a lowercase
    path fragment lands near 3.5."""
    if not value:
        return 0.0
    length = len(value)
    total = 0.0
    # Counter keeps first-occurrence order, so the float sum is summed in the
    # same order as the per-character loop it replaced: identical results.
    for count in Counter(value).values():
        p = count / length
        total -= p * math.log2(p)
    return total


#: Fewest DISTINCT characters a base64-shaped run must use before it is worth
#: decoding as a possible credential. A run of one repeated character carries no
#: key material: `3` x 48 is a language model doing arithmetic inside a GSM8K
#: answer, not a secret, and `ababab...` is no better.
#:
#: Chosen over Shannon entropy because this floor has to hold identically for a
#: 24-character window and a 16K one, and entropy over a short window is
#: dominated by its LENGTH -- 24 random base64 chars and 240 of them score very
#: differently while being equally secret. Distinct-character count is
#: length-independent, so one number is honest at both ends.
#:
#: Every real credential clears it by a wide margin: base64 needs ~12 distinct
#: characters before it can carry even 64 bits, and the least diverse fixture in
#: the test suite uses far more. `test_diversity_floor_admits_no_real_secret` is
#: the negative control -- it fails if this number is ever raised far enough to
#: let a real credential through.
_MIN_CANDIDATE_DISTINCT = 6


def low_diversity(value: str) -> bool:
    """True when a base64-shaped run is too uniform to encode a credential.

    Shared with the artifact gate (`probe.sdk.secret_gate`) so both halves of
    the boundary agree about what is not even worth calling a candidate.
    """
    return len(set(value)) < _MIN_CANDIDATE_DISTINCT


#: A value is WORD-LIKE when `-`/`_` split it into three or more parts and at
#: least two of those are plain alphabetic words. That is what a CLI flag
#: (`--some-thing-SOME-VALUE-`), a test name, and a hyphenated identifier all
#: look like, and what an opaque credential never does: an AWS secret has no
#: separators at all, and a base64url token's segments carry digits rather than
#: being words. Measured on 4,000 real production chunks, this is the shape of
#: every remaining entropy-based false positive and none of the true positives.
#: A "word" is SINGLE-CASE alphabetic. That is the whole trick: path segments,
#: flag names and test names are `configs` / `SOMETHING`, while base64 segments
#: are mixed-case (`wJalrXUtnFEMI`, `bPxRfiCYEXAMPLEKEY`). Requiring mixed case
#: to count as opaque is what lets this reject `.../configs-abc-defg` while
#: keeping AWS's own documented example secret, which splits into three parts
#: on `/` and would otherwise look exactly as word-like.
_WORD_PART = re.compile(r"^(?:[a-z]{3,}|[A-Z]{3,})$")

#: Split on `/` too. `_SECRET_CHARS` contains `/` because an AWS secret may, so
#: a FILE PATH also matches the value pattern — and a 40-character path is
#: precisely what produced 539 of the 615 false positives in 2026-08. The
#: anchor requirement already stops most of them; this stops the rest.
_PART_SEPARATORS = re.compile(r"[-_/.]")


def _is_word_like(value: str) -> bool:
    parts = [p for p in _PART_SEPARATORS.split(value) if p]
    if len(parts) < 2:
        return False
    return sum(1 for p in parts if _WORD_PART.match(p)) >= 2


def _character_classes(value: str) -> int:
    """How many of {lowercase, uppercase, digit} appear. Opaque credentials use
    at least two; `attention-ablation-config` uses one."""
    return (
        any(c.islower() for c in value)
        + any(c.isupper() for c in value)
        + any(c.isdigit() for c in value)
    )
from .credential_scan import _is_indirect  # noqa: E402 -- pieces need each other; the front door loads this one first
