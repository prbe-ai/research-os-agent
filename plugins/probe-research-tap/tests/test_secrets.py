"""Credential detection, span redaction, and the wiring that applies it.

THE FALSE-POSITIVE CORPUS IS THE POINT OF THIS FILE.

The previous gate (`app/ingestion/redaction.py`, removed 0.104.9.0) dropped 645
batches across 206 sessions; 615 of them — 95% — were false positives, and
because a finding wrote a `session_quarantines` tombstone that refused every
LATER batch, each one erased a whole transcript permanently and silently.

`FALSE_POSITIVES` below is that outage, encoded. The two paths with their
measured entropies are verbatim from PR #365's postmortem. Any change to a rule
replays them. A rule that fires on one of these does not ship.

Every "credential" in `TRUE_POSITIVES` is synthetic — random, or AWS's own
published documentation example. No live secret appears in this repository.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import time

import pytest

from tap import secrets
from tap.codex_sanitize import sanitize_event as codex_sanitize
from tap.sanitize import sanitize_event as cc_sanitize
from tap.transcript import build_batch_body

# ---------------------------------------------------------------------------
# Corpora
# ---------------------------------------------------------------------------

#: Synthetic credential fixtures, ASSEMBLED AT RUNTIME rather than written as
#: literals. GitHub push protection blocks a commit containing an AKIA-shaped
#: string even when it is invented, and it is right to — a scanner that trusts
#: "this one is a test" is a scanner you cannot rely on. None of these values
#: is or has ever been a real key.
_AWS_ID = "AKIA" + "4KX7QZJ2MNVB3TWD"
_AWS_SECRET = "hT7xQ2mVb9Lk" + "Zp0RwYe4Ns6Uc1Ai8Jd3Fg5Oh2Pq"
_GH_PAT = "ghp_" + "16C7e42F292c6912E7710c838347Ae178B4a"
_GCP_KEY = "AIza" + "SyC1x9Kp0RwYe4Ns6Uc1Ai8Jd3Fg5Oh2Pq7"
_HF = "hf_" + "QZJmNVbTWDxKpLwRyEeNsUcAiJdFgOhqQz"
_SLACK = "xoxb-" + "123456789012-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx"
#: These two are split mid-PREFIX, not just before the body. GitHub's OpenAI
#: detector matches on the `sk-proj-` + `T3BlbkFJ` marker alone, so writing
#: that marker as one literal is enough to block a push even with a synthetic
#: body — which is exactly how this file broke the public mirror on 2026-09-12.
_ANTHROPIC = "sk-" + "ant-" + "api03-" + "a" * 80 + "9xQ"
_OPENAI = "sk-" + "proj-" + "T3Blb" + "kFJ" + "b" * 50
#: Probe's own ingest tokens, both lengths the server mints: `probe login`'s
#: device flow cuts an HMAC to 32 hex, pairing draws 48. Only the 48 was
#: caught, and `ros`/`ing` made the 32 read as word-like to the anchored pass,
#: so a printed `~/.config/probe/config.json` shipped a live ingest token.
_PROBE_INGEST_LOGIN = "ros_" + "ing_" + "0f3a9c7e" * 4
_PROBE_INGEST_PAIRED = "ros_" + "ing_" + "0f3a9c7e" * 6

_HEX32 = "5e8a1c9f3b7d2e4a" + "6c0f9b1d3e5a7c2f"
_HEX40 = _HEX32 + "8d4b6e1a"
_PW = "Tr0ub4" + "dor&3xQ"
_LONG_PW = "Zq7Lm2" * 12
_YA29 = "ya29." + "a0AfB_by" + "Xk9-Lm2_Qp7" * 16
_DOCKER_AUTH = "bWU6" + "VHIwdWI0ZG9yJjN4UQ=="  # base64 of me:<_PW>
# Generated passwords with letters and digits only: how most tools make them.
# `_PW`'s `&` let the first #2000 rules pass on shapes that still leaked these.
_ALNUM_PW = "a8Kd93" + "jLm2Qx"
_ALNUM_PW2 = "S3cr3t" + "Passw0rd"
_HEX_PW = "e3b0c442" + "98fc1c14"  # `openssl rand -hex 8`

#: Must be caught. Synthetic values only.
TRUE_POSITIVES: tuple[tuple[str, str], ...] = (
    # The two shapes that actually leaked (Anthrogen, 2026-08-30).
    ("aws_configure_echo_id", f"AWS Access Key ID [None]: {_AWS_ID}"),
    ("aws_configure_echo_secret", f"AWS Secret Access Key [None]: {_AWS_SECRET}"),
    (
        "aws_printf_pair",
        "printf '[default]\\naws_access_key_id = %s\\naws_secret_access_key = %s\\n' "
        f"'{_AWS_ID}' '{_AWS_SECRET}'",
    ),
    ("aws_ini_secret", f"aws_secret_access_key = {_AWS_SECRET}"),
    ("aws_export", f"export AWS_SECRET_ACCESS_KEY={_AWS_SECRET}"),
    # The one literal that stays: AWS publishes this exact string in its own
    # documentation, every scanner allowlists it on the "EXAMPLE" substring,
    # and it is worth asserting we still catch the canonical shape.
    ("aws_doc_example", "AKIAIOSFODNN7EXAMPLE"),
    ("github_pat", _GH_PAT),
    ("anthropic", _ANTHROPIC),
    ("openai_proj", _OPENAI),
    ("huggingface", _HF),
    ("slack_bot", _SLACK),
    ("gcp_api_key", _GCP_KEY),
    ("bearer_header", "Authorization: Bearer abc123XYZdef456GHIjkl789MNO"),
    ("credential_uri", "postgres://admin:s3cr3tP4ssw0rd@db.internal:5432/probe"),
    ("probe_ingest_device_login", f"'ingest_token': '{_PROBE_INGEST_LOGIN}'"),
    ("probe_ingest_paired", f"PROBE_INGEST_TOKEN={_PROBE_INGEST_PAIRED}"),
    # --- #2000 re-review: shapes the rules did not see (2026-09-27). ---
    # H3: camelCase key names.
    ("camel_ts_api_key", f'const openaiApiKey = "{_HEX32}";'),
    ("camel_helm_password", f"postgresql:\n  auth:\n    postgresPassword: {_PW}"),
    ("camel_wandb_api_key", f"wandbApiKey: {_HEX40}"),
    ("camel_maven_password", f"mavenPassword={_PW}"),
    # M3: string prefixes, and defaults a program falls back to.
    ("fstring_prefix", f'API_KEY = f"{_HEX32}"'),
    ("bytes_prefix", f"SECRET = rb'{_HEX32}'"),
    ("environ_setdefault", f'os.environ.setdefault("WANDB_API_KEY", "{_HEX40}")'),
    ("getenv_default", f'token = os.getenv("HF_TOKEN", "{_HEX32}")'),
    # M2: `*_KEY` names and a `key:` under a `wandb:` block.
    ("env_star_key", f"AZURE_OPENAI_KEY={_HEX32}"),
    ("signing_key", f'SIGNING_KEY = "{_HEX40}"'),
    ("encryption_key", f"encryption_key: {_HEX40}"),
    ("yaml_wandb_key", f"wandb:\n  project: odyssey\n  key: {_HEX40}"),
    # M5: shapes with no `key = value` separator.
    ("netrc_entry", f"machine api.wandb.ai\n  login user\n  password {_HEX40}"),
    ("netrc_one_line", f"machine github.com login bot password {_PW}"),
    ("maven_password", f"<server><id>releases</id><password>{_PW}</password></server>"),
    ("docker_auth", '{"auths": {"ghcr.io": {"auth": "' + _DOCKER_AUTH + '"}}}'),
    # M6: a URL password longer than 64 characters (CodeArtifact, GCP).
    ("uri_long_password", f"postgresql://app:{_LONG_PW}@db.internal:5432/app"),
    ("uri_ya29", f"--extra-index-url https://oauth2accesstoken:{_YA29}@us-python.pkg.dev/p/r/simple/"),
)

#: The #2000 security review (2026-09-27): shapes that still leaked, as
#: `(name, text, secret)`. The SECRET must be gone, not just some span.
REVIEW3_LEAKS: tuple[tuple[str, str, str], ...] = (
    # HIGH-1: camelCase passwords of letters and digits only.
    ("camel_helm_alnum", f"postgresql:\n  auth:\n    postgresPassword: {_ALNUM_PW}\n", _ALNUM_PW),
    ("camel_helm_quoted_alnum", f'postgresql:\n  auth:\n    postgresPassword: "{_ALNUM_PW2}"\n', _ALNUM_PW2),
    ("camel_ts_quoted_alnum", f'const dbPassword = "{_ALNUM_PW2}";', _ALNUM_PW2),
    ("camel_gradle_alnum", f"mavenPassword={_ALNUM_PW}", _ALNUM_PW),
    ("camel_json_alnum", f'{{"dbPassword": "{_ALNUM_PW2}"}}', _ALNUM_PW2),
    ("camel_helm_hex", f"redis:\n  auth:\n    redisPassword: {_HEX_PW}\n", _HEX_PW),
    ("camel_ts_typed", f'private dbPassword: string = "{_ALNUM_PW2}";', _ALNUM_PW2),
    # MED-1: key names the rules did not read.
    ("lower_vendor_key_yaml", f"model:\n  azure_openai_key: {_HEX32}\n", _HEX32),
    ("lower_vendor_key_py", f'openai_key = "{_HEX32}"', _HEX32),
    ("wandb_key_yaml", f"logging:\n  wandb_key: {_HEX40}\n", _HEX40),
    ("camel_short_secret", f'export const jwtSecret = "{_PW}";', _PW),
    ("camel_vendor_key", f'const openaiKey = "{_HEX32}";', _HEX32),
    ("camel_acronym_vendor_key", f'const azureOpenAIKey = "{_HEX32}";', _HEX32),
    ("acronym_db_password", f'{{"ConnectionStrings": {{}}, "DBPassword": "{_PW}"}}', _PW),
    ("acronym_smtp_password", f'SMTPPassword = "{_PW}"', _PW),
    ("acronym_jwt_secret", f'const JWTSecret = "{_HEX32}";', _HEX32),
    ("camel_short_hex_secret", f"facebook:\n  appSecret: {_HEX_PW}\n", _HEX_PW),
    ("getenv_default_keyword", f'key = os.getenv("WANDB_API_KEY", default="{_HEX40}")', _HEX40),
    ("environ_get_or", f'key = os.environ.get("OPENAI_API_KEY") or "{_HEX32}"', _HEX32),
    ("js_env_or", f'const key = process.env.AZURE_OPENAI_KEY || "{_HEX32}";', _HEX32),
    ("js_env_nullish", f'const apiKey = process.env["OPENAI_API_KEY"] ?? "{_HEX32}";', _HEX32),
    ("argparse_default", f'p.add_argument("--wandb-api-key", default="{_HEX40}")', _HEX40),
    ("argparse_default_multiline", f'p.add_argument(\n    "--wandb-key",\n    default="{_HEX40}",\n)', _HEX40),
    ("click_default", f'@click.option("--token", default="{_HEX40}")', _HEX40),
    ("k8s_env_pair", f"env:\n- name: AZURE_OPENAI_KEY\n  value: {_HEX32}\n", _HEX32),
    ("k8s_env_pair_quoted", f'env:\n  - name: DB_PASSWORD\n    value: "{_ALNUM_PW}"\n', _ALNUM_PW),
    # LOW-2 shapes that were clean to add.
    ("dockerfile_env_space", f"ENV AZURE_OPENAI_KEY {_HEX32}", _HEX32),
    ("netrc_default_entry", f"default login user password {_HEX40}", _HEX40),
    ("docker_auth_single_quoted", "CFG = {'auths': {'r.io': {'auth': '" + _DOCKER_AUTH + "'}}}", _DOCKER_AUTH),
    ("docker_auth_yaml", 'auth: "' + _DOCKER_AUTH + '"', _DOCKER_AUTH),
    ("docker_identitytoken", '{"auths": {"x.azurecr.io": {"identitytoken": "eyJhbGciOiJSUzI1NiJ9' + _HEX40 + '"}}}', _HEX40),
    ("npmrc_auth", "_auth=" + _DOCKER_AUTH, _DOCKER_AUTH),
    ("gradle_password_line", f'credentials {{\n  username "u"\n  password "{_PW}"\n}}', _PW),
    # HIGH-2's rule still takes a long token password.
    ("uri_long_token_password", f"--index-url https://aws:{_YA29}@x.d.codeartifact.us-east-1.amazonaws.com/pypi/r/simple/", _YA29),
)


@pytest.mark.parametrize("name,text,secret", REVIEW3_LEAKS, ids=[n for n, _, _ in REVIEW3_LEAKS])
def test_review3_shape_is_redacted(name: str, text: str, secret: str) -> None:
    redacted, rules = secrets.redact(text)
    assert rules, f"{name}: no rule fired"
    assert secret not in redacted, f"{name}: the secret survived: {redacted!r}"


#: Benign shapes the first #2000 rules rewrote (review, MED-2 / HIGH-2): bytes
#: must come back unchanged.
REVIEW3_BENIGN: tuple[tuple[str, str], ...] = (
    # HIGH-2: ordinary JSON, from an `https://` value to an email's `@`.
    ("json_url_then_email", '{"source":"https://huggingface.co","id":"a1b2c3","n_tokens":123456,'
     '"label":"positive","split":"train","annotator":"worker-17","score":0.9321,'
     '"created":"2026-09-01T12:00:00Z","reviewer":"alice@lab.org"}'),
    ("json_short_url_then_email", '{"url":"https://hf.co/datasets/x","by":"alice@lab.org"}'),
    # MED-2: template slots and shell variables are not values.
    ("xml_password_slot", "<server><id>c</id><username>{user}</username><password>{password}</password></server>"),
    ("xml_password_ellipsis", "Put it in `~/.m2/settings.xml` as `<password>...</password>`."),
    ("xml_password_jinja", "<password>{{ maven_password }}</password>"),
    ("netrc_shell_variable", 'printf "machine github.com login ci password $GH_TOKEN\\n" > ~/.netrc'),
    ("config_get_secret_name", 'we read cfg.get("secret_name", "prod-db-2") at boot'),
    ("setdefault_token_setting", 'claims.setdefault("id_token_encrypted_response_enc", "A128CBC-HS256")'),
    ("camel_request_token_uuid", '"ClientRequestToken": "a8f5f167-f44f-4964-a6c9-8f1b2d3e4a5b",'),
    ("ws_key_guid", 'WS_KEY: Final[bytes] = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"'),
    ("docker_auth_placeholder", '"auth": "dXNlcm5hbWU6cGFzc3dvcmQ="'),  # username:password
    ("fstring_password_slot", 'repair_args += [f"-sPDFPassword={password}"]'),
    # HIGH-1's fix keeps these clean: code, prose and generated API clients.
    ("camel_messages_field", "class Db:\n    adminPassword = _messages.StringField(1)"),
    ("camel_dotted_value", "const userPassword = form.password.value;"),
    ("camel_snake_identifier", "'MasterUserPassword': master_user_password,"),
    ("camel_prose_value", "- **Rotate** the helm `postgresPassword: see 1Password item db-prod` after each restore."),
    ("camel_help_text", "oauth2ClientSecret: OAuth2 client secret to use for the authentication"),
    ("camel_quoted_help", '"pvkPassword": "Private key password 1"'),
    ("camel_quoted_word", '"dataStorePassword": "dataStoreTestQuery",'),
    ("camel_class_def", "class RSAPrivateKey(metaclass=abc.ABCMeta):"),
    ("camel_def_default", "def AddUserPassword(parser, required=False):"),
    ("camel_subscript", "challengePassword['type'] = pkcs_9_at_challengePassword"),
    ("camel_type_on_next_line", "RSAPrivateKey:\n     version=0"),
    ("camel_method_type", "async function getBearerToken(): Promise<string | null> {"),
    ("pagination_token", '"NextToken": "CpHNsscimcV5oH7bSbub03CI2Qms5+ypNpNm+53MNlR0YcXAkp0xFlfKf91yVx",'),
    ("public_key_name", '{"LANGFUSE_PUBLIC_KEY": "kFiKa1VZukMmD8RB6WXB9F"}'),
    ("unowned_lower_key", 'cache_key = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4"'),
    ("unowned_camel_key", 'const sortKey = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4";'),
    ("raw_regex_secret_name", "_SECRET_NAME_PARTIAL = r'(?P<secret>[a-zA-Z0-9-_]{1,255})'"),
    ("raw_regex_token", "token = r\"[-!#$%&'*+.^_`|~0-9a-zA-Z]+\""),
    ("k8s_env_reference_name", "env:\n- name: SECRET_NAME\n  value: prod-db-2\n"),
    ("dockerfile_env_path", "ENV TOKEN_PATH /run/secrets/token2"),
    ("netrc_prose_default", "the default password for the image is documented below"),
)


@pytest.mark.parametrize("name,text", REVIEW3_BENIGN, ids=[n for n, _ in REVIEW3_BENIGN])
def test_review3_benign_shape_is_unchanged(name: str, text: str) -> None:
    redacted, rules = secrets.redact(text)
    assert rules == [] and redacted == text, f"{name}: fired {rules}: {redacted!r}"


#: The #2034 re-review (2026-09-27), `(name, text, secret)`: `*_KEY` names that
#: end in an ordinary anchor lost it to the new `*_KEY` rule (caught on main),
#: and the controls that keep the fixes below narrow.
REVIEW4_LEAKS: tuple[tuple[str, str, str], ...] = (
    ("star_key_api_key_letters", 'OPENAI_API_KEY = "abcdefghijklmnopqrstuvwxyzabcdef"', "abcdefghijklmnopqrstuvwxyzabcdef"),
    ("star_key_secret_key_letters", 'DJANGO_SECRET_KEY = "qwertyuiopasdfghjklzxcvbnmqwertyuiopasdf"', "qwertyuiopasdfghjklzxcvbnmqwertyuiopasdf"),
    ("star_key_private_key_letters", 'GPG_PRIVATE_KEY = "qwertyuiopasdfghjklzxcvbnmqwertyuiopasdf"', "qwertyuiopasdfghjklzxcvbnmqwertyuiopasdf"),
    # Controls: a camelCase `...Secret` whose word is not a reference, a
    # spaced camel value WITH a digit, a netrc entry with `login` and a plain
    # word, one without `login` and a generated value, a `$`-first password.
    ("camel_secret_not_a_reference", "dbSecret: pg-auth-v2x9", "pg-auth-v2x9"),
    ("camel_spaced_with_digit", 'adminPassword: "Tr0ub4dor 3xQ"', "Tr0ub4dor 3xQ"),
    ("netrc_login_plain_word", "machine h.example.com login u password hunter", "hunter"),
    ("netrc_no_login_generated", f"machine gpu01 password {_PW}", _PW),
    ("dsl_password_dollar_first", 'credentials {\n  password "$ecretPa55"\n}', "$ecretPa55"),
)


@pytest.mark.parametrize("name,text,secret", REVIEW4_LEAKS, ids=[n for n, _, _ in REVIEW4_LEAKS])
def test_review4_shape_is_redacted(name: str, text: str, secret: str) -> None:
    redacted, rules = secrets.redact(text)
    assert rules, f"{name}: no rule fired"
    assert secret not in redacted, f"{name}: the secret survived: {redacted!r}"


#: The #2034 re-review's false positives: UI labels under camelCase keys (a
#: space was a symbol), names of secret OBJECTS, netrc words in prose, and
#: lower-case variables. Bytes must come back unchanged.
REVIEW4_BENIGN: tuple[tuple[str, str], ...] = (
    ("label_forgot_password", '{\n  "forgotPassword": "Forgot password?",\n  "email": "Email"\n}'),
    ("label_confirm_password", '{\n  "confirmPassword": "Confirm password",\n  "email": "Email"\n}'),
    ("label_ts_new_password", 'export const labels = {\n  newPassword: "New password",\n  email: "Email",\n};'),
    ("label_german", '{"forgotPassword": "Passwort vergessen?"}'),
    ("label_must_match", 'const errors = { confirmPassword: "Must match!" };'),
    ("label_yaml_unquoted", "auth:\n  forgotPassword: Forgot password?\n  resetPassword: \"Reset password\""),
    ("java_not_applicable", 'String newPassword = "n/a";'),
    ("helm_existing_secret", "auth:\n  existingSecret: pg-auth-v2"),
    ("helm_image_pull_secret", "global:\n  imagePullSecret: regcred-v1"),
    ("helm_tls_secret", "ingress:\n  tlsSecret: ingress-tls2"),
    ("netrc_prose", "Use the machine gpu01 password reset flow."),
    ("netrc_prose_period", "Log in to machine gpu01 password reset."),
    ("gradle_lower_variable", 'credentials {\n  username "$mavenUser"\n  password "$mavenPassword"\n}'),
    ("netrc_echo_lower_variable", 'echo "machine github.com login x password $github_token" > ~/.netrc'),
)


@pytest.mark.parametrize("name,text", REVIEW4_BENIGN, ids=[n for n, _ in REVIEW4_BENIGN])
def test_review4_benign_shape_is_unchanged(name: str, text: str) -> None:
    redacted, rules = secrets.redact(text)
    assert rules == [] and redacted == text, f"{name}: fired {rules}: {redacted!r}"


@pytest.mark.parametrize(
    "unit", ["dbPassword=a8Kd93jLm2Qx,", "dbPassword=a8Kd93jLm2Qx "], ids=["camel-comma", "camel-space"]
)
def test_one_long_line_is_linear_when_read_whole(unit: str) -> None:
    """Code capture and the server read a text WHOLE (an accelerator, no 64K
    windows). A camelCase value's check read to the end of its line per match,
    so one 2 MB line took 18-29 s (#2034 re-review; 1 MB took a quarter of
    that). The check now reads a bounded stretch: ~1 s here; the ceiling leaves
    room for a slow CI machine and still fails the old code."""
    text = (unit * (2_000_000 // len(unit) + 1))[:2_000_000]
    started = time.perf_counter()
    secrets.scan(text, _accel=secrets._keyword_windows)
    elapsed = time.perf_counter() - started
    assert elapsed < 8.0, f"{unit!r}: {elapsed:.2f}s for one 2 MB line"


#: Must NOT be caught. The first four are verbatim from the outage.
FALSE_POSITIVES: tuple[tuple[str, str], ...] = (
    # PR #365, 539 drops: any 40-character path satisfied the base64 window.
    ("outage_path_casp", "/OdysseyPrivate/odyssey/experiments/casp"),  # H=3.85
    ("outage_path_fsq", "/workspace/library/checkpoints/fsq/FINAL"),  # H=4.38
    # PR #365, 76 drops: ordinary pipeline stdout read as a pasted .env.
    (
        "outage_env_sha",
        "SLICE_SHA256=9f2c1ab44e3d8071b5c6e2f9a0d4738b1c5e6f7a8b9c0d1e2f3a4b5c6d7e8f90",
    ),
    ("outage_env_bytes", "SLICE_BYTES=48210347"),
    ("outage_env_n", "EVAL_N=1024"),
    # Ordinary ML-transcript noise.
    ("path_long", "/workspace/shyam/runs/2026-08-30/checkpoints/step_48000"),
    (
        "path_safetensors",
        "loading /workspace/odyssey/checkpoints/fsq_v3/step_412000/model.safetensors",
    ),
    ("git_sha", "commit 03a24b90bee1c7341cc4714a5ca21850f8a9a91c"),
    ("uuid", "session 4525087c-392d-4acf-b221-d861512fb467"),
    ("wandb_run_dir", "wandb: Run data is saved locally in wandb/run-20260830_152500-a7k3m9qz"),
    (
        "content_hash",
        "content_hash = e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    ),
    ("hex_digest", "sha256:9f2c1ab44e3d8071b5c6e2f9a0d4738b1c5e6f7a8b9c0d1e2f3a4b5c6d7e8f90"),
    ("torch_shape", "tensor shape torch.Size([32, 1024, 4096]) dtype=torch.bfloat16"),
    ("s3_uri", "s5cmd ls s3://runpod-files-new/checkpoints/odyssey3/"),
    ("pip_wheel", "Downloading torch-2.9.1+cu128-cp313-cp313-linux_x86_64.whl (912.4 MB)"),
    ("pytest_line", "tests/integration/test_generation_worker.py::test_claims_one_row PASSED"),
    ("base64_not_jwt", "payload = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9payloadonly'"),
    # References and placeholders are not credentials.
    ("env_reference", "model = 'claude-opus-5' ; api_key = os.environ['ANTHROPIC_API_KEY']"),
    ("env_reference_aws", "aws_secret_access_key = os.environ['AWS_SECRET_ACCESS_KEY']"),
    ("placeholder_angle", "aws_secret_access_key = <your-secret-here>"),
    ("yaml_interpolation", "  api_key: ${ANTHROPIC_API_KEY}"),
    # An anchor word inside an ordinary English sentence.
    ("secretary", "the secretary said: meeting at 1400 hours in room 12"),
    # --- Found by scanning 4,000 REAL production chunks (2026-09-12). ---
    # Every one of these fired before the `_is_word_like` filter landed. They
    # are the same failure as the 2026-08 outage wearing a different coat: a
    # path or an identifier sitting close enough to a credential word to be
    # taken for a value. Keep them; they are the only evidence we have that the
    # detector survives contact with real transcripts rather than a fixture.
    ("prod_cli_flag", "create the pull secret here:  --some-thing-SOMEEE-SOMEE-SOMEEEE-"),
    (
        "prod_path_after_anchor",
        'creates R2 credential secret for pods"}, {"path": "SomeThing/configs-abc-defg"',
    ),
    (
        "prod_flag_with_literal",
        ':  --from-literal=R2_ACCESS_KEY_ID="<key>" \\ 54:  --some-thing-K8-SOMEEEE-SOMEEE-ABC-',
    ),
    (
        "prod_test_assertion",
        "test_secret_syncs_before_app_secret - AssertionError: /some/path_with/parts-9a-bc9de",
    ),
    (
        "prod_hyphenated_ident",
        "loaded config `attention-ablation` (26) — the attention-ablation-config entry",
    ),
    # --- Controls for the #2000 rules: near misses that must stay quiet. ---
    ("camel_inside_word", "bypassword = 1; compassword: frobnicate"),
    ("sort_key", 'SORT_KEY = "created_at"'),
    ("primary_key", "PRIMARY_KEY = 'user_id'"),
    ("fstring_template_key", 'CACHE_KEY = f"user:{uid}:profile"'),
    ("max_tokens_default", 'n = cfg.get("max_tokens", "1024")'),
    ("tokenizer_default", 'name = os.environ.get("TOKENIZER", "bert-base-uncased")'),
    ("empty_default", 'tok = os.getenv("HF_TOKEN", "")'),
    ("none_default", 'key = os.environ.get("OPENAI_API_KEY", "none")'),
    ("prose_machine_password", "Log into the machine with your password first, then run make."),
    ("maven_env_reference", "<password>${env.MAVEN_PASSWORD}</password>"),
    ("docker_auth_word", '{"auth": "anonymous"}'),
    ("yaml_key_without_wandb", "cache:\n  key: " + "0" * 40),
    ("uri_placeholder_short", "postgres://user:pw@localhost/db"),
    # Found measuring the #2000 rules on this repo and prbe-knowledge.
    ("shell_capital_run", 'export PGUSER="$(cat /u)" PGPASSWORD="$(cat /superuser/password)"'),
    ("camel_call", "const cliToken = readProbeConfigMcpToken(env);"),
    ("camel_code_value", "const userPassword = form.password.value;"),
    ("header_name_key", "[ENGINE_INTERNAL_KEY: X-Internal-Knowledge-Key; see CONTRACT]"),
    ("fstring_template_bearer", 'MCP(url=url, authorization_token=f"Bearer {key}", id="probe")'),
    ("posthog_project_key", 'POSTHOG_KEY = "phc_' + "pCSs24bQtPaxoJ59PaTtTp" + 'JDS3dfzymfZeY74XQQ956K"'),
)


# ---------------------------------------------------------------------------
# The two corpora
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,text", TRUE_POSITIVES, ids=[n for n, _ in TRUE_POSITIVES])
def test_true_positive_is_redacted(name: str, text: str) -> None:
    redacted, rules = secrets.redact(text)
    assert rules, f"{name}: no rule fired"
    assert redacted != text, f"{name}: text unchanged despite a finding"
    assert "<redacted:" in redacted


def test_redacted_text_is_stable_under_a_second_scan() -> None:
    """A marker names its rule, and rule names hold anchor words: the "secret"
    in `<redacted:anchored-secret>` used to anchor the next value, one value per
    pass, redacting every sha256 in a JSON line eleven scans deep."""
    shas = [hashlib.sha256(str(i).encode()).hexdigest() for i in range(10)]
    line = json.dumps(
        {"token": "Zq8Wv6Ut4Sr2Po0Nm8Lk6Ab", **{f"sha{i}": v for i, v in enumerate(shas)}}
    )
    once, rules = secrets.redact(line)
    assert rules and secrets.redact(once) == (once, [])
    assert all(sha in once for sha in shas)


def test_a_real_key_after_a_marker_is_still_found() -> None:
    """Skipping a marker-anchored match resumes past the marker, so a real key
    name inside that skipped match's reach is still scanned."""
    value = "hT7xQ2mVb9LkZp0RwYe4Ns6Uc1Ai8Jd3Fg5Oh2Pq"
    text = f'"a": "<redacted:anchored-secret>", "api_key": "{value}"'
    redacted, rules = secrets.redact(text)
    assert rules == ["anchored-secret"] and value not in redacted


@pytest.mark.parametrize("token", [_PROBE_INGEST_LOGIN, _PROBE_INGEST_PAIRED])
def test_both_probe_ingest_token_lengths_are_removed_whole(token: str) -> None:
    dump = f"{{'contexts': {{'default': {{'ingest_token': '{token}'}}}}}}"
    redacted, rules = secrets.redact(dump)
    assert rules == ["probe-ingest-token"]
    assert token[len("ros_ing_") :] not in redacted


@pytest.mark.parametrize("name,text", FALSE_POSITIVES, ids=[n for n, _ in FALSE_POSITIVES])
def test_false_positive_does_not_fire(name: str, text: str) -> None:
    redacted, rules = secrets.redact(text)
    assert rules == [], f"{name}: fired {rules} on benign text — this is the 615-drop class"
    assert redacted == text, f"{name}: benign text was modified"


# ---------------------------------------------------------------------------
# NEGATIVE CONTROL
# ---------------------------------------------------------------------------


def test_suite_fails_when_the_detector_is_stubbed(monkeypatch) -> None:
    """A detector that silently returns nothing must not pass this file.

    Every other test here — shape preservation, the FP corpus, the envelope
    plumbing — passes against a `scan()` that does nothing at all. Without this
    test, breaking the detector looks exactly like the detector working.
    """
    monkeypatch.setattr(secrets, "scan", lambda _text: [])
    survived = []
    for name, text in TRUE_POSITIVES:
        redacted, rules = secrets.redact(text)
        if not rules and redacted == text:
            survived.append(name)
    assert len(survived) == len(TRUE_POSITIVES), (
        "stubbing scan() did not make every true positive survive; the corpus "
        "is not actually exercising the detector"
    )


# ---------------------------------------------------------------------------
# Detection mechanics
# ---------------------------------------------------------------------------


def test_structured_and_paired_halves_both_go() -> None:
    """The AWS access key id is structured; its secret is not.

    A per-rule policy redacts the harmless identifier and keeps the half that
    actually grants access. Pair promotion is what stops that.
    """
    text = f"aws_access_key_id = {_AWS_ID}\naws_secret_access_key = {_AWS_SECRET}\n"
    redacted, rules = secrets.redact(text)
    assert _AWS_ID not in redacted
    assert _AWS_SECRET not in redacted
    assert "aws-access-key-id" in rules


def test_entropy_is_never_consulted_alone() -> None:
    """A high-entropy token with no anchor and no partner is left alone.

    This is the invariant the last gate violated. A 40-character random string
    on its own is a checkpoint name as often as it is a credential.
    """
    lone = "artifact id 7Fq2Xb9LkZp0RwYe4Ns6Uc1Ai8Jd3Fg5Oh2PqRt"
    assert secrets.scan(lone) == []


def test_indirect_reference_is_not_a_credential() -> None:
    for value in (
        "api_key = os.environ['OPENAI_API_KEY']",
        "password = ${DB_PASSWORD}",
        "client_secret = <replace-me-before-deploy>",
        "secret = ****************************",
    ):
        assert secrets.scan(value) == [], value


def test_spans_do_not_overlap_after_dedupe() -> None:
    text = f"aws_access_key_id = {_AWS_ID} and aws_secret_access_key = {_AWS_SECRET}"
    findings = secrets.scan(text)
    for earlier, later in itertools.pairwise(findings):
        assert earlier.end <= later.start, "overlapping spans corrupt the replacement"


def test_shannon_entropy_matches_the_outage_measurements() -> None:
    """The postmortem's two numbers, pinned.

    If these move, the corpus above is measuring something other than what
    PR #365 measured, and its authority as a regression suite is gone.
    """
    assert round(secrets.shannon_entropy("/OdysseyPrivate/odyssey/experiments/casp"), 2) == 3.85
    assert round(secrets.shannon_entropy("/workspace/library/checkpoints/fsq/FINAL"), 2) == 4.38


# ---------------------------------------------------------------------------
# Bounded work — no rule may be pathological
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label,text",
    [
        ("repeated_a", "a" * 40_000),
        ("akia_flood", ("AKIA" + "B" * 60) * 600),
        ("bearer_flood", ("Authorization: Bearer " + "a" * 5_000) * 8),
        ("begin_marker_flood", "-----BEGIN PRIVATE KEY-----" * 2_000),
        ("anchor_flood", ("secret=" + "Zq" * 40 + "\n") * 2_000),
        ("uri_flood", ("postgres://" + "a" * 60 + ":" + "b" * 60 + "@h ") * 500),
        ("uri_no_at_flood", ("postgres://a:" + "b" * 3000 + " ") * 40),
        ("star_key_flood", ("A_" * 40_000)),
        ("camel_flood", ("aPassword" * 5_000)),
        ("netrc_flood", ("machine h login u " * 4_000)),
        ("xml_flood", ("<password>" + "a" * 600) * 200),
        ("env_default_flood", ('os.getenv("API_KEY", "' + "a" * 600) * 200),
        ("docker_auth_flood", ('"auth": "' + "A" * 5_000) * 20),
        ("wandb_key_flood", ("wandb:\n  key: " + "a" * 39 + "\n") * 2_000),
        ("uri_long_no_at_flood", ("https://u:" + "A" * 3_000 + "/") * 20),
        ("camel_bare_flood", ("xPassword: " + "a1" * 300 + " ") * 100),
        ("acronym_flood", ("ABCDEFGPassword" * 4_000)),
        ("env_fallback_flood", ('process.env.API_KEY || "' + "a" * 600) * 100),
        ("cli_default_flood", ('add_argument("--api-key", ' + "x" * 290 + " default=\"" + "a" * 600) * 50),
        ("env_pair_flood", ("- name: API_KEY\n" + " " * 5_000) * 10),
        ("env_pair_spaces", " " * 60_000),
        ("dockerfile_env_flood", ("ENV A_KEY " + "a" * 600 + "\n") * 100),
        ("identitytoken_flood", ('"identitytoken": "' + "a" * 5_000) * 10),
        ("npmrc_flood", ("_auth=" + "A" * 5_000 + "\n") * 10),
    ],
)
def test_no_rule_is_pathological(label: str, text: str) -> None:
    """A wall-clock ceiling cannot interrupt a running `re.search` — CPython
    holds the GIL inside one match. So the bound has to come from the rules
    themselves being linear. This test is the only thing that proves it."""
    started = time.perf_counter()
    secrets.scan(text)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"{label}: scan took {elapsed:.2f}s on {len(text)} chars"


def test_long_input_is_windowed_not_truncated() -> None:
    """Truncating would bound runtime by creating a blind spot."""
    filler = "ordinary transcript text about checkpoints and loss curves. "
    base = filler * 1_200  # comfortably past MAX_SCAN_CHARS
    key = _AWS_ID
    for offset in (5_000, secrets.MAX_SCAN_CHARS - 100, 70_000):
        blob = base[:offset] + " " + key + " " + base[offset:]
        redacted, rules = secrets.redact(blob)
        assert key not in redacted, f"credential at offset {offset} survived"
        assert "aws-access-key-id" in rules


# ---------------------------------------------------------------------------
# Event walking — shape preservation
# ---------------------------------------------------------------------------


def test_redact_event_preserves_shape() -> None:
    event = {
        "type": "assistant",
        "uuid": "abc",
        "nested": {"list": [1, 2.5, True, None, _AWS_ID]},
        "message": {"content": [{"type": "text", "text": "clean"}]},
    }
    out, rules = secrets.redact_event(event)
    assert rules == ["aws-access-key-id"]
    assert out["type"] == "assistant"
    assert out["uuid"] == "abc"
    assert out["nested"]["list"][:4] == [1, 2.5, True, None]
    assert out["nested"]["list"][4] == "<redacted:aws-access-key-id>"
    assert out["message"]["content"][0]["text"] == "clean"


def test_redact_event_ignores_non_dict() -> None:
    assert secrets.redact_event("plain") == ("plain", [])
    assert secrets.redact_event(None) == (None, [])


# ---------------------------------------------------------------------------
# The wiring: every lane, every producer
# ---------------------------------------------------------------------------

_LEAK = f"AWS Access Key ID [None]: {_AWS_ID}"


def _cc_line() -> bytes:
    return json.dumps(
        {
            "type": "assistant",
            "uuid": "u1",
            "message": {"role": "assistant", "content": [{"type": "text", "text": _LEAK}]},
        }
    ).encode()


def _codex_line() -> bytes:
    return json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": _LEAK}],
            },
            "timestamp": "2026-09-12T00:00:00Z",
        }
    ).encode()


@pytest.mark.parametrize(
    "label,line,sanitize",
    [("claude_code", _cc_line(), cc_sanitize), ("codex", _codex_line(), codex_sanitize)],
)
def test_build_batch_body_redacts_every_lane(label: str, line: bytes, sanitize) -> None:
    body = build_batch_body(
        device_id="d",
        session_id="s",
        batch_seq=0,
        cwd="/tmp",
        base_line_no=0,
        lines=[line],
        sanitize=sanitize,
    )
    assert body is not None, f"{label}: nothing shipped"
    text = body.decode()
    assert _AWS_ID not in text, f"{label}: credential reached the wire"
    assert "<redacted:aws-access-key-id>" in text


def test_batch_body_carries_a_redaction_count_and_no_values() -> None:
    body = build_batch_body(
        device_id="d",
        session_id="s",
        batch_seq=0,
        cwd="/tmp",
        base_line_no=0,
        lines=[_cc_line()],
        sanitize=cc_sanitize,
    )
    parsed = json.loads(body)
    assert parsed["redactions"]["count"] >= 1
    assert parsed["redactions"]["rules"] == ["aws-access-key-id"]
    # The envelope carries names and counts. It must never carry a value.
    assert "AKIA" not in json.dumps(parsed["redactions"])


def test_clean_batch_carries_no_redaction_key() -> None:
    """A session with nothing to redact must look exactly as it did before, so
    the dashboard has no empty state to render and no notice to suppress."""
    line = json.dumps(
        {
            "type": "assistant",
            "uuid": "u1",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "all clean"}]},
        }
    ).encode()
    body = build_batch_body(
        device_id="d",
        session_id="s",
        batch_seq=0,
        cwd="/tmp",
        base_line_no=0,
        lines=[line],
        sanitize=cc_sanitize,
    )
    assert "redactions" not in json.loads(body)


def test_unparseable_line_is_still_redacted() -> None:
    """A malformed line is kept as a raw string rather than dropped. That
    lenient path must not become the way a credential gets through."""
    body = build_batch_body(
        device_id="d",
        session_id="s",
        batch_seq=0,
        cwd="/tmp",
        base_line_no=0,
        lines=[f"not json at all: {_AWS_ID}".encode()],
        sanitize=cc_sanitize,
    )
    assert body is not None
    assert _AWS_ID not in body.decode()


# ---------------------------------------------------------------------------
# Telling the researcher — the tap has no terminal of its own
# ---------------------------------------------------------------------------


def _storage(tmp_path):
    from tap.storage import Storage

    return Storage(tmp_path / "state.db")


def test_enqueue_records_a_notice_the_next_session_can_print(tmp_path) -> None:
    from tap import outbox

    storage = _storage(tmp_path)
    try:
        body = build_batch_body(
            device_id="d",
            session_id="s",
            batch_seq=0,
            cwd="/tmp",
            base_line_no=0,
            lines=[_cc_line()],
            sanitize=cc_sanitize,
        )
        outbox.enqueue(storage=storage, session_id="s", batch_seq=0, cwd="/tmp", body=body, now=0)
        notice = outbox.redaction_notice(storage)
        assert "redacted 1 credential-shaped value" in notice
        assert "aws-access-key-id" in notice
        assert "rotate" in notice
        # No value, ever.
        assert "AKIA" not in notice
    finally:
        storage.close()


def test_notice_is_cleared_after_being_read(tmp_path) -> None:
    """Told once. A notice that repeats every session is a notice people learn
    to scroll past."""
    from tap import outbox

    storage = _storage(tmp_path)
    try:
        body = build_batch_body(
            device_id="d",
            session_id="s",
            batch_seq=0,
            cwd="/tmp",
            base_line_no=0,
            lines=[_cc_line()],
            sanitize=cc_sanitize,
        )
        outbox.enqueue(storage=storage, session_id="s", batch_seq=0, cwd="/tmp", body=body, now=0)
        assert outbox.redaction_notice(storage)
        assert outbox.redaction_notice(storage) == ""
    finally:
        storage.close()


def test_notice_accumulates_across_batches(tmp_path) -> None:
    from tap import outbox

    storage = _storage(tmp_path)
    try:
        for seq in range(3):
            body = build_batch_body(
                device_id="d",
                session_id="s",
                batch_seq=seq,
                cwd="/tmp",
                base_line_no=0,
                lines=[_cc_line()],
                sanitize=cc_sanitize,
            )
            outbox.enqueue(
                storage=storage, session_id="s", batch_seq=seq, cwd="/tmp", body=body, now=0
            )
        assert "redacted 3 credential-shaped values" in outbox.redaction_notice(storage)
    finally:
        storage.close()


def test_clean_session_produces_no_notice(tmp_path) -> None:
    from tap import outbox

    storage = _storage(tmp_path)
    try:
        line = json.dumps(
            {
                "type": "assistant",
                "uuid": "u1",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "clean"}]},
            }
        ).encode()
        body = build_batch_body(
            device_id="d",
            session_id="s",
            batch_seq=0,
            cwd="/tmp",
            base_line_no=0,
            lines=[line],
            sanitize=cc_sanitize,
        )
        outbox.enqueue(storage=storage, session_id="s", batch_seq=0, cwd="/tmp", body=body, now=0)
        assert outbox.redaction_notice(storage) == ""
    finally:
        storage.close()


def test_recording_a_notice_never_breaks_capture(tmp_path) -> None:
    """Reporting is best-effort by construction. A malformed body, or a storage
    that refuses the write, must not stop the batch being spooled."""
    from tap import outbox

    storage = _storage(tmp_path)
    try:
        outbox.enqueue(
            storage=storage, session_id="s", batch_seq=0, cwd="/tmp", body=b"not json", now=0
        )
        assert outbox.redaction_notice(storage) == ""
    finally:
        storage.close()
