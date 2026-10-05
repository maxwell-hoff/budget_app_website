import base64
import hashlib
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

import plaid_webhook
from extensions import db
from models import PlaidItem
from tests.conftest import make_app
from tests.test_app_sessions import add_user
from tests.test_plaid_api import plaid_config, plaid_exception

KEY_ID = '6c5516e1-92dc-479e-a8ff-5a51992e0001'


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def b64_json(value):
    return b64(json.dumps(value).encode())


class SigningKey:
    """Stands in for Plaid's webhook signing key."""

    def __init__(self, kid=KEY_ID):
        self.kid = kid
        self.private = ec.generate_private_key(ec.SECP256R1())

    def jwk(self, expired_at=None):
        numbers = self.private.public_key().public_numbers()
        return {
            'alg': 'ES256', 'crv': 'P-256', 'kid': self.kid, 'kty': 'EC', 'use': 'sig',
            'x': b64(numbers.x.to_bytes(32, 'big')), 'y': b64(numbers.y.to_bytes(32, 'big')),
            'created_at': 1560466150, 'expired_at': expired_at,
        }

    def sign(self, body, iat=None, header=None, claims=None):
        header = header or {'alg': 'ES256', 'kid': self.kid, 'typ': 'JWT'}
        claims = claims if claims is not None else {
            'iat': int(time.time()) if iat is None else iat,
            'request_body_sha256': hashlib.sha256(body).hexdigest(),
        }
        signing_input = f'{b64_json(header)}.{b64_json(claims)}'
        der = self.private.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        return f'{signing_input}.{b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))}'


class FakeKeys:
    def __init__(self):
        self.keys = {}
        self.fetches = []

    def fetch(self, key_id):
        self.fetches.append(key_id)
        if key_id not in self.keys:
            raise plaid_exception('INVALID_WEBHOOK_VERIFICATION_KEY_ID')
        return self.keys[key_id]


@pytest.fixture
def signing_key():
    return SigningKey()


@pytest.fixture
def fake_keys(monkeypatch, signing_key):
    fake = FakeKeys()
    fake.keys[KEY_ID] = signing_key.jwk()
    monkeypatch.setattr(plaid_webhook, 'fetch_verification_key', fake.fetch)
    return fake


@pytest.fixture
def webhook_app():
    app = make_app(**plaid_config())
    user_id = add_user(app)
    with app.app_context():
        db.session.add(PlaidItem(user_id=user_id, item_id='item-1', access_token_encrypted='x'))
        db.session.commit()
    return app


def payload(webhook_type='ITEM', webhook_code='ERROR', item_id='item-1', **extra):
    body = {'webhook_type': webhook_type, 'webhook_code': webhook_code, 'item_id': item_id,
            'environment': 'sandbox', **extra}
    # Plaid sends pretty-printed JSON; the hash covers the exact bytes.
    return json.dumps(body, indent=2).encode()


def post(app, body, token):
    headers = {'Plaid-Verification': token} if token is not None else {}
    return app.test_client().post('/plaid/webhook', data=body, headers=headers, content_type='application/json')


def send(app, signing_key, body):
    return post(app, body, signing_key.sign(body))


def status(app, item_id='item-1'):
    with app.app_context():
        return db.session.scalar(db.select(PlaidItem.status).filter_by(item_id=item_id))


def set_status(app, value, item_id='item-1'):
    with app.app_context():
        db.session.scalar(db.select(PlaidItem).filter_by(item_id=item_id)).status = value
        db.session.commit()


LOGIN_REQUIRED = {'error': {'error_type': 'ITEM_ERROR', 'error_code': 'ITEM_LOGIN_REQUIRED',
                            'error_message': 'the login details of this item have changed'}}


# --- Availability ----------------------------------------------------------------

def test_404_when_accounts_disabled():
    app = make_app(**plaid_config(ACCOUNTS_ENABLED=False))
    assert app.test_client().post('/plaid/webhook').status_code == 404


def test_404_when_plaid_not_configured():
    app = make_app(**plaid_config(PLAID_SECRET=''))
    assert app.test_client().post('/plaid/webhook').status_code == 404


def test_needs_no_csrf_token(signing_key, fake_keys):
    app = make_app(**plaid_config(WTF_CSRF_ENABLED=True))
    assert send(app, signing_key, payload()).status_code == 200


# --- Status changes ----------------------------------------------------------------

@pytest.mark.parametrize('before,webhook_type,code,extra,after', [
    ('ok', 'ITEM', 'ERROR', LOGIN_REQUIRED, 'relink_required'),
    ('ok', 'ITEM', 'ERROR', {'error': {'error_code': 'ITEM_NOT_FOUND'}}, 'error'),
    ('ok', 'ITEM', 'ERROR', {'error': {'error_code': 'INSTITUTION_DOWN'}}, 'relink_required'),
    ('relink_recommended', 'ITEM', 'ERROR', LOGIN_REQUIRED, 'relink_required'),
    ('ok', 'ITEM', 'PENDING_EXPIRATION', {'consent_expiration_time': '2026-10-12T00:00:00Z'}, 'relink_recommended'),
    ('ok', 'ITEM', 'PENDING_DISCONNECT', {'reason': 'INSTITUTION_MIGRATION'}, 'relink_recommended'),
    ('relink_required', 'ITEM', 'PENDING_EXPIRATION', {}, 'relink_required'),
    ('error', 'ITEM', 'PENDING_DISCONNECT', {}, 'error'),
    ('ok', 'ITEM', 'USER_PERMISSION_REVOKED', {}, 'error'),
    ('relink_required', 'ITEM', 'LOGIN_REPAIRED', {}, 'ok'),
    ('relink_recommended', 'ITEM', 'LOGIN_REPAIRED', {}, 'ok'),
    ('error', 'ITEM', 'LOGIN_REPAIRED', {}, 'error'),
    ('ok', 'ITEM', 'WEBHOOK_UPDATE_ACKNOWLEDGED', {}, 'ok'),
    ('ok', 'ITEM', 'NEW_ACCOUNTS_AVAILABLE', {}, 'ok'),
    ('relink_required', 'TRANSACTIONS', 'DEFAULT_UPDATE', {'new_transactions': 3}, 'relink_required'),
    ('ok', 'TRANSACTIONS', 'HISTORICAL_UPDATE', {}, 'ok'),
])
def test_webhook_updates_item_status(webhook_app, signing_key, fake_keys, before, webhook_type, code, extra, after):
    set_status(webhook_app, before)
    resp = send(webhook_app, signing_key, payload(webhook_type, code, **extra))
    assert resp.status_code == 200
    assert resp.get_json() == {'received': True}
    assert status(webhook_app) == after


def test_unknown_item_is_acknowledged(webhook_app, signing_key, fake_keys):
    assert send(webhook_app, signing_key, payload(item_id='someone-else')).status_code == 200
    assert send(webhook_app, signing_key, payload(item_id=None)).status_code == 200
    assert status(webhook_app) == 'ok'


def test_other_environment_is_ignored(webhook_app, signing_key, fake_keys):
    body = payload(**LOGIN_REQUIRED, environment='production')
    assert send(webhook_app, signing_key, body).status_code == 200
    assert status(webhook_app) == 'ok'


# --- Verification ----------------------------------------------------------------

def assert_rejected(app, resp):
    assert resp.status_code == 400
    assert status(app) == 'ok'


def test_missing_header_rejected(webhook_app, fake_keys):
    assert_rejected(webhook_app, post(webhook_app, payload(), None))
    assert fake_keys.fetches == []


@pytest.mark.parametrize('token', ['', 'garbage', 'a.b', 'a.b.c.d', '!!!.???.***', 'e30.e30.e30'])
def test_malformed_token_rejected(webhook_app, fake_keys, token):
    assert_rejected(webhook_app, post(webhook_app, payload(), token))


@pytest.mark.parametrize('header', [
    {'alg': 'none', 'kid': KEY_ID},
    {'alg': 'HS256', 'kid': KEY_ID},
    {'alg': 'RS256', 'kid': KEY_ID},
    {'alg': 'ES256'},
    {'alg': 'ES256', 'kid': ''},
    {'alg': 'ES256', 'kid': 5},
])
def test_unexpected_header_rejected(webhook_app, signing_key, fake_keys, header):
    body = payload()
    assert_rejected(webhook_app, post(webhook_app, body, signing_key.sign(body, header=header)))
    assert fake_keys.fetches == []


def test_unsigned_token_rejected(webhook_app, signing_key, fake_keys):
    body = payload()
    header, claims, _ = signing_key.sign(body).split('.')
    assert_rejected(webhook_app, post(webhook_app, body, f'{header}.{claims}.'))


def test_signature_from_another_key_rejected(webhook_app, fake_keys):
    impostor = SigningKey()  # same kid, different private key
    body = payload(**LOGIN_REQUIRED)
    assert_rejected(webhook_app, post(webhook_app, body, impostor.sign(body)))


def test_tampered_claims_rejected(webhook_app, signing_key, fake_keys):
    body = payload(**LOGIN_REQUIRED)
    header, _claims, signature = signing_key.sign(body).split('.')
    forged = b64_json({'iat': int(time.time()), 'request_body_sha256': hashlib.sha256(body).hexdigest(), 'x': 1})
    assert_rejected(webhook_app, post(webhook_app, body, f'{header}.{forged}.{signature}'))


def test_tampered_body_rejected(webhook_app, signing_key, fake_keys):
    token = signing_key.sign(payload(webhook_code='WEBHOOK_UPDATE_ACKNOWLEDGED'))
    assert_rejected(webhook_app, post(webhook_app, payload(**LOGIN_REQUIRED), token))


def test_reformatted_body_rejected(webhook_app, signing_key, fake_keys):
    body = payload(**LOGIN_REQUIRED)
    token = signing_key.sign(body)
    assert_rejected(webhook_app, post(webhook_app, json.dumps(json.loads(body)).encode(), token))


@pytest.mark.parametrize('age', [plaid_webhook.MAX_AGE_SECONDS + 5, -plaid_webhook.CLOCK_SKEW_SECONDS - 5])
def test_old_or_future_token_rejected(webhook_app, signing_key, fake_keys, age):
    body = payload(**LOGIN_REQUIRED)
    assert_rejected(webhook_app, post(webhook_app, body, signing_key.sign(body, iat=int(time.time()) - age)))


def test_recent_token_accepted(webhook_app, signing_key, fake_keys):
    body = payload(**LOGIN_REQUIRED)
    resp = post(webhook_app, body, signing_key.sign(body, iat=int(time.time()) - plaid_webhook.MAX_AGE_SECONDS + 10))
    assert resp.status_code == 200 and status(webhook_app) == 'relink_required'


@pytest.mark.parametrize('claims', [
    {},
    {'request_body_sha256': 'x'},
    {'iat': 'now', 'request_body_sha256': 'x'},
    {'iat': int(time.time())},
    {'iat': int(time.time()), 'request_body_sha256': 5},
])
def test_missing_claims_rejected(webhook_app, signing_key, fake_keys, claims):
    body = payload()
    if claims.get('request_body_sha256') == 'x':
        claims = {**claims, 'request_body_sha256': hashlib.sha256(body).hexdigest()}
    assert_rejected(webhook_app, post(webhook_app, body, signing_key.sign(body, claims=claims)))


def test_expired_key_rejected(webhook_app, signing_key, fake_keys):
    fake_keys.keys[KEY_ID] = signing_key.jwk(expired_at=1700000000)
    assert_rejected(webhook_app, send(webhook_app, signing_key, payload(**LOGIN_REQUIRED)))


def test_unknown_key_rejected(webhook_app, fake_keys):
    stranger = SigningKey(kid='not-a-plaid-key')
    body = payload(**LOGIN_REQUIRED)
    assert_rejected(webhook_app, post(webhook_app, body, stranger.sign(body)))
    assert fake_keys.fetches == ['not-a-plaid-key']


def test_wrong_key_type_rejected(webhook_app, signing_key, fake_keys):
    fake_keys.keys[KEY_ID] = {**signing_key.jwk(), 'crv': 'P-384'}
    assert_rejected(webhook_app, send(webhook_app, signing_key, payload(**LOGIN_REQUIRED)))


@pytest.mark.parametrize('body', [b'not json', b'[1, 2]'])
def test_signed_but_not_a_json_object_rejected(webhook_app, signing_key, fake_keys, body):
    assert send(webhook_app, signing_key, body).status_code == 400


def test_keys_are_cached(webhook_app, signing_key, fake_keys, monkeypatch):
    send(webhook_app, signing_key, payload())
    send(webhook_app, signing_key, payload())
    assert fake_keys.fetches == [KEY_ID]

    later = time.time() + plaid_webhook.KEY_CACHE_SECONDS + 1
    monkeypatch.setattr(plaid_webhook.time, 'time', lambda: later)
    body = payload()
    post(webhook_app, body, signing_key.sign(body, iat=int(later)))
    assert fake_keys.fetches == [KEY_ID, KEY_ID]


def test_verify_returns_the_claims(webhook_app, signing_key, fake_keys):
    body = payload()
    with webhook_app.test_request_context():
        claims = plaid_webhook.verify(body, signing_key.sign(body, iat=1000), now=1010)
    assert claims == {'iat': 1000, 'request_body_sha256': hashlib.sha256(body).hexdigest()}
