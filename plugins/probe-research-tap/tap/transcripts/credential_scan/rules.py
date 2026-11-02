"""the structured rules: vendor key shapes and the entropy floors they are held to."""

from __future__ import annotations
import re
from dataclasses import dataclass

#: Chars that can appear inside an opaque credential value.
_SECRET_CHARS = r"A-Za-z0-9+/=_\-"

#: How far from a structured hit a formless partner is still "the same
#: credential". An `aws configure` echo puts the two on consecutive lines; a
#: `printf` writing a credentials file puts them in adjacent quoted args.
_PAIR_WINDOW = 400

#: Shannon entropy floor for a value a KEY NAME introduced. The anchor is
#: strong evidence on its own, so the bar here is about excluding obvious prose.
_ENTROPY_MIN = 3.5

#: Higher floor for PAIR promotion, which has no key name behind it — only
#: "something credential-shaped matched nearby". On 4,000 real production
#: chunks the single false positive at 3.5 was a hyphenated identifier
#: (`attention-ablation-config`, H=3.59) sitting near a document that discussed
#: AWS keys. A real 40-character secret measures well above 5.
_PAIR_ENTROPY_MIN = 4.0

#: Pair promotion also requires MIXED character classes. An opaque credential
#: interleaves cases and digits; an English identifier does not.
_PAIR_MIN_CLASSES = 2

#: Shortest value worth testing for entropy. Below this, ordinary identifiers
#: dominate and the measure says nothing.
_ENTROPY_MIN_LEN = 20

#: Longest span we will consider one value. Beyond this it is a blob, not a
#: credential, and scanning it is wasted work.
_ENTROPY_MAX_LEN = 512


@dataclass(frozen=True)
class _Rule:
    name: str
    pattern: re.Pattern[str]
    #: Lowercase substrings; the rule is skipped unless one is in the text.
    #: Empty means "always consider" — reserved for shapes with no literal.
    keywords: tuple[str, ...]
    #: True when this rule's match implies a formless partner nearby.
    pairs_with_entropy: bool = False


def _r(name: str, pattern: str, keywords: tuple[str, ...], *, pairs: bool = False) -> _Rule:
    return _Rule(name, re.compile(pattern), keywords, pairs)


# ---------------------------------------------------------------------------
# Structured rules — vendor-defined shapes only.
#
# Every pattern here is linear: no nested unbounded quantifier, no alternation
# inside a repetition. `test_secrets.py::test_no_rule_is_pathological` pins it.
# ---------------------------------------------------------------------------

_RULES: tuple[_Rule, ...] = (
    # AWS. The access key id is structured; its secret is not, which is why
    # this rule sets pairs=True.
    _r("aws-access-key-id", r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b",
       ("akia", "asia", "abia", "acca"), pairs=True),
    # Anthropic: API and admin keys, and the OAuth access/refresh tokens a
    # Claude Code login writes (`sk-ant-oat01-`, `sk-ant-ort01-`).
    _r("anthropic-api-key", r"\bsk-ant-(?:api|admin|oat|ort)[0-9]{2}-[A-Za-z0-9_\-]{40,200}\b",
       ("sk-ant-",)),
    # Probe's own credentials (app/auth/tokens.py): user PATs (and the legacy
    # `ros_pat_`), service tokens, ingest tokens. Fixed prefix + hex, so exact.
    # Ingest tokens come in two lengths: pairing mints 48 hex, `probe login`'s
    # device flow 32 (app/auth/device_router.py). The anchored pass cannot catch
    # either, because `ros` and `ing` make the value read as word-like.
    _r("probe-token", r"\b(?:probe_pat_|ros_pat_|probe_svc_)[0-9a-f]{32}\b",
       ("probe_pat_", "ros_pat_", "probe_svc_")),
    _r("probe-ingest-token", r"\bros_ing_[0-9a-f]{32}(?:[0-9a-f]{16})?\b", ("ros_ing_",)),
    # Per-run tokens (app/auth/run_tokens.py): `probe_run_` + an ES256 JWT, the
    # credential `probe exec` prints for a remote job. One run only, but still a
    # credential: bounded tails, so the accelerated scan never runs open.
    _r("probe-run-token",
       r"\bprobe_run_[A-Za-z0-9_\-]{10,200}\.[A-Za-z0-9_\-]{10,4000}\.[A-Za-z0-9_\-]{20,200}",
       ("probe_run_",)),
    # OpenAI, both the project-scoped and the classic shape.
    _r("openai-api-key", r"\bsk-(?:proj|svcacct|admin)-[A-Za-z0-9_\-]{40,200}\b",
       ("sk-proj-", "sk-svcacct-", "sk-admin-")),
    _r("openai-api-key-classic", r"\bsk-[A-Za-z0-9]{20}T3BlbkFJ[A-Za-z0-9]{20}\b",
       ("t3blbkfj",)),
    # GitHub.
    _r("github-token", r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b",
       ("ghp_", "gho_", "ghu_", "ghs_", "ghr_")),
    _r("github-fine-grained-pat", r"\bgithub_pat_[A-Za-z0-9_]{60,90}\b",
       ("github_pat_",)),
    # Hugging Face.
    _r("huggingface-token", r"\bhf_[A-Za-z]{34}\b", ("hf_",)),
    # Capy (capy.ai) API key: `capy_` + 43 characters, org-wide with its
    # owner's full role (no scopes).
    _r("capy-api-key", r"\bcapy_[A-Za-z0-9_\-]{43}(?![A-Za-z0-9_\-])", ("capy_",)),
    # Slack.
    _r("slack-token", r"\bxox[baprse]-[A-Za-z0-9\-]{10,250}\b", ("xox",)),
    _r("slack-webhook", r"https://hooks\.slack\.com/services/[A-Za-z0-9/]{20,120}",
       ("hooks.slack.com",)),
    # Google.
    _r("gcp-api-key", r"\bAIza[0-9A-Za-z_\-]{35}\b", ("aiza",)),
    # Stripe live keys only; test keys are not credentials worth redacting.
    _r("stripe-live-key", r"\b(?:sk|rk)_live_[A-Za-z0-9]{20,60}\b", ("_live_",)),
    # GitLab.
    _r("gitlab-pat", r"\bglpat-[A-Za-z0-9_\-]{20,50}\b", ("glpat-",)),
    # npm.
    _r("npm-token", r"\bnpm_[A-Za-z0-9]{36}\b", ("npm_",)),
    # Weights & Biases keys are 40 hex with no prefix, so they are ONLY safe to
    # match next to the literal `wandb` — a bare 40-hex rule is a git-sha rule.
    _r("wandb-key", r"(?i)\bwandb(?:[_ -]?(?:api[_ -]?)?key[\"']?\s*[:=]\s*|\s+login\s+|\s*[:=]\s*|\.login\(\s*key\s*=\s*)[\"']?[0-9a-f]{40}\b", ("wandb",)),
    # A private key block. The body requirement stops a bare header comment
    # from matching.
    _r("private-key-block",
       r"-----BEGIN(?:[ A-Z0-9]{0,30})PRIVATE KEY(?: BLOCK)?-----[^-]{16,32768}?-----END(?:[ A-Z0-9]{0,30})PRIVATE KEY(?: BLOCK)?-----",
       ("-----begin",)),
    # JSON Web Token: three base64url segments, the first two JSON-shaped.
    _r("jwt", r"\beyJ[A-Za-z0-9_\-]{8,2000}\.eyJ[A-Za-z0-9_\-]{8,2000}\.[A-Za-z0-9_\-]{8,2000}",
       ("eyj",)),
    # An Authorization header carrying a real value.
    _r("bearer-token", r"(?i)authorization[\"']?\s*[:=]\s*[\"']?bearer\s+[A-Za-z0-9._~+/=\-]{8,4096}",
       ("authorization",)),
    _r("basic-auth", r"(?i)authorization[\"']?\s*[:=]\s*[\"']?basic\s+[A-Za-z0-9+/=]{8,4096}",
       ("authorization",)),
    # user:password@host in a URI. Up to 64 characters the password may hold
    # anything but `/`, `@` and whitespace, as before. Longer ones -- AWS
    # CodeArtifact and GCP Artifact Registry (`oauth2accesstoken:ya29...`) index
    # URLs carry a 1 KB-class token there -- only in TOKEN characters: with `"`,
    # `,` and `:` allowed that far, ordinary JSON matched from a
    # `"https://..."` value to the `@` of an email address keys later
    # (`{"source":"https://huggingface.co",...,"reviewer":"alice@lab.org"}`).
    _r("credential-uri",
       r"\b[a-z][a-z0-9+.\-]{1,20}://[^/@\s:]{1,64}:(?:[^/@\s]{3,64}|[A-Za-z0-9._~+=%\-]{65,2048})@",
       ("://",)),
)
