"""Synthetic credentials at the actual journal/wire and scanner boundaries."""
import base64
import hashlib
import json
from unittest.mock import patch

import pytest

from tap import secrets, transcript
from tap.sanitize import sanitize_event
from tap.session_journal import Journal, ReconciliationRequired, Wire

KEY = 'ghp_' + hashlib.sha256(b'synthetic-transcript-regression').hexdigest()[:36]
OPAQUE = 'qT7mP2vN9cX4zL8hR5kW1yB6dF0sA3uE7jG9pC2n'


def native(source, text):
    if source == 'codex':
        return {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}}
    if source == 'pi':
        return {'type': 'message', 'id': 'synthetic', 'message': {'role': 'user', 'content': text}}
    return {'type': 'user', 'message': {'role': 'user', 'content': text}}


def reserve(tmp_path, source='claude_code', historical=False, text=KEY):
    path = tmp_path / 'source.jsonl'
    path.write_text(json.dumps(native(source, text)) + '\n')
    journal = Journal('https://synthetic.invalid', 'synthetic', source, directory=tmp_path/'journal')
    remote = {'customer_id': 'synthetic', 'source': source, 'session_id': 'synthetic', 'stream': None}
    journal.ensure('synthetic', path, remote, historical=historical, provenance={'source_cwd': '/work/'+KEY})
    return journal


@pytest.mark.parametrize('source', ['claude_code', 'codex', 'pi'])
@pytest.mark.parametrize('historical', [False, True])
def test_protocol_two_scrubs_before_pending_and_wire(tmp_path, source, historical):
    journal = reserve(tmp_path, source, historical)
    try:
        body = journal.stage('synthetic', cwd='/work/'+KEY)
        assert body and KEY.encode() not in body
        assert journal.pending('synthetic') == body
        captured = []
        def post(request, **kwargs):
            captured.append(request.data)
            data = json.loads(request.data)
            receipt = {k: data[k] for k in ('batch_seq', 'source_byte_end', 'source_line_end', 'event_end', 'prefix_sha256')}
            receipt.update(body_sha256=hashlib.sha256(request.data).hexdigest(), finalized=False)
            class Response:
                status = 202
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def read(self): return json.dumps({'protocol_version': 2, 'receipt': receipt}).encode()
            return Response()
        with patch('urllib.request.urlopen', post):
            assert journal.deliver('synthetic', Wire('https://synthetic.invalid', 'synthetic-auth', source))
        assert captured == [body]
        assert journal.pending('synthetic') is None
    finally:
        journal.close()


@pytest.mark.parametrize('entry', ['stage', 'deliver'])
def test_old_unsafe_pending_is_blocked_without_changing_receipt_identity(tmp_path, entry):
    journal = reserve(tmp_path, text='benign')
    try:
        clean = journal.stage('synthetic', cwd='/work')
        data = json.loads(clean)
        data['events'][0]['raw']['message']['content'] = KEY
        unsafe = json.dumps(data).encode()
        digest = hashlib.sha256(unsafe).hexdigest()
        journal.conn.execute('UPDATE pending SET body=?,digest=?', (unsafe, digest))
        with (
            patch('urllib.request.urlopen', side_effect=AssertionError('must not upload')),
            pytest.raises(ReconciliationRequired, match=r'redact|credential'),
        ):
            if entry == 'stage':
                journal.stage('synthetic', cwd='/work')
            else:
                journal.deliver('synthetic', Wire('https://synthetic.invalid', 'synthetic', 'claude_code'))
        assert journal.pending('synthetic') == unsafe
        assert journal.conn.execute('SELECT digest FROM pending').fetchone()[0] == digest
    finally:
        journal.close()


def test_legacy_envelope_and_dict_context():
    event = native('claude_code', 'keep conversation')
    event['extension'] = {'password': OPAQUE, KEY: 'keep value'}
    body = transcript.build_batch_body(device_id='device', session_id='session', batch_seq=0,
        cwd='/work/'+KEY, base_line_no=0, lines=[json.dumps(event).encode()], sanitize=sanitize_event)
    assert KEY.encode() not in body and OPAQUE.encode() not in body
    assert b'keep conversation' in body and b'keep value' in body


@pytest.mark.parametrize('anchor', ['api-key', 'auth-token', 'access-token', 'refresh_token', 'refresh-token', 'refresh token'])
def test_supported_anchor_spelling(anchor):
    cleaned, rules = secrets.redact(anchor+'='+OPAQUE)
    assert OPAQUE not in cleaned and rules


@pytest.mark.parametrize('value', ['R4in!Harbor7', 'x7Q9p2Z4', 'quote space R4in!Harbor7'])
def test_short_explicit_password(value):
    cleaned, rules = secrets.redact('password="'+value+'"')
    assert value not in cleaned and rules


def test_long_private_key_block():
    key = '-----BEGIN PRIVATE KEY-----\n' + 'aB7kP2nX9mQ4'*700 + '\n-----END PRIVATE KEY-----'
    cleaned, rules = secrets.redact('before\n'+key+'\nafter')
    assert key not in cleaned and rules
    assert 'before' in cleaned and 'after' in cleaned


@pytest.mark.parametrize('encode', [lambda s: base64.b64encode(s.encode()).decode(),
    lambda s: ''.join('%'+format(ord(c),'02X') for c in s),
    lambda s: ''.join(f'\\u{ord(c):04x}' for c in s),
    lambda s: s[:12]+'\x1b[0m'+s[12:], lambda s: s[:12]+'\u200b'+s[12:]])
def test_encoded_secret_preserves_surrounding_text(encode):
    encoded = encode(KEY)
    cleaned, rules = secrets.redact('before '+encoded+' after')
    assert encoded not in cleaned and rules
    assert cleaned.startswith('before ') and cleaned.endswith(' after')


def test_split_text_blocks_and_events():
    events = [native('claude_code', KEY[:20]), native('claude_code', KEY[20:])]
    cleaned, rules = secrets.redact_event(events)
    assert rules
    assert ''.join(e['message']['content'] for e in cleaned) != KEY
    blocks = [{'type':'text','text':KEY[:10]}, {'type':'text','text':KEY[10:]}]
    cleaned, rules = secrets.redact_event({'content':blocks})
    assert rules and ''.join(b['text'] for b in cleaned['content']) != KEY


@pytest.mark.parametrize('text', ['wandb git commit '+hashlib.sha1(b'benign revision').hexdigest(),
    'password=${DB_PASSWORD}', 'api_key=os.environ["API_KEY"]',
    '/workspace/library/checkpoints/model/FINAL_v2', 'secret=ordinary-configuration-checkpoint-path',
    'tokenizer=vocabulary-encoder-test'])
def test_benign_controls_unchanged(text):
    assert secrets.redact(text) == (text, [])


@pytest.mark.parametrize('text,needle', [
    ('wandb.login(key="'+hashlib.sha1(b'synthetic wandb').hexdigest()+'")', hashlib.sha1(b'synthetic wandb').hexdigest()),
    ('Authorization: Bearer x7Q9p2Z4', 'x7Q9p2Z4'),
    ('AUTHORIZATION: Basic '+base64.b64encode(b'audit-user:synthetic-password').decode(), base64.b64encode(b'audit-user:synthetic-password').decode()),
    ('token='+OPAQUE, OPAQUE),
], ids=['wandb_login', 'short_bearer', 'uppercase_basic', 'token_assignment'])
def test_explicit_credential_contexts(text, needle):
    cleaned, rules = secrets.redact(text)
    assert needle not in cleaned and rules


@pytest.mark.parametrize('error', [RuntimeError, MemoryError, RecursionError])
def test_journal_scanner_failure_keeps_source_and_never_reserves(tmp_path, monkeypatch, error):
    journal = reserve(tmp_path)
    try:
        before = journal.get('synthetic')
        def fail(*args, **kwargs): raise error('synthetic failure')
        monkeypatch.setattr(secrets, 'scan', fail)
        with pytest.raises(error):
            journal.stage('synthetic', cwd='/work')
        assert journal.pending('synthetic') is None
        assert journal.get('synthetic') == before
        assert (tmp_path/'source.jsonl').is_file()
    finally:
        journal.close()


def test_colliding_sensitive_keys_preserve_values_and_redaction_is_idempotent():
    other = 'ghp_' + hashlib.sha256(b'other synthetic key').hexdigest()[:36]
    original = {KEY: 'first value', other: 'second value', 'content': [KEY[:20], KEY[20:]]}
    cleaned, rules = secrets.redact_event(original)
    assert KEY not in json.dumps(cleaned) and other not in json.dumps(cleaned)
    assert set(v for v in cleaned.values() if isinstance(v, str)) == {'first value', 'second value'}
    assert rules and secrets.redact_event(cleaned) == (cleaned, [])


def test_large_fragment_run_does_not_restore_redacted_middle_block():
    blocks = [' ' * 32000 + KEY[:20], KEY[20:], ' ' * 32000]
    cleaned, rules = secrets.redact_event(blocks)
    assert rules and KEY not in ''.join(cleaned)
    assert KEY[20:] not in cleaned[1]


def test_explicit_password_passphrase_is_not_benign_prose():
    # A password assignment supplies credential context even for dictionary
    # words; retaining all but its first word still exposes the passphrase.
    clean, fired = secrets.redact('password = correct horse battery staple; loss=0.5')
    assert 'correct' not in clean and 'horse' not in clean and 'battery' not in clean and 'staple' not in clean
    assert 'loss=0.5' in clean and fired


def test_redacted_keys_reserve_existing_suffixes_and_preserve_every_sibling():
    payload = {
        '<redacted:github-token>:2': 'first',
        '<redacted:github-token>': 'second',
        KEY: 'third',
    }
    clean, fired = secrets.redact_event(payload)
    assert len(clean) == 3
    assert sorted(clean.values()) == ['first', 'second', 'third']
    assert clean['<redacted:github-token>:2'] == 'first'
    assert clean['<redacted:github-token>'] == 'second'
    assert KEY not in clean and fired
    assert secrets.redact_event(clean)[0] == clean


def test_redacted_keys_cannot_take_a_later_benign_key():
    payload = {KEY: 'first', '<redacted:github-token>': 'second',
               '<redacted:github-token>:1': 'third'}
    clean, _ = secrets.redact_event(payload)
    assert len(clean) == 3
    assert clean['<redacted:github-token>'] == 'second'
    assert clean['<redacted:github-token>:1'] == 'third'
    assert sorted(clean.values()) == ['first', 'second', 'third']
    assert secrets.redact_event(clean)[0] == clean
