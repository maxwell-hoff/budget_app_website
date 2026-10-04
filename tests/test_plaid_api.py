import json
from datetime import datetime, timedelta, timezone

import plaid
import pytest
import urllib3
from cryptography.fernet import Fernet

import plaid_api
from extensions import db
from models import PlaidItem, Subscription, User, utcnow
from tests.conftest import make_app
from tests.test_app_sessions import DesktopApp, add_user, approve, exchange, sign_in

ACCESS_TOKEN = 'access-sandbox-11111111-2222-3333-4444-555555555555'


def plaid_config(**overrides):
    config = {
        'ACCOUNTS_ENABLED': True, 'PLAID_CLIENT_ID': 'cid', 'PLAID_SECRET': 'secret',
        'PLAID_TOKEN_KEY': Fernet.generate_key().decode(),
    }
    config.update(overrides)
    return config


def plaid_exception(error_code, status=400):
    exc = plaid.ApiException(status=status, reason='Bad Request')
    exc.body = json.dumps({'error_code': error_code, 'error_type': 'INVALID_INPUT', 'request_id': 'req-1'})
    return exc


class FakePlaid:
    def __init__(self):
        self.link_tokens_for = []
        self.exchanged = []
        self.removed = []
        self.fail = {}
        self.next_item_id = 'item-1'

    def _maybe_fail(self, name):
        if name in self.fail:
            raise self.fail[name]

    def create_link_token(self, user):
        self._maybe_fail('link')
        self.link_tokens_for.append(user.id)
        return 'link-sandbox-abc', datetime(2026, 10, 5, 2, 0, tzinfo=timezone.utc)

    def exchange_public_token(self, public_token):
        self._maybe_fail('exchange')
        self.exchanged.append(public_token)
        return ACCESS_TOKEN, self.next_item_id

    def remove_item(self, access_token):
        self.removed.append(access_token)
        self._maybe_fail('remove')


@pytest.fixture
def fake_plaid(monkeypatch):
    fake = FakePlaid()
    for name in ('create_link_token', 'exchange_public_token', 'remove_item'):
        monkeypatch.setattr(plaid_api, name, getattr(fake, name))
    return fake


@pytest.fixture
def plaid_app():
    return make_app(**plaid_config())


def subscribe(app, user_id, status='active'):
    with app.app_context():
        db.session.add(Subscription(
            user_id=user_id, stripe_customer_id=f'cus_{user_id}', stripe_subscription_id=f'sub_{user_id}',
            status=status, current_period_start=utcnow() - timedelta(days=40),
            current_period_end=utcnow() - timedelta(days=10),
        ))
        db.session.commit()


@pytest.fixture
def subscriber(plaid_app):
    """(user_id, auth headers) for a signed-in user with an active subscription."""
    user_id = add_user(plaid_app)
    subscribe(plaid_app, user_id)
    return user_id, {'Authorization': f'Bearer {sign_in(plaid_app)}'}


def call(app, method, path, headers=None, **kwargs):
    return getattr(app.test_client(), method)(path, headers=headers, **kwargs)


def link(app, headers, public_token='public-sandbox-1', institution=None):
    body = {'public_token': public_token}
    if institution is not False:
        body['institution'] = institution or {'id': 'ins_109508', 'name': 'First Platypus Bank'}
    return call(app, 'post', '/v1/plaid/exchange', headers, json=body)


def stored_items(app):
    with app.app_context():
        return [
            (i.user_id, i.item_id, i.access_token_encrypted, i.institution_id, i.institution_name, i.status)
            for i in db.session.scalars(db.select(PlaidItem).order_by(PlaidItem.id))
        ]


def assert_api_error(resp, status, code):
    assert resp.status_code == status, resp.data
    assert resp.get_json()['error']['code'] == code


ENDPOINTS = [
    ('post', '/v1/plaid/link-token'), ('post', '/v1/plaid/exchange'),
    ('get', '/v1/plaid/items'), ('delete', '/v1/plaid/items/item-1'),
]


# --- Availability and configuration ---------------------------------------------

@pytest.mark.parametrize('method,path', ENDPOINTS)
def test_404_when_accounts_disabled(method, path):
    app = make_app(**plaid_config(ACCOUNTS_ENABLED=False))
    assert call(app, method, path).status_code == 404


@pytest.mark.parametrize('missing', ['PLAID_CLIENT_ID', 'PLAID_SECRET', 'PLAID_TOKEN_KEY'])
def test_404_when_plaid_not_configured(missing):
    app = make_app(**plaid_config(**{missing: ''}))
    assert not app.config['PLAID_ENABLED']
    for method, path in ENDPOINTS:
        assert call(app, method, path).status_code == 404


def test_bad_environment_fails_at_startup():
    with pytest.raises(RuntimeError, match='PLAID_ENVIRONMENT'):
        make_app(**plaid_config(PLAID_ENVIRONMENT='development'))


def test_malformed_token_key_fails_at_startup():
    with pytest.raises(ValueError):
        make_app(**plaid_config(PLAID_TOKEN_KEY='not-a-fernet-key'))


def test_bad_settings_ignored_while_accounts_disabled():
    app = make_app(**plaid_config(ACCOUNTS_ENABLED=False, PLAID_ENVIRONMENT='nope', PLAID_TOKEN_KEY='bad'))
    assert app.test_client().get('/').status_code == 200


@pytest.mark.parametrize('method,path', ENDPOINTS)
def test_401_without_session(plaid_app, fake_plaid, method, path):
    resp = call(plaid_app, method, path)
    assert_api_error(resp, 401, 'unauthorized')
    assert call(plaid_app, method, path, {'Authorization': 'Bearer wrong'}).status_code == 401


@pytest.mark.parametrize('status', [None, 'canceled', 'unpaid', 'incomplete'])
@pytest.mark.parametrize('method,path', ENDPOINTS[:3])
def test_402_without_plaid_access(plaid_app, fake_plaid, method, path, status):
    user_id = add_user(plaid_app)
    if status:
        subscribe(plaid_app, user_id, status)
    headers = {'Authorization': f'Bearer {sign_in(plaid_app)}'}
    resp = call(plaid_app, method, path, headers, json={'public_token': 'public-sandbox-1'})
    assert_api_error(resp, 402, 'plaid_access_required')
    assert not fake_plaid.link_tokens_for and not fake_plaid.exchanged


# --- POST /v1/plaid/link-token ---------------------------------------------------

def test_link_token(plaid_app, fake_plaid, subscriber):
    user_id, headers = subscriber
    resp = call(plaid_app, 'post', '/v1/plaid/link-token', headers)
    assert resp.status_code == 200
    assert resp.get_json() == {'link_token': 'link-sandbox-abc', 'expiration': '2026-10-05T02:00:00Z'}
    assert fake_plaid.link_tokens_for == [user_id]


def test_link_token_plaid_error(plaid_app, fake_plaid, subscriber):
    fake_plaid.fail['link'] = plaid_exception('INTERNAL_SERVER_ERROR', status=500)
    assert_api_error(call(plaid_app, 'post', '/v1/plaid/link-token', subscriber[1]), 502, 'plaid_error')


@pytest.mark.parametrize('action,path', [
    ('link', '/v1/plaid/link-token'), ('exchange', '/v1/plaid/exchange'), ('remove', '/v1/plaid/items/item-1'),
])
def test_network_failure_reaching_plaid_is_502(plaid_app, fake_plaid, subscriber, action, path):
    headers = subscriber[1]
    if action == 'remove':
        link(plaid_app, headers)
    fake_plaid.fail[action] = urllib3.exceptions.MaxRetryError(None, '/link/token/create', 'DNS failure')
    method = 'delete' if action == 'remove' else 'post'
    resp = call(plaid_app, method, path, headers, json={'public_token': 'public-sandbox-1'})
    assert_api_error(resp, 502, 'plaid_error')


def test_link_token_request(plaid_app, monkeypatch):
    sent = {}

    class Client:
        def link_token_create(self, req, _request_timeout=None):
            sent['timeout'] = _request_timeout
            sent['request'] = req.to_dict()
            return type('Resp', (), {'link_token': 'link-x', 'expiration': None})()

    monkeypatch.setattr(plaid_api, '_plaid', Client)
    with plaid_app.app_context():
        user = User(id=42, email='u@example.com')
        assert plaid_api.create_link_token(user) == ('link-x', None)
    req = sent['request']
    assert sent['timeout'] == plaid_api.PLAID_TIMEOUT
    assert req['user'] == {'client_user_id': '42'}
    assert req['client_name'] == 'Workbench Budgeting'
    assert [str(p) for p in req['products']] == ['transactions']
    assert req['transactions'] == {'days_requested': 730}
    assert [str(c) for c in req['country_codes']] == ['US']


# --- POST /v1/plaid/exchange -----------------------------------------------------

def test_exchange_stores_encrypted_token_and_never_returns_it(plaid_app, fake_plaid, subscriber):
    user_id, headers = subscriber
    resp = link(plaid_app, headers)
    assert resp.status_code == 200
    item = resp.get_json()['item']
    assert item['item_id'] == 'item-1'
    assert item['institution_id'] == 'ins_109508'
    assert item['institution_name'] == 'First Platypus Bank'
    assert item['status'] == 'ok'
    assert item['last_synced_at'] is None
    assert item['created_at'].endswith('Z')
    assert ACCESS_TOKEN not in resp.get_data(as_text=True)
    assert fake_plaid.exchanged == ['public-sandbox-1']

    [(owner, item_id, ciphertext, *_)] = stored_items(plaid_app)
    assert owner == user_id and item_id == 'item-1'
    assert ACCESS_TOKEN not in ciphertext
    with plaid_app.app_context():
        assert plaid_api.decrypt_token(ciphertext) == ACCESS_TOKEN


def test_exchange_without_institution(plaid_app, fake_plaid, subscriber):
    item = link(plaid_app, subscriber[1], institution=False).get_json()['item']
    assert item['institution_id'] is None and item['institution_name'] is None


def test_exchange_trims_institution_fields(plaid_app, fake_plaid, subscriber):
    link(plaid_app, subscriber[1], institution={'id': ' ins_1 ', 'name': 'B' * 300})
    [(*_, institution_id, institution_name, _status)] = stored_items(plaid_app)
    assert institution_id == 'ins_1' and institution_name == 'B' * 255


@pytest.mark.parametrize('body', [
    None, [], {}, {'public_token': ''}, {'public_token': '  '}, {'public_token': 5},
    {'public_token': 'p', 'institution': 'Chase'},
])
def test_exchange_bad_requests(plaid_app, fake_plaid, subscriber, body):
    resp = call(plaid_app, 'post', '/v1/plaid/exchange', subscriber[1], json=body)
    assert_api_error(resp, 400, 'bad_request')
    assert fake_plaid.exchanged == []


def test_exchange_invalid_public_token(plaid_app, fake_plaid, subscriber):
    fake_plaid.fail['exchange'] = plaid_exception('INVALID_PUBLIC_TOKEN')
    assert_api_error(link(plaid_app, subscriber[1]), 400, 'bad_request')
    assert stored_items(plaid_app) == []


def test_exchange_plaid_error(plaid_app, fake_plaid, subscriber):
    fake_plaid.fail['exchange'] = plaid_exception('INTERNAL_SERVER_ERROR', status=500)
    assert_api_error(link(plaid_app, subscriber[1]), 502, 'plaid_error')
    assert stored_items(plaid_app) == []


def test_exchange_same_item_again_updates_it(plaid_app, fake_plaid, subscriber):
    headers = subscriber[1]
    link(plaid_app, headers)
    first_ciphertext = stored_items(plaid_app)[0][2]
    with plaid_app.app_context():
        db.session.scalar(db.select(PlaidItem)).status = 'relink_required'
        db.session.commit()
    link(plaid_app, headers, institution={'id': 'ins_2', 'name': 'Renamed'})
    [(_, _, ciphertext, institution_id, name, status)] = stored_items(plaid_app)
    assert (institution_id, name, status) == ('ins_2', 'Renamed', 'ok')
    assert ciphertext != first_ciphertext  # re-encrypted (Fernet uses a fresh IV)


def test_exchange_item_of_another_user_refused(plaid_app, fake_plaid, subscriber):
    link(plaid_app, subscriber[1])
    other_id = add_user(plaid_app, email='other@example.com')
    subscribe(plaid_app, other_id)
    client = plaid_app.test_client()
    client.post('/login', data={'email': 'other@example.com', 'password': 'correct horse battery'})
    desktop = DesktopApp()
    token = exchange(plaid_app.test_client(), approve(client, desktop), desktop.verifier).get_json()['session_token']
    assert_api_error(link(plaid_app, {'Authorization': f'Bearer {token}'}), 400, 'bad_request')
    assert [row[0] for row in stored_items(plaid_app)] == [subscriber[0]]


def test_item_limit(plaid_app, fake_plaid, subscriber):
    user_id, headers = subscriber
    with plaid_app.app_context():
        for n in range(plaid_api.MAX_ITEMS_PER_USER):
            db.session.add(PlaidItem(user_id=user_id, item_id=f'existing-{n}', access_token_encrypted='x'))
        db.session.commit()
    assert_api_error(call(plaid_app, 'post', '/v1/plaid/link-token', headers), 400, 'item_limit_reached')
    assert_api_error(link(plaid_app, headers), 400, 'item_limit_reached')
    assert fake_plaid.link_tokens_for == [] and fake_plaid.exchanged == []


# --- GET /v1/plaid/items ---------------------------------------------------------

def test_items_lists_only_the_callers_items(plaid_app, fake_plaid, subscriber):
    user_id, headers = subscriber
    other_id = add_user(plaid_app, email='other@example.com')
    with plaid_app.app_context():
        db.session.add(PlaidItem(user_id=other_id, item_id='theirs', access_token_encrypted='x'))
        db.session.commit()
    link(plaid_app, headers)
    fake_plaid.next_item_id = 'item-2'
    link(plaid_app, headers, institution={'id': 'ins_2', 'name': 'Second Bank'})

    resp = call(plaid_app, 'get', '/v1/plaid/items', headers)
    assert resp.status_code == 200
    items = resp.get_json()['items']
    assert [i['item_id'] for i in items] == ['item-1', 'item-2']
    assert set(items[0]) == {'item_id', 'institution_id', 'institution_name', 'status', 'created_at', 'last_synced_at'}
    assert ACCESS_TOKEN not in resp.get_data(as_text=True)


def test_items_empty(plaid_app, fake_plaid, subscriber):
    assert call(plaid_app, 'get', '/v1/plaid/items', subscriber[1]).get_json() == {'items': []}


# --- DELETE /v1/plaid/items/<item_id> --------------------------------------------

def test_delete_removes_at_plaid_and_locally(plaid_app, fake_plaid, subscriber):
    headers = subscriber[1]
    link(plaid_app, headers)
    resp = call(plaid_app, 'delete', '/v1/plaid/items/item-1', headers)
    assert resp.status_code == 204 and resp.data == b''
    assert fake_plaid.removed == [ACCESS_TOKEN]
    assert stored_items(plaid_app) == []
    assert_api_error(call(plaid_app, 'delete', '/v1/plaid/items/item-1', headers), 404, 'not_found')


def test_delete_someone_elses_item_is_404(plaid_app, fake_plaid, subscriber):
    other_id = add_user(plaid_app, email='other@example.com')
    with plaid_app.app_context():
        db.session.add(PlaidItem(user_id=other_id, item_id='theirs', access_token_encrypted='x'))
        db.session.commit()
    assert_api_error(call(plaid_app, 'delete', '/v1/plaid/items/theirs', subscriber[1]), 404, 'not_found')
    assert fake_plaid.removed == [] and len(stored_items(plaid_app)) == 1


@pytest.mark.parametrize('code', sorted(plaid_api.ITEM_GONE_ERRORS))
def test_delete_when_already_gone_at_plaid(plaid_app, fake_plaid, subscriber, code):
    link(plaid_app, subscriber[1])
    fake_plaid.fail['remove'] = plaid_exception(code)
    assert call(plaid_app, 'delete', '/v1/plaid/items/item-1', subscriber[1]).status_code == 204
    assert stored_items(plaid_app) == []


def test_delete_plaid_error_keeps_the_item(plaid_app, fake_plaid, subscriber):
    link(plaid_app, subscriber[1])
    fake_plaid.fail['remove'] = plaid_exception('INTERNAL_SERVER_ERROR', status=500)
    assert_api_error(call(plaid_app, 'delete', '/v1/plaid/items/item-1', subscriber[1]), 502, 'plaid_error')
    assert len(stored_items(plaid_app)) == 1


def test_delete_allowed_after_subscription_lapses(plaid_app, fake_plaid, subscriber):
    user_id, headers = subscriber
    link(plaid_app, headers)
    with plaid_app.app_context():
        db.session.scalar(db.select(Subscription)).status = 'unpaid'
        db.session.commit()
    assert_api_error(call(plaid_app, 'get', '/v1/plaid/items', headers), 402, 'plaid_access_required')
    assert call(plaid_app, 'delete', '/v1/plaid/items/item-1', headers).status_code == 204
    assert fake_plaid.removed == [ACCESS_TOKEN]


def test_deleting_the_user_deletes_their_items(plaid_app, fake_plaid, subscriber):
    user_id, headers = subscriber
    link(plaid_app, headers)
    with plaid_app.app_context():
        db.session.delete(db.session.get(User, user_id))
        db.session.commit()
    assert stored_items(plaid_app) == []


# --- Encryption ------------------------------------------------------------------

def test_key_rotation_reads_old_tokens_and_writes_with_new_key():
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    old_app = make_app(**plaid_config(PLAID_TOKEN_KEY=old))
    with old_app.app_context():
        ciphertext = plaid_api.encrypt_token(ACCESS_TOKEN)

    rotated = make_app(**plaid_config(PLAID_TOKEN_KEY=f'{new}, {old}'))
    with rotated.app_context():
        assert plaid_api.decrypt_token(ciphertext) == ACCESS_TOKEN
        fresh = plaid_api.encrypt_token(ACCESS_TOKEN)
    assert Fernet(new).decrypt(fresh.encode()).decode() == ACCESS_TOKEN
