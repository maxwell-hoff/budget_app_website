"""Live tests against Plaid's sandbox. They run only when PLAID_SANDBOX_CLIENT_ID and
PLAID_SANDBOX_SECRET are in the environment or .env, and are skipped otherwise. Separate
names from PLAID_CLIENT_ID / PLAID_SECRET so production keys are never used here.

    pytest -m plaid_sandbox -v
"""
import os
import time

import pytest
from cryptography.fernet import Fernet
from plaid.model.products import Products
from plaid.model.sandbox_item_reset_login_request import SandboxItemResetLoginRequest
from plaid.model.sandbox_public_token_create_request import SandboxPublicTokenCreateRequest

import config  # noqa: F401  (loads .env into os.environ)
import plaid_api
from extensions import db
from models import PlaidItem
from tests.conftest import make_app
from tests.test_app_sessions import add_user, sign_in
from tests.test_plaid_api import subscribe
from tests.test_plaid_sync import FIXTURE_ITEM

SANDBOX_INSTITUTION = 'ins_109508'  # First Platypus Bank
CLIENT_ID = os.environ.get('PLAID_SANDBOX_CLIENT_ID', '').strip()
SECRET = os.environ.get('PLAID_SANDBOX_SECRET', '').strip()

pytestmark = [
    pytest.mark.plaid_sandbox,
    pytest.mark.skipif(
        not (CLIENT_ID and SECRET),
        reason='Set PLAID_SANDBOX_CLIENT_ID and PLAID_SANDBOX_SECRET to run live Plaid tests.',
    ),
]


@pytest.fixture
def live_app():
    return make_app(
        ACCOUNTS_ENABLED=True,
        PLAID_CLIENT_ID=CLIENT_ID,
        PLAID_SECRET=SECRET,
        PLAID_ENVIRONMENT='sandbox',
        PLAID_TOKEN_KEY=Fernet.generate_key().decode(),
    )


def sandbox_public_token(app):
    """What Plaid Link hands the app after a user picks a bank (sandbox shortcut)."""
    with app.app_context():
        response = plaid_api._plaid().sandbox_public_token_create(SandboxPublicTokenCreateRequest(
            institution_id=SANDBOX_INSTITUTION, initial_products=[Products('transactions')],
        ))
    return response.public_token


def linked_item(app):
    """(auth headers, item_id) for a new subscriber with one sandbox bank linked."""
    user_id = add_user(app)
    subscribe(app, user_id)
    headers = {'Authorization': f'Bearer {sign_in(app)}'}
    resp = app.test_client().post('/v1/plaid/exchange', headers=headers, json={
        'public_token': sandbox_public_token(app),
        'institution': {'id': SANDBOX_INSTITUTION, 'name': 'First Platypus Bank'},
    })
    assert resp.status_code == 200, resp.get_json()
    return headers, resp.get_json()['item']['item_id']


def sync_when_ready(client, headers, body, timeout=60):
    """Plaid answers PRODUCT_NOT_READY for a few seconds after linking (503 here)."""
    deadline = time.monotonic() + timeout
    while True:
        resp = client.post('/v1/plaid/sync', headers=headers, json=body)
        if resp.status_code != 503 or time.monotonic() > deadline:
            return resp
        assert resp.get_json()['error']['code'] == 'plaid_not_ready'
        assert int(resp.headers['Retry-After']) > 0
        time.sleep(2)


def test_sync_against_sandbox_matches_the_fixture_shape(live_app):
    headers, item_id = linked_item(live_app)
    client = live_app.test_client()
    try:
        resp = sync_when_ready(client, headers, {'item_id': item_id, 'days_back': 730})
        assert resp.status_code == 200, resp.get_json()
        assert 'access-sandbox-' not in resp.get_data(as_text=True)
        [result] = resp.get_json()['items']
        assert result['error'] is None
        assert result['item']['item_id'] == item_id and result['item']['last_synced_at']
        assert result['accounts'] and result['transactions']

        # Same objects, with the same fields, as the shared fixture the desktop builds against.
        assert set(result) == set(FIXTURE_ITEM)
        assert {key for a in result['accounts'] for key in a} == {key for a in FIXTURE_ITEM['accounts'] for key in a}
        assert {key for t in result['transactions'] for key in t} == {
            key for t in FIXTURE_ITEM['transactions'] for key in t
        }
        account_ids = {a['account_id'] for a in result['accounts']}
        assert all(t['account_id'] in account_ids for t in result['transactions'])

        # Every item at once, and a shorter window.
        resp = sync_when_ready(client, headers, {'days_back': 30})
        assert resp.status_code == 200, resp.get_json()
        [everything] = resp.get_json()['items']
        assert everything['error'] is None and everything['item']['item_id'] == item_id
        assert len(everything['transactions']) <= len(result['transactions'])
    finally:
        assert client.delete(f'/v1/plaid/items/{item_id}', headers=headers).status_code == 204


def test_item_login_required_against_sandbox_is_409(live_app):
    headers, item_id = linked_item(live_app)
    client = live_app.test_client()
    try:
        with live_app.app_context():
            row = db.session.scalar(db.select(PlaidItem).filter_by(item_id=item_id))
            plaid_api._plaid().sandbox_item_reset_login(
                SandboxItemResetLoginRequest(access_token=plaid_api.decrypt_token(row.access_token_encrypted)),
            )
        resp = sync_when_ready(client, headers, {'item_id': item_id})
        assert resp.status_code == 409, resp.get_json()
        assert resp.get_json()['error']['code'] == 'plaid_relink_required'
        [item] = client.get('/v1/plaid/items', headers=headers).get_json()['items']
        assert item['status'] == 'relink_required'

        # Update mode: Plaid gives a Link token for the existing Item.
        resp = client.post(f'/v1/plaid/items/{item_id}/relink-token', headers=headers)
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()['link_token'].startswith('link-sandbox-')
        resp = client.post(f'/v1/plaid/items/{item_id}/relink-complete', headers=headers)
        assert resp.get_json()['item']['status'] == 'ok'
        # The sandbox Item still needs its login (no one went through Link), so sync says so again.
        assert sync_when_ready(client, headers, {'item_id': item_id}).status_code == 409
    finally:
        assert client.delete(f'/v1/plaid/items/{item_id}', headers=headers).status_code == 204


def test_link_exchange_list_delete_against_sandbox(live_app):
    user_id = add_user(live_app)
    subscribe(live_app, user_id)
    headers = {'Authorization': f'Bearer {sign_in(live_app)}'}
    client = live_app.test_client()

    resp = client.post('/v1/plaid/link-token', headers=headers)
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()['link_token'].startswith('link-sandbox-')
    assert resp.get_json()['expiration'].endswith('Z')

    resp = client.post('/v1/plaid/exchange', headers=headers, json={
        'public_token': sandbox_public_token(live_app),
        'institution': {'id': SANDBOX_INSTITUTION, 'name': 'First Platypus Bank'},
    })
    assert resp.status_code == 200, resp.get_json()
    item = resp.get_json()['item']
    assert 'access-sandbox-' not in resp.get_data(as_text=True)
    with live_app.app_context():
        row = db.session.scalar(db.select(PlaidItem).filter_by(item_id=item['item_id']))
        assert plaid_api.decrypt_token(row.access_token_encrypted).startswith('access-sandbox-')

    resp = client.get('/v1/plaid/items', headers=headers)
    assert [i['item_id'] for i in resp.get_json()['items']] == [item['item_id']]
    assert 'access-sandbox-' not in resp.get_data(as_text=True)

    assert client.delete(f"/v1/plaid/items/{item['item_id']}", headers=headers).status_code == 204
    assert client.get('/v1/plaid/items', headers=headers).get_json() == {'items': []}


def test_exchanging_the_same_public_token_twice_keeps_one_item(live_app):
    # Plaid answers a repeat exchange with the same Item and access token (e.g. the app
    # retried after a timeout), so it must not create a second row.
    user_id = add_user(live_app)
    subscribe(live_app, user_id)
    headers = {'Authorization': f'Bearer {sign_in(live_app)}'}
    client = live_app.test_client()
    public_token = sandbox_public_token(live_app)

    first = client.post('/v1/plaid/exchange', headers=headers, json={'public_token': public_token})
    again = client.post('/v1/plaid/exchange', headers=headers, json={'public_token': public_token})
    assert first.status_code == again.status_code == 200
    item_id = first.get_json()['item']['item_id']
    assert again.get_json()['item']['item_id'] == item_id
    assert [i['item_id'] for i in client.get('/v1/plaid/items', headers=headers).get_json()['items']] == [item_id]

    assert client.delete(f'/v1/plaid/items/{item_id}', headers=headers).status_code == 204


def test_invalid_public_token_is_a_bad_request(live_app):
    user_id = add_user(live_app)
    subscribe(live_app, user_id)
    headers = {'Authorization': f'Bearer {sign_in(live_app)}'}
    resp = live_app.test_client().post('/v1/plaid/exchange', headers=headers, json={
        'public_token': 'public-sandbox-00000000-0000-0000-0000-000000000000',
    })
    assert resp.status_code == 400
    assert resp.get_json()['error']['code'] == 'bad_request'
