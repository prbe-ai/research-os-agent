"""the anchored and field shapes: key = value, env defaults, netrc, docker auth, npmrc, YAML keys."""

from __future__ import annotations

import base64
import binascii
import re
from .rules import _ENTROPY_MAX_LEN, _ENTROPY_MIN_LEN, _SECRET_CHARS

# ---------------------------------------------------------------------------
# Anchored entropy — a credential-shaped KEY NAME immediately before a value.
#
# The anchor list is deliberately tight. `hash`, `id`, `sha`, `digest` and
# `signature` are NOT anchors: `content_hash = e3b0c442…` is ordinary output
# and firing on it is how the last gate died.
# ---------------------------------------------------------------------------

_ANCHOR_WORDS = (
    "secret", "password", "passwd", "api[_ -]?key", "apikey", "access[_ -]?key",
    "secret[_ -]?key", "private[_ -]?key", "auth[_ -]?token", "access[_ -]?token",
    "refresh[_ -]?token", "bearer[_ -]?token", "client[_ -]?secret", "credential", "token",
)

#: `<anchor> <sep> <value>` where sep is `=`, `:` or `: [None]:`-style noise.
#: The `[^\n]{0,24}?` between anchor and separator absorbs `[None]` and
#: quoting without ever crossing a line.
#:
#: The boundaries are `(?<![A-Za-z0-9])` / `(?![A-Za-z0-9])`, NOT `\b`. `_` is a
#: word character, so `\bsecret` never matches inside `aws_secret_access_key`
#: or `AWS_SECRET_ACCESS_KEY` — which are the two spellings that actually
#: appear in a credentials file and an `export` line.
#: `[^\n.!?]` and not `[^\n]`: the gap between the anchor and its separator may
#: not cross SENTENCE punctuation. Measured on 4,000 real production chunks,
#: that one character class is the difference between matching
#: `AWS Secret Access Key [None]:` (a credential) and
#: `... secret configuration. BEFORE:` (prose about credentials, followed by a
#: CLI flag) — which was 6 of the 7 entropy-based false positives in that sample.
#: Nor may it leave a string the anchor is not the name in: a closing quote
#: followed by `,`, `}` or `],` ends a VALUE, and the separator after it
#: belongs to the next key. A JSONL glossary
#: (`{"term": "Token"}, {"term": "Amazon US"}`) masked whatever term followed
#: "Token", and a transcript (`"input": "...forgot password should",
#: "output": "..."`) its whole output. One `",` may still follow the anchor's
#: own name, run on by identifier characters (`{"name": "DB_PASSWORD_PROD",
#: "value": "..."}`): a value that merely ENDS in an anchor word reads the same
#: and is still masked. Any other label (`"Password for DB"`, `"API Key
#: (prod)"`, an AWS tag's `"Key"`) may close its string only into a value
#: slot -- `value`, `string_value`, `data`, `val`, `default`/`defaultValue`,
#: quoted or not, before `:` or `=` (JSON, JS, YAML flow, HCL, kwargs) --
#: which the transcript's `output` is not (`secret` is an anchor of its own;
#: `valueFrom` is a reference, not a value). Spaces, never newlines: the gap
#: stays on its line.
_GAP = (r"(?P<gap>(?:[A-Za-z0-9_\-/}]{0,24}[ \t]*[\"'][ \t]*,"
        r"|[^\n.!?\"']{0,24}[\"'][ \t]*(?:\][ \t]*)?,"
        r"(?=[ \t]*[\"']?(?:(?:string_?)?value|data|val|default(?:_?value)?)[\"']?[ \t]*[:=]))?"
        r"(?:(?![\"'][ \t]*(?:\][ \t]*)?[,}])[^\n.!?]){0,24}?)")
#: Where an anchor may start: after a non-alphanumeric character, or at a
#: camelCase hump -- a lowercase letter or digit, then the anchor's capital
#: (`postgresPassword`, `openaiApiKey`), or the last capital of an acronym
#: before a capitalized word (`DBPassword`, `SMTPPassword`, `JWTSecret`).
#: Case-sensitive inside the otherwise case-insensitive patterns, so
#: `bypassword` and `PASSWORDS` stay one word.
_ANCHOR_START = r"(?:(?<![A-Za-z0-9])|(?-i:(?<=[a-z0-9])(?=[A-Z]))|(?-i:(?<=[A-Z])(?=[A-Z][a-z])))"
#: Key names that mark a credential only when a LONG, high-entropy value follows
#: (`_ANCHORED`, never the short-value rules): `encryption_key`, `signing_key`,
#: and an environment-style `*_KEY` (`AZURE_OPENAI_KEY`, `SIGNING_KEY`). The
#: capitals are case-sensitive, so `sort_key = 3` never qualifies, and the
#: length is bounded so a long `A_A_A...` run stays linear. A `*_KEY` ending in
#: an ordinary anchor (`OPENAI_API_KEY`, `DJANGO_SECRET_KEY`) is left to that
#: anchor, which has no digit rule: matched here first, the whole name was
#: dropped for a value with no digit and the `API_KEY` inside it never read.
_LONG_VALUE_ANCHOR_WORDS = (
    "encryption[_ -]?key", "signing[_ -]?key",
    r"(?-i:[A-Z][A-Z0-9_]{0,40}(?<!API)(?<!ACCESS)(?<!SECRET)(?<!PRIVATE)_KEY)",
)
#: A lower-case `*_key` name or a camelCase `...Key` (`azure_openai_key`,
#: `openaiKey`, `azureOpenAIKey`). Far more of these are NOT credentials
#: (`cache_key`, `sortKey`, `publicKey`, `partition_key`), so they count only
#: when the name also names who issued the key (`_owned_key_name`), and like
#: the other widened anchors only before a long value with a digit in it.
_VENDOR_KEY_WORDS = (r"(?-i:[a-z][a-z0-9_]{0,40}_key)", r"(?-i:(?<=[A-Za-z0-9])Key)")
#: The same two, as anchors that start AT the final `key` / `Key` (the name
#: before them is read with `_identifier_at`): a literal is cheap to look for,
#: where `[a-z][a-z0-9_]{0,40}_key` had to be tried from every word's first
#: letter. After `_ANCHOR_START`: `key` follows `_`, `Key` a camelCase hump.
_VENDOR_KEY_ANCHOR = r"(?-i:(?<=[a-z0-9]_)key|(?<=[A-Za-z0-9])Key)"
#: A Python/JS string prefix before the opening quote: `f"..."`, `rb'...'`.
_STRING_PREFIX = r"(?:[rbuf]{1,2}(?=[\"']))?"
_ANCHORED = re.compile(
    r"(?i)" + _ANCHOR_START + r"(?:" + "|".join(_ANCHOR_WORDS)
    + r"|(?P<keyname>" + "|".join(_LONG_VALUE_ANCHOR_WORDS) + r")"
    + r"|(?P<vendorkey>" + _VENDOR_KEY_ANCHOR + r"))"
    r"(?![A-Za-z0-9])"
    + _GAP + r"[:=]\s*" + _STRING_PREFIX +
    r"[\"']?(?P<value>[" + _SECRET_CHARS + r"]{" + str(_ENTROPY_MIN_LEN) + r",512})",
)

#: Candidate opaque values, used for pair promotion.
_TOKEN = re.compile(
    r"[" + _SECRET_CHARS + r"]{" + str(_ENTROPY_MIN_LEN) + r"," + str(_ENTROPY_MAX_LEN) + r"}"
)

#: A value that is a REFERENCE to a credential, not the credential.
#: `os.environ['X']`, `${X}`, `%s`, `<your-key>`, `***`, `xxxxx`.
_INDIRECT = re.compile(
    r"(?i)^(?:os\.(?:environ|getenv)|process\.env|env\[|\$\{|<|%[sd]|\*{3,}|x{8,}|"
    r"your[_-]|placeholder|example|redacted|changeme|dummy|sample)"
)

#: Anchor words as plain lowercase substrings, for the prefilter.
_ANCHOR_KEYWORDS = ("secret", "password", "passwd", "api key", "api_key", "apikey",
                    "access key", "access_key", "private key", "private_key",
                    "auth token", "auth_token", "access token", "access_token",
                    "credential", "client secret", "client_secret", "bearer",
                    "api-key", "access-key", "private-key", "auth-token", "access-token",
                    "accesskey", "privatekey",
                    "refresh_token", "refresh-token", "refresh token", "token",
                    "_key", "encryption", "signing")

# Explicit assignments allow short, mixed-class passwords and quoted spaces.
# They still require an anchor, entropy, and mixed character classes: prose and
# references must not become the bare-entropy gate this module replaced.
_SHORT_ANCHORED = re.compile(
    r"(?i)" + _ANCHOR_START + r"(?P<anchor>" + "|".join(_ANCHOR_WORDS) + r")(?![A-Za-z0-9])"
    + _GAP + r"[:=]\s*" + _STRING_PREFIX + r"(?:\"(?P<double>[^\"\n]{1,512})\"|"
    r"'(?P<single>[^'\n]{1,512})'|(?P<bare>[^\s\"'`,;]{1,512}))"
)
# Password assignments carry context even when the value is a short word or
# a human passphrase. Quoting ends at its matching quote; an unquoted value
# continues through spaces until a statement delimiter, never just word one.
#: A JSON/Python number literal. After a QUOTED `"password"` key it is a
#: tokenizer vocabulary's id (`"password": 20786` in BERT's tokenizer.json),
#: never a credential: replacing it also breaks the JSON.
_NUMBER_LITERAL = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
_PASSWORD_ASSIGNMENT = re.compile(
    r"(?i)" + _ANCHOR_START + r"(?:password|passwd)(?![A-Za-z0-9])(?P<gap>[\"']?\s*)[:=]\s*" + _STRING_PREFIX +
    r"(?:\"(?P<double>(?:\\.|[^\"\\])*)\"|'(?P<single>(?:\\.|[^'\\])*)'|"
    r"(?P<bare>[^\r\n,;)}\]\"'`]+))"
)
# ---------------------------------------------------------------------------
# Field shapes with no `key = value` separator (#2000 re-review)
# ---------------------------------------------------------------------------

#: A default a program falls back to when the environment has none:
#: `os.environ.setdefault("WANDB_API_KEY", "...")`, `os.getenv("HF_TOKEN", "...")`,
#: `settings.get("api_key", "...")`. The NAME must be credential-shaped
#: (`_CREDENTIAL_NAME`) and the value passes the short-value checks.
_ENV_DEFAULT = re.compile(
    r"(?i)(?:setdefault|getenv|\.get)\(\s*[\"'](?P<name>[A-Za-z0-9_.\-]{1,80})[\"']\s*,\s*"
    r"(?:default\s*=\s*)?" + _STRING_PREFIX + r"[\"'](?P<value>[^\"'\n]{1,512})[\"']"
)
#: The same default written as a fallback after the lookup:
#: `os.environ.get("OPENAI_API_KEY") or "..."`, `process.env.AZURE_OPENAI_KEY || "..."`,
#: `process.env["OPENAI_API_KEY"] ?? "..."`.
_ENV_FALLBACK = re.compile(
    r"(?:(?:getenv|environ\.get)\(\s*[\"'](?P<name>[A-Za-z0-9_.\-]{1,80})[\"']\s*\)"
    r"|process\.env(?:\.(?P<jsname>[A-Za-z_][A-Za-z0-9_]{0,79})"
    r"|\[\s*[\"'](?P<jsquoted>[A-Za-z0-9_]{1,80})[\"']\s*\]))"
    r"\s*(?:\bor\b|\|\||\?\?)\s*" + _STRING_PREFIX + r"[\"'`](?P<value>[^\"'`\n]{1,512})[\"'`]"
)
#: A command-line option's default: `parser.add_argument("--wandb-api-key",
#: default="...")`, `@click.option("--token", default="...")`.
_CLI_DEFAULT = re.compile(
    r"(?:add_argument|option)\(\s*[\"'](?P<name>-{1,2}[A-Za-z0-9][A-Za-z0-9_\-]{0,79})[\"']"
    r"[^()]{0,300}?\bdefault\s*=\s*" + _STRING_PREFIX + r"[\"'](?P<value>[^\"'\n]{1,512})[\"']"
)
#: A Kubernetes (or compose / Helm) env list entry: `- name: AZURE_OPENAI_KEY`,
#: then `value: ...` on the next line.
_ENV_PAIR = re.compile(
    r"(?m)^[ \t]*-[ \t]?name[ \t]*:[ \t]*[\"']?(?P<name>[A-Za-z_][A-Za-z0-9_.\-]{0,79})[\"']?[ \t]*\r?\n"
    r"[ \t]*value[ \t]*:[ \t]*[\"']?(?P<value>[^\"'\s]{1,512})"
)
#: Keywords (lower-cased text) for the rules below. Each is one pass over the
#: text and one window per hit, so each is as narrow as its pattern allows:
#: `env ` rather than `env`, `default login` rather than `default`.
_DOCKERFILE_ENV_KEYWORDS = ("env ", "env\t")
_NETRC_KEYWORDS = ("machine", "default login")
_DOCKER_AUTH_KEYWORDS = ('auth":', "auth':", "auth:")
#: A Dockerfile `ENV NAME value` (the form without `=`).
_DOCKERFILE_ENV = re.compile(
    r"(?m)^[ \t]*ENV[ \t]+(?P<name>[A-Za-z_][A-Za-z0-9_]{0,79})[ \t]+[\"']?(?P<value>[^\"'\s]{1,512})"
)
#: A build-DSL line holding only `password "..."` (Gradle / Groovy `credentials {}`).
_DSL_PASSWORD = re.compile(
    r"(?im)^[ \t]*password[ \t]+[\"'](?P<value>[^\"'\n]{1,512})[\"'][ \t]*$"
)
#: A credential-shaped variable NAME, for the shapes above: its LAST word is an
#: anchor (`WANDB_API_KEY`, `HF_TOKEN`, `DB_PASSWORD`, `dbPassword`), with at most
#: a number or `_BASE` after it (`API_KEY_2`, `SECRET_KEY_BASE`). Not a name
#: that is ABOUT a credential: `secret_name`, `TOKEN_URL`,
#: `id_token_encrypted_response_enc`.
_CREDENTIAL_NAME = re.compile(
    r"(?i)" + _ANCHOR_START + r"(?:" + "|".join(_ANCHOR_WORDS + _LONG_VALUE_ANCHOR_WORDS)
    + r"|(?P<vendorkey>" + "|".join(_VENDOR_KEY_WORDS) + r"))"
    r"(?:[_\-.]?(?:[0-9]{1,3}|base))?$"
)
#: A netrc entry: `machine <host> [login <u>] [account <a>] [port <p>] password <x>`,
#: or the `default login <u> password <x>` entry, across any whitespace (netrc
#: allows one entry over several lines).
_NETRC_PASSWORD = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:machine\s+\S{1,253}|default(?=\s+login\s))"
    r"(?:\s+(?:login|account|port)\s+\S{1,256}){0,3}"
    r"\s+password\s+(?P<value>\S{1,512})"
)
_NETRC_VALUE_END = re.compile(r"\\[nrt]|[\"'`]")
#: A netrc entry with no `login` field needs a value with a digit or a symbol:
#: without one, `machine gpu01 password reset` is a sentence.
_NETRC_LOGIN = re.compile(r"(?i)\slogin\s")
#: `<password>...</password>` (Maven `settings.xml`, many XML configs).
_XML_PASSWORD = re.compile(r"(?i)<password>\s*(?P<value>[^<\s][^<]{0,510})</password>")
#: A registry `auth` value in a docker config: base64 of `user:password`. JSON,
#: single-quoted (a Python dict), or a bare YAML key.
_DOCKER_AUTH = re.compile(
    r"(?<![A-Za-z0-9_])[\"']?auth[\"']?:[ \t]*[\"']?(?P<value>[A-Za-z0-9+/]{8,4096}={0,2})(?![A-Za-z0-9+/=])"
)
#: A registry `identitytoken` (docker `config.json`, an ACR / ECR login).
_DOCKER_IDENTITY_TOKEN = re.compile(
    r"(?i)(?<![A-Za-z0-9_])[\"']?identitytoken[\"']?[ \t]*:[ \t]*[\"'](?P<value>[A-Za-z0-9._~+/=\-]{20,4096})[\"']"
)
#: An npm `_auth=` line (`.npmrc`, optionally scoped to a registry): base64 of
#: `user:password`.
_NPMRC_AUTH = re.compile(
    r"(?m)^[ \t]*(?://\S{1,253}:)?_auth[ \t]*=[ \t]*[\"']?(?P<value>[A-Za-z0-9+/]{8,4096}={0,2})(?![A-Za-z0-9+/=])"
)
#: A 40-hex `key:` line under a `wandb:` block (Hydra / Lightning configs):
#: the key is formless, so the block it sits in is the anchor.
_YAML_WANDB_KEY = re.compile(
    r"(?im)^[ \t]*(?:api[_-]?)?key[ \t]*:[ \t]*[\"']?(?P<value>[0-9a-f]{40})(?![0-9A-Za-z])"
)
#: How far above a `key:` line its `wandb:` block may open.
_WANDB_BLOCK_REACH = 400


#: A docker `auth` secret that only names what goes there (`username:password`).
_AUTH_PLACEHOLDERS = frozenset({"password", "pass", "passwd", "pwd", "secret", "token", "pat"})


def _docker_auth_decodes(value: str) -> bool:
    """Whether a docker `auth` value is base64 of printable `user:password`,
    with a password that is not a placeholder."""
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return False
    user, sep, secret = decoded.partition(":")
    if not (sep and user and secret and decoded.isprintable()):
        return False
    return secret.lower() not in _AUTH_PLACEHOLDERS and not _is_indirect(secret) and not _is_slot(secret)
from .credential_scan import _is_indirect  # noqa: E402 -- pieces need each other; the front door loads this one first
from .values import _is_slot  # noqa: E402 -- pieces need each other; the front door loads this one first
