import copy
import json
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import plaid
import pytest
import urllib3
from plaid.model.account_base import AccountBase
from plaid.model.transaction import Transaction
from plaid.model_utils import validate_and_convert_types

import plaid_api
from extensions import db
from models import PlaidItem, Subscription
from tests.test_app_sessions import add_user
from tests.test_plaid_api import (  # noqa: F401  (fake_plaid, plaid_app, subscriber are fixtures)
    ACCESS_TOKEN, assert_api_error, call, fake_plaid, link, plaid_app, plaid_exception, stored_items, subscriber,
)

FIXTURE_PATH = Path(__file__).resolve().parent.parent / 'docs' / 'paid_plaid' / 'fixtures' / 'plaid_sync_response.json'
FIXTURE = json.loads(FIXTURE_PATH.read_text())
FIXTURE_ITEM = FIXTURE['items'][0]
NOW = datetime(2026, 10, 5, 13, 0, tzinfo=timezone.utc)


def as_plaid_models(data, model):
    """What plaid-python's /transactions/get hands back for this JSON."""
    return validate_and_convert_types(
        copy.deepcopy(data), ([model],), ['received_data'], True, True, configuration=plaid.Configuration(),
    )


def page(accounts, transactions, total=None):
    return SimpleNamespace(
        accounts=accounts, transactions=transactions,
        total_transactions=len(transactions) if total is None else total,
    )


class FakeTransactions:
    def __init__(self):
        self.calls = []
        # access token -> list of pages (or an exception) returned in order
        self.pages = {}

    def get_transactions_page(self, access_token, start_date, end_date, offset):
        self.calls.append((access_token, start_date, end_date, offset))
        result = self.pages[access_token]
        if isinstance(result, Exception):
            raise result
        return result.pop(0)


@pytest.fixture
def fake_transactions(monkeypatch):
    fake = FakeTransactions()
    monkeypatch.setattr(plaid_api, 'get_transactions_page', fake.get_transactions_page)
    monkeypatch.setattr(plaid_api, 'utcnow', lambda: NOW)
    return fake


@pytest.fixture
def linked(plaid_app, fake_plaid, subscriber, fake_transactions):
    """Auth headers for a subscriber with one linked item ('item-1', access token ACCESS_TOKEN)."""
    headers = subscriber[1]
    assert link(plaid_app, headers).status_code == 200
    return headers


def fixture_page():
    return page(
        as_plaid_models(FIXTURE_ITEM['accounts'], AccountBase),
        as_plaid_models(FIXTURE_ITEM['transactions'], Transaction),
    )


def sync(app, headers, **body):
    return call(app, 'post', '/v1/plaid/sync', headers, json=body)


def item_row(app, item_id='item-1'):
    with app.app_context():
        item = db.session.scalar(db.select(PlaidItem).filter_by(item_id=item_id))
        return item.status, item.last_synced_at


# --- The shared fixture ----------------------------------------------------------

def test_response_matches_the_shared_fixture(plaid_app, fake_plaid, subscriber, fake_transactions):
    # Plaid's objects for the fixture's data go in; the fixture's JSON must come out
    # exactly, so the desktop can build against the file in docs/paid_plaid/fixtures/.
    headers = subscriber[1]
    expected_item = FIXTURE_ITEM['item']
    fake_plaid.next_item_id = expected_item['item_id']
    link(plaid_app, headers, institution={
        'id': expected_item['institution_id'], 'name': expected_item['institution_name'],
    })
    with plaid_app.app_context():
        db.session.scalar(db.select(PlaidItem)).created_at = datetime(2026, 10, 5, 12, 58, 41, tzinfo=timezone.utc)
        db.session.commit()
    fake_transactions.pages[ACCESS_TOKEN] = [fixture_page()]

    resp = sync(plaid_app, headers, item_id=expected_item['item_id'])
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json() == FIXTURE


def test_fixture_covers_every_desktop_account_type():
    kinds = {account['type'] for account in FIXTURE_ITEM['accounts']}
    assert {'depository', 'credit', 'investment'} <= kinds
    account_ids = {account['account_id'] for account in FIXTURE_ITEM['accounts']}
    assert FIXTURE_ITEM['transactions']
    assert all(t['account_id'] in account_ids for t in FIXTURE_ITEM['transactions'])
    assert any(t['amount'] < 0 for t in FIXTURE_ITEM['transactions'])


# --- POST /v1/plaid/sync (one item) ----------------------------------------------

def test_sync_one_item(plaid_app, linked, fake_transactions):
    fake_transactions.pages[ACCESS_TOKEN] = [fixture_page()]
    resp = sync(plaid_app, linked, item_id='item-1', days_back=30)
    assert resp.status_code == 200
    [result] = resp.get_json()['items']
    assert set(result) == {'item', 'accounts', 'transactions', 'error'}
    assert result['error'] is None
    assert result['item']['item_id'] == 'item-1'
    assert result['item']['last_synced_at'] == '2026-10-05T13:00:00Z'
    assert result['accounts'] == FIXTURE_ITEM['accounts']
    assert result['transactions'] == FIXTURE_ITEM['transactions']
    assert ACCESS_TOKEN not in resp.get_data(as_text=True)
    assert fake_transactions.calls == [(ACCESS_TOKEN, date(2026, 9, 5), date(2026, 10, 5), 0)]
    assert item_row(plaid_app) == ('ok', NOW.replace(tzinfo=None))


def test_days_back_defaults_to_730(plaid_app, linked, fake_transactions):
    fake_transactions.pages[ACCESS_TOKEN] = [page([], [])]
    assert sync(plaid_app, linked, item_id='item-1').status_code == 200
    [(_, start, end, _)] = fake_transactions.calls
    assert (end - start).days == 730 and end == date(2026, 10, 5)


def test_last_synced_at_shows_in_items_list(plaid_app, linked, fake_transactions):
    fake_transactions.pages[ACCESS_TOKEN] = [page([], [])]
    sync(plaid_app, linked, item_id='item-1')
    [item] = call(plaid_app, 'get', '/v1/plaid/items', linked).get_json()['items']
    assert item['last_synced_at'] == '2026-10-05T13:00:00Z'


def test_pages_through_every_transaction(plaid_app, linked, fake_transactions):
    template = FIXTURE_ITEM['transactions'][0]
    txns = [dict(template, transaction_id=f'txn-{n}') for n in range(1200)]
    accounts = FIXTURE_ITEM['accounts']
    fake_transactions.pages[ACCESS_TOKEN] = [
        page(accounts, txns[:500], 1200), page(accounts, txns[500:1000], 1200), page(accounts, txns[1000:], 1200),
    ]
    [result] = sync(plaid_app, linked, item_id='item-1').get_json()['items']
    assert [t['transaction_id'] for t in result['transactions']] == [t['transaction_id'] for t in txns]
    assert result['accounts'] == accounts
    assert [offset for *_, offset in fake_transactions.calls] == [0, 500, 1000]


def test_stops_paging_when_plaid_returns_an_empty_page(plaid_app, linked, fake_transactions):
    txns = [dict(FIXTURE_ITEM['transactions'][0], transaction_id=f'txn-{n}') for n in range(3)]
    fake_transactions.pages[ACCESS_TOKEN] = [page([], txns, 10), page([], [], 10)]
    [result] = sync(plaid_app, linked, item_id='item-1').get_json()['items']
    assert len(result['transactions']) == 3
    assert len(fake_transactions.calls) == 2


def test_item_login_required_is_409_and_marks_the_item(plaid_app, linked, fake_transactions):
    fake_transactions.pages[ACCESS_TOKEN] = plaid_exception('ITEM_LOGIN_REQUIRED')
    assert_api_error(sync(plaid_app, linked, item_id='item-1'), 409, 'plaid_relink_required')
    assert item_row(plaid_app) == ('relink_required', None)
    [item] = call(plaid_app, 'get', '/v1/plaid/items', linked).get_json()['items']
    assert item['status'] == 'relink_required'

    # Once the bank works again, a sync clears the flag.
    fake_transactions.pages[ACCESS_TOKEN] = [page([], [])]
    assert sync(plaid_app, linked, item_id='item-1').status_code == 200
    assert item_row(plaid_app)[0] == 'ok'


def test_product_not_ready_is_503_with_retry_after(plaid_app, linked, fake_transactions):
    fake_transactions.pages[ACCESS_TOKEN] = plaid_exception('PRODUCT_NOT_READY')
    resp = sync(plaid_app, linked, item_id='item-1')
    assert_api_error(resp, 503, 'plaid_not_ready')
    assert resp.headers['Retry-After'] == str(plaid_api.NOT_READY_RETRY_AFTER)
    assert item_row(plaid_app) == ('ok', None)


@pytest.mark.parametrize('failure', [
    plaid_exception('INTERNAL_SERVER_ERROR', status=500),
    plaid_exception('RATE_LIMIT_EXCEEDED', status=429),
    urllib3.exceptions.ReadTimeoutError(None, '/transactions/get', 'read timed out'),
])
def test_other_failures_are_502_and_leave_the_item_alone(plaid_app, linked, fake_transactions, failure):
    fake_transactions.pages[ACCESS_TOKEN] = failure
    assert_api_error(sync(plaid_app, linked, item_id='item-1'), 502, 'plaid_error')
    assert item_row(plaid_app) == ('ok', None)


def test_unknown_or_someone_elses_item_is_404(plaid_app, linked, fake_transactions):
    other_id = add_user(plaid_app, email='other@example.com')
    with plaid_app.app_context():
        db.session.add(PlaidItem(user_id=other_id, item_id='theirs', access_token_encrypted='x'))
        db.session.commit()
    for item_id in ('theirs', 'nope'):
        assert_api_error(sync(plaid_app, linked, item_id=item_id), 404, 'not_found')
    assert fake_transactions.calls == []


@pytest.mark.parametrize('body', [
    [], 'x', {'days_back': 0}, {'days_back': 731}, {'days_back': -5}, {'days_back': '30'},
    {'days_back': 30.0}, {'days_back': True}, {'days_back': None},
    {'item_id': ''}, {'item_id': '  '}, {'item_id': 5},
])
def test_bad_requests(plaid_app, linked, fake_transactions, body):
    assert_api_error(call(plaid_app, 'post', '/v1/plaid/sync', linked, json=body), 400, 'bad_request')
    assert fake_transactions.calls == []


def test_malformed_json_is_400(plaid_app, linked, fake_transactions):
    resp = call(plaid_app, 'post', '/v1/plaid/sync', linked, data='{', content_type='application/json')
    assert_api_error(resp, 400, 'bad_request')


# --- POST /v1/plaid/sync (every item) --------------------------------------------

def test_sync_all_items_without_a_body(plaid_app, linked, fake_transactions, monkeypatch):
    second_token = 'access-sandbox-second'
    monkeypatch.setattr(plaid_api, 'exchange_public_token', lambda public_token: (second_token, 'item-2'))
    link(plaid_app, linked, public_token='public-sandbox-2')
    fake_transactions.pages[ACCESS_TOKEN] = [fixture_page()]
    fake_transactions.pages[second_token] = [page([], [])]

    resp = call(plaid_app, 'post', '/v1/plaid/sync', linked)
    assert resp.status_code == 200
    results = resp.get_json()['items']
    assert [r['item']['item_id'] for r in results] == ['item-1', 'item-2']
    assert results[0]['transactions'] == FIXTURE_ITEM['transactions']
    assert results[1]['transactions'] == [] and results[1]['error'] is None
    assert all((end - start).days == 730 for _, start, end, _ in fake_transactions.calls)


def test_one_failing_item_does_not_fail_the_others(plaid_app, linked, fake_transactions, monkeypatch):
    second_token = 'access-sandbox-second'
    monkeypatch.setattr(plaid_api, 'exchange_public_token', lambda public_token: (second_token, 'item-2'))
    link(plaid_app, linked, public_token='public-sandbox-2')
    fake_transactions.pages[ACCESS_TOKEN] = plaid_exception('ITEM_LOGIN_REQUIRED')
    fake_transactions.pages[second_token] = [fixture_page()]

    resp = sync(plaid_app, linked, days_back=90)
    assert resp.status_code == 200
    first, second = resp.get_json()['items']
    assert first['error']['code'] == 'plaid_relink_required'
    assert first['item']['status'] == 'relink_required'
    assert first['accounts'] == [] and first['transactions'] == []
    assert second['error'] is None and second['transactions'] == FIXTURE_ITEM['transactions']
    assert [row[5] for row in stored_items(plaid_app)] == ['relink_required', 'ok']


def test_errors_are_reported_per_item(plaid_app, linked, fake_transactions):
    fake_transactions.pages[ACCESS_TOKEN] = plaid_exception('PRODUCT_NOT_READY')
    [result] = sync(plaid_app, linked).get_json()['items']
    assert result['error']['code'] == 'plaid_not_ready'
    fake_transactions.pages[ACCESS_TOKEN] = plaid_exception('INTERNAL_SERVER_ERROR', status=500)
    [result] = sync(plaid_app, linked).get_json()['items']
    assert result['error'] == {'code': 'plaid_error', 'message': plaid_api.PLAID_ERROR_MESSAGE}


def test_sync_all_only_includes_the_callers_items(plaid_app, fake_plaid, subscriber, fake_transactions):
    other_id = add_user(plaid_app, email='other@example.com')
    with plaid_app.app_context():
        db.session.add(PlaidItem(user_id=other_id, item_id='theirs', access_token_encrypted='x'))
        db.session.commit()
    assert sync(plaid_app, subscriber[1]).get_json() == {'items': []}
    assert fake_transactions.calls == []


# --- The Plaid request -----------------------------------------------------------

def test_transactions_get_request(plaid_app, monkeypatch):
    sent = {}

    class Client:
        def transactions_get(self, req, _request_timeout=None):
            sent['timeout'] = _request_timeout
            sent['request'] = req.to_dict()
            return page([], [])

    monkeypatch.setattr(plaid_api, '_plaid', Client)
    with plaid_app.app_context():
        assert plaid_api.fetch_transactions('access-x', 30) == ([], [])
    req = sent['request']
    assert sent['timeout'] == plaid_api.PLAID_TIMEOUT
    assert req['access_token'] == 'access-x'
    assert (req['end_date'] - req['start_date']).days == 30
    assert req['options'] == {'count': 500, 'offset': 0}


def test_402_after_subscription_lapses(plaid_app, linked, fake_transactions):
    with plaid_app.app_context():
        db.session.scalar(db.select(Subscription)).status = 'unpaid'
        db.session.commit()
    assert_api_error(sync(plaid_app, linked), 402, 'plaid_access_required')
    assert fake_transactions.calls == []
