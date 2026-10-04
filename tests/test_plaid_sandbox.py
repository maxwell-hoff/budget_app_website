"""Live tests against Plaid's sandbox. They run only when PLAID_SANDBOX_CLIENT_ID and
PLAID_SANDBOX_SECRET are in the environment or .env, and are skipped otherwise. Separate
names from PLAID_CLIENT_ID / PLAID_SECRET so production keys are never used here.

    pytest -m plaid_sandbox -v
"""
import os

import pytest
from cryptography.fernet import Fernet
from plaid.model.products import Products
from plaid.model.sandbox_public_token_create_request import SandboxPublicTokenCreateRequest

import config  # noqa: F401  (loads .env into os.environ)
import plaid_api
from extensions import db
from models import PlaidItem
from tests.conftest import make_app
from tests.test_app_sessions import add_user, sign_in
from tests.test_plaid_api import subscribe

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


def test_reused_public_token_is_rejected_by_sandbox(live_app):
    user_id = add_user(live_app)
    subscribe(live_app, user_id)
    headers = {'Authorization': f'Bearer {sign_in(live_app)}'}
    client = live_app.test_client()
    public_token = sandbox_public_token(live_app)

    first = client.post('/v1/plaid/exchange', headers=headers, json={'public_token': public_token})
    assert first.status_code == 200
    again = client.post('/v1/plaid/exchange', headers=headers, json={'public_token': public_token})
    assert again.status_code == 400
    assert again.get_json()['error']['code'] == 'bad_request'

    item_id = first.get_json()['item']['item_id']
    assert client.delete(f'/v1/plaid/items/{item_id}', headers=headers).status_code == 204
