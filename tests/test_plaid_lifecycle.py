import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import stripe
from cryptography.fernet import Fernet

import billing
import plaid_api
from config import BASE_DIR
from extensions import db
from models import AppSession, OAuthIdentity, PlaidItem, Subscription, User, utcnow
from tests.conftest import log_in_as, make_app
from tests.test_app_sessions import PASSWORD, add_user, password_login, sign_in
from tests.test_billing import STRIPE_CONFIG, FakeStripe, send, subscription_event
from tests.test_plaid_api import (  # noqa: F401  (fake_plaid, plaid_app, subscriber are fixtures)
    ACCESS_TOKEN, assert_api_error, call, fake_plaid, link, plaid_app, plaid_config, plaid_exception, subscribe,
    subscriber,
)
from tests.test_plaid_sync import fake_transactions, page  # noqa: F401  (fake_transactions is a fixture)

PAST = int((utcnow() - timedelta(days=1)).timestamp())
FUTURE = int((utcnow() + timedelta(days=20)).timestamp())


def item_status(app, item_id='item-1'):
    with app.app_context():
        return db.session.scalar(db.select(PlaidItem.status).filter_by(item_id=item_id))


def set_item_status(app, value, item_id='item-1'):
    with app.app_context():
        db.session.scalar(db.select(PlaidItem).filter_by(item_id=item_id)).status = value
        db.session.commit()


def add_item(app, user_id, item_id, access_token):
    with app.app_context():
        db.session.add(PlaidItem(user_id=user_id, item_id=item_id,
                                 access_token_encrypted=plaid_api.encrypt_token(access_token)))
        db.session.commit()


def item_ids(app):
    with app.app_context():
        return sorted(db.session.scalars(db.select(PlaidItem.item_id)))


class FakeRemovals:
    """plaid_api.remove_item that can fail for chosen access tokens."""

    def __init__(self):
        self.removed = []
        self.fail = {}

    def __call__(self, access_token):
        if access_token in self.fail:
            raise self.fail[access_token]
        self.removed.append(access_token)


@pytest.fixture
def removals(monkeypatch):
    fake = FakeRemovals()
    monkeypatch.setattr(plaid_api, 'remove_item', fake)
    return fake


# --- POST /v1/plaid/items/<id>/relink-token ----------------------------------------

@pytest.fixture
def update_tokens(monkeypatch):
    calls = []

    def create_update_link_token(user, access_token):
        calls.append((user.id, access_token))
        return 'link-sandbox-update', datetime(2026, 10, 6, 2, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(plaid_api, 'create_update_link_token', create_update_link_token)
    return calls


def test_relink_token(plaid_app, fake_plaid, subscriber, update_tokens):
    user_id, headers = subscriber
    link(plaid_app, headers)
    resp = call(plaid_app, 'post', '/v1/plaid/items/item-1/relink-token', headers)
    assert resp.status_code == 200
    assert resp.get_json() == {'link_token': 'link-sandbox-update', 'expiration': '2026-10-06T02:00:00Z'}
    assert update_tokens == [(user_id, ACCESS_TOKEN)]
    assert ACCESS_TOKEN not in resp.get_data(as_text=True)


def test_relink_token_for_someone_elses_item_is_404(plaid_app, fake_plaid, subscriber, update_tokens):
    other_id = add_user(plaid_app, email='other@example.com')
    add_item(plaid_app, other_id, 'theirs', 'access-theirs')
    assert_api_error(call(plaid_app, 'post', '/v1/plaid/items/theirs/relink-token', subscriber[1]), 404, 'not_found')
    assert update_tokens == []


def test_relink_token_plaid_errors(plaid_app, fake_plaid, subscriber, monkeypatch):
    headers = subscriber[1]
    link(plaid_app, headers)
    failure = {'exc': plaid_exception('INTERNAL_SERVER_ERROR', status=500)}

    def fail(user, access_token):
        raise failure['exc']

    monkeypatch.setattr(plaid_api, 'create_update_link_token', fail)
    assert_api_error(call(plaid_app, 'post', '/v1/plaid/items/item-1/relink-token', headers), 502, 'plaid_error')
    assert item_status(plaid_app) == 'ok'
    failure['exc'] = plaid_exception('ITEM_NOT_FOUND')
    assert_api_error(call(plaid_app, 'post', '/v1/plaid/items/item-1/relink-token', headers), 502, 'plaid_error')
    assert item_status(plaid_app) == 'error'


def test_update_link_token_request(plaid_app, monkeypatch):
    sent = {}

    class Client:
        def link_token_create(self, req, _request_timeout=None):
            sent['request'] = req.to_dict()
            return SimpleNamespace(link_token='link-x', expiration=None)

    monkeypatch.setattr(plaid_api, '_plaid', Client)
    with plaid_app.app_context():
        assert plaid_api.create_update_link_token(User(id=7, email='u@example.com'), 'access-x') == ('link-x', None)
    req = sent['request']
    assert req['access_token'] == 'access-x'
    assert req['user'] == {'client_user_id': '7'}
    assert 'products' not in req and 'webhook' not in req


@pytest.mark.parametrize('create', ['create_link_token', 'create_update_link_token'])
def test_link_tokens_carry_the_webhook_url_from_public_base_url(monkeypatch, create):
    app = make_app(**plaid_config(PUBLIC_BASE_URL='https://workbenchbudgeting.com'))
    sent = {}

    class Client:
        def link_token_create(self, req, _request_timeout=None):
            sent['request'] = req.to_dict()
            return SimpleNamespace(link_token='link-x', expiration=None)

    monkeypatch.setattr(plaid_api, '_plaid', Client)
    user = User(id=7, email='u@example.com')
    with app.test_request_context(base_url='https://evil.example'):
        args = (user,) if create == 'create_link_token' else (user, 'access-x')
        getattr(plaid_api, create)(*args)
    assert sent['request']['webhook'] == 'https://workbenchbudgeting.com/plaid/webhook'


def test_no_webhook_url_without_public_base_url(plaid_app):
    with plaid_app.test_request_context(base_url='https://evil.example'):
        assert plaid_api.webhook_url() is None


# --- POST /v1/plaid/items/<id>/relink-complete -------------------------------------

@pytest.mark.parametrize('before,after', [
    ('relink_required', 'ok'), ('relink_recommended', 'ok'), ('ok', 'ok'), ('error', 'error'),
])
def test_relink_complete(plaid_app, fake_plaid, subscriber, before, after):
    headers = subscriber[1]
    link(plaid_app, headers)
    set_item_status(plaid_app, before)
    resp = call(plaid_app, 'post', '/v1/plaid/items/item-1/relink-complete', headers)
    assert resp.status_code == 200
    assert resp.get_json()['item']['status'] == after
    assert item_status(plaid_app) == after


def test_relink_complete_for_someone_elses_item_is_404(plaid_app, fake_plaid, subscriber):
    other_id = add_user(plaid_app, email='other@example.com')
    add_item(plaid_app, other_id, 'theirs', 'access-theirs')
    set_item_status(plaid_app, 'relink_required', 'theirs')
    resp = call(plaid_app, 'post', '/v1/plaid/items/theirs/relink-complete', subscriber[1])
    assert_api_error(resp, 404, 'not_found')
    assert item_status(plaid_app, 'theirs') == 'relink_required'


# --- Sync and item status ------------------------------------------------------------

def test_sync_keeps_a_pending_expiration_warning(plaid_app, fake_plaid, subscriber, fake_transactions):
    headers = subscriber[1]
    link(plaid_app, headers)
    set_item_status(plaid_app, 'relink_recommended')
    fake_transactions.pages[ACCESS_TOKEN] = [page([], [])]
    [result] = call(plaid_app, 'post', '/v1/plaid/sync', headers, json={'item_id': 'item-1'}).get_json()['items']
    assert result['item']['status'] == 'relink_recommended' and result['item']['last_synced_at']


@pytest.mark.parametrize('before', ['relink_required', 'error'])
def test_sync_success_clears_other_statuses(plaid_app, fake_plaid, subscriber, fake_transactions, before):
    headers = subscriber[1]
    link(plaid_app, headers)
    set_item_status(plaid_app, before)
    fake_transactions.pages[ACCESS_TOKEN] = [page([], [])]
    assert call(plaid_app, 'post', '/v1/plaid/sync', headers, json={'item_id': 'item-1'}).status_code == 200
    assert item_status(plaid_app) == 'ok'


@pytest.mark.parametrize('code', sorted(plaid_api.ITEM_GONE_ERRORS))
def test_sync_marks_an_item_gone_at_plaid_as_error(plaid_app, fake_plaid, subscriber, fake_transactions, code):
    headers = subscriber[1]
    link(plaid_app, headers)
    fake_transactions.pages[ACCESS_TOKEN] = plaid_exception(code)
    resp = call(plaid_app, 'post', '/v1/plaid/sync', headers, json={'item_id': 'item-1'})
    assert_api_error(resp, 502, 'plaid_error')
    assert item_status(plaid_app) == 'error'


# --- Subscription ends -> items removed at Plaid -------------------------------------

@pytest.fixture
def billing_app():
    return make_app(**plaid_config(**STRIPE_CONFIG))


@pytest.fixture
def fake_stripe(monkeypatch):
    fake = FakeStripe()
    for name in ('create_customer', 'create_checkout_session', 'fetch_subscription'):
        monkeypatch.setattr(billing, name, getattr(fake, name))
    return fake


@pytest.fixture
def paying_user(billing_app):
    """A user with an active subscription (sub_1 / cus_1) and two linked banks."""
    user_id = add_user(billing_app)
    with billing_app.app_context():
        db.session.add(Subscription(
            user_id=user_id, stripe_customer_id='cus_1', stripe_subscription_id='sub_1', status='active',
            current_period_start=utcnow() - timedelta(days=10), current_period_end=utcnow() + timedelta(days=20),
        ))
        db.session.commit()
    add_item(billing_app, user_id, 'item-a', 'access-a')
    add_item(billing_app, user_id, 'item-b', 'access-b')
    return user_id


def stripe_update(app, fake_stripe, event_type='customer.subscription.updated', **fields):
    sub = fake_stripe.set_subscription('sub_1', 'cus_1', **fields)
    resp = send(app.test_client(), subscription_event(event_type, sub))
    assert resp.status_code == 200, resp.data
    return resp


@pytest.mark.parametrize('fields', [
    {'status': 'canceled', 'cancellation_reason': 'cancellation_requested', 'period_end': PAST},
    {'status': 'canceled', 'cancellation_reason': 'payment_failed', 'period_end': FUTURE},
    {'status': 'canceled', 'cancellation_reason': 'payment_disputed', 'period_end': FUTURE},
    {'status': 'incomplete_expired', 'period_end': FUTURE},
])
def test_subscription_ending_removes_items_at_plaid(billing_app, fake_stripe, paying_user, removals, fields):
    stripe_update(billing_app, fake_stripe, 'customer.subscription.deleted', **fields)
    assert sorted(removals.removed) == ['access-a', 'access-b']
    assert item_ids(billing_app) == []


@pytest.mark.parametrize('fields', [
    {'status': 'active', 'period_end': FUTURE},
    {'status': 'active', 'cancel_at_period_end': True, 'period_end': FUTURE},
    {'status': 'past_due', 'period_end': FUTURE},
    {'status': 'past_due', 'period_end': PAST},
    {'status': 'unpaid', 'period_end': FUTURE},
    {'status': 'paused', 'period_end': FUTURE},
    # Canceled at the customer's request but paid up: access lasts until the period end.
    {'status': 'canceled', 'cancellation_reason': 'cancellation_requested', 'period_end': FUTURE},
])
def test_items_kept_while_the_subscription_can_still_pay(billing_app, fake_stripe, paying_user, removals, fields):
    stripe_update(billing_app, fake_stripe, **fields)
    assert removals.removed == []
    assert item_ids(billing_app) == ['item-a', 'item-b']


def test_removal_failure_keeps_the_item_and_the_webhook_succeeds(billing_app, fake_stripe, paying_user, removals):
    removals.fail['access-a'] = plaid_exception('INTERNAL_SERVER_ERROR', status=500)
    stripe_update(billing_app, fake_stripe, 'customer.subscription.deleted', status='canceled',
                  cancellation_reason='payment_failed')
    assert removals.removed == ['access-b']
    assert item_ids(billing_app) == ['item-a']
    with billing_app.app_context():
        assert db.session.scalar(db.select(Subscription.status)) == 'canceled'


def test_item_already_gone_at_plaid_is_deleted_here(billing_app, fake_stripe, paying_user, removals):
    removals.fail['access-a'] = plaid_exception('ITEM_NOT_FOUND')
    stripe_update(billing_app, fake_stripe, 'customer.subscription.deleted', status='canceled',
                  cancellation_reason='payment_failed')
    assert item_ids(billing_app) == []


def test_other_users_items_are_untouched(billing_app, fake_stripe, paying_user, removals):
    other_id = add_user(billing_app, email='other@example.com')
    add_item(billing_app, other_id, 'item-other', 'access-other')
    stripe_update(billing_app, fake_stripe, 'customer.subscription.deleted', status='canceled',
                  cancellation_reason='payment_failed')
    assert item_ids(billing_app) == ['item-other']


def test_no_plaid_calls_when_plaid_is_not_configured(fake_stripe, removals):
    app = make_app(ACCOUNTS_ENABLED=True, **STRIPE_CONFIG)
    user_id = add_user(app)
    with app.app_context():
        db.session.add(Subscription(user_id=user_id, stripe_customer_id='cus_1', stripe_subscription_id='sub_1',
                                    status='active'))
        db.session.add(PlaidItem(user_id=user_id, item_id='item-a', access_token_encrypted='x'))
        db.session.commit()
    stripe_update(app, fake_stripe, 'customer.subscription.deleted', status='canceled',
                  cancellation_reason='payment_failed')
    assert removals.removed == []


def test_subscription_ended_branches():
    def user_with(status, reason=None, end_days=20):
        sub = Subscription(status=status, cancellation_reason=reason,
                           current_period_start=utcnow() - timedelta(days=10),
                           current_period_end=utcnow() + timedelta(days=end_days)) if status != 'none' else None
        return SimpleNamespace(subscription=sub)

    assert billing.subscription_ended(user_with('none'))
    assert billing.subscription_ended(user_with(None))
    assert billing.subscription_ended(user_with('canceled', 'payment_failed'))
    assert billing.subscription_ended(user_with('canceled', 'cancellation_requested', end_days=-1))
    assert billing.subscription_ended(user_with('incomplete_expired'))
    assert not billing.subscription_ended(user_with('canceled', 'cancellation_requested'))
    for status in ('active', 'trialing', 'past_due', 'unpaid', 'incomplete', 'paused'):
        assert not billing.subscription_ended(user_with(status, end_days=-30)), status


# --- flask plaid-remove-lapsed ------------------------------------------------------

def test_cli_removes_items_once_paid_time_runs_out(billing_app, fake_stripe, paying_user, removals):
    # Canceled immediately at the customer's request: no Stripe event comes when the
    # paid-for time ends, so the command picks it up.
    stripe_update(billing_app, fake_stripe, 'customer.subscription.deleted', status='canceled',
                  cancellation_reason='cancellation_requested', period_end=FUTURE)
    assert item_ids(billing_app) == ['item-a', 'item-b']
    runner = billing_app.test_cli_runner()
    assert 'Removed 0 item(s)' in runner.invoke(args=['plaid-remove-lapsed']).output

    with billing_app.app_context():
        db.session.scalar(db.select(Subscription)).current_period_end = utcnow() - timedelta(minutes=1)
        db.session.commit()
    result = runner.invoke(args=['plaid-remove-lapsed'])
    assert result.exit_code == 0
    assert 'Removed 2 item(s); 0 failed' in result.output
    assert item_ids(billing_app) == []


def test_cli_retries_failed_removals(billing_app, fake_stripe, paying_user, removals):
    removals.fail['access-a'] = plaid_exception('INTERNAL_SERVER_ERROR', status=500)
    stripe_update(billing_app, fake_stripe, 'customer.subscription.deleted', status='canceled',
                  cancellation_reason='payment_failed')
    runner = billing_app.test_cli_runner()
    assert 'Removed 0 item(s); 1 failed' in runner.invoke(args=['plaid-remove-lapsed']).output
    del removals.fail['access-a']
    assert 'Removed 1 item(s); 0 failed' in runner.invoke(args=['plaid-remove-lapsed']).output
    assert item_ids(billing_app) == []


def test_cli_leaves_paying_users_alone(billing_app, paying_user, removals):
    assert 'Removed 0 item(s)' in billing_app.test_cli_runner().invoke(args=['plaid-remove-lapsed']).output
    assert removals.removed == []


def test_cli_runs_in_a_fresh_process(tmp_path):
    # In-process tests have used the models before the command runs; the cron job hasn't.
    script = (
        'from extensions import db\n'
        'from serve import create_app\n'
        'app = create_app()\n'
        'with app.app_context():\n'
        '    db.create_all()\n'
        "result = app.test_cli_runner().invoke(args=['plaid-remove-lapsed'])\n"
        'print(result.output)\n'
        'raise SystemExit(result.exit_code)\n'
    )
    env = {
        'PATH': os.environ.get('PATH', ''),
        'DATABASE_URL': f"sqlite:///{tmp_path / 'app.db'}",
        'ACCOUNTS_ENABLED': 'true',
        'PLAID_CLIENT_ID': 'client',
        'PLAID_SECRET': 'secret',
        'PLAID_TOKEN_KEY': Fernet.generate_key().decode(),
    }
    result = subprocess.run([sys.executable, '-c', script], cwd=BASE_DIR, env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert 'Removed 0 item(s); 0 failed' in result.stdout


def test_cli_not_registered_without_plaid():
    app = make_app(ACCOUNTS_ENABLED=True)
    assert app.test_cli_runner().invoke(args=['plaid-remove-lapsed']).exit_code != 0


# --- POST /account/delete ------------------------------------------------------------

@pytest.fixture
def deleted_customers(monkeypatch):
    deleted = []
    monkeypatch.setattr(billing, 'delete_customer', deleted.append)
    return deleted


def web_client(app, email='user@example.com'):
    client = app.test_client()
    password_login(client, email=email)
    return client


def delete_account(client, email='user@example.com', password=PASSWORD):
    data = {'confirm_email': email}
    if password is not None:
        data['current_password'] = password
    return client.post('/account/delete', data=data)


def user_exists(app, email='user@example.com'):
    with app.app_context():
        return db.session.scalar(db.select(User).filter_by(email=email)) is not None


def test_delete_404_when_accounts_disabled(client):
    assert client.post('/account/delete').status_code == 404


def test_delete_requires_login(accounts_client):
    resp = accounts_client.post('/account/delete', data={'confirm_email': 'user@example.com'})
    assert resp.status_code == 302 and resp.headers['Location'].startswith('/login')


def test_delete_requires_csrf_token():
    app = make_app(ACCOUNTS_ENABLED=True, WTF_CSRF_ENABLED=True)
    user_id = add_user(app)
    client = app.test_client()
    log_in_as(app, client, user_id)
    assert client.get('/account').status_code == 200
    assert delete_account(client).status_code == 400
    assert user_exists(app)


def test_delete_everything(billing_app, paying_user, removals, deleted_customers):
    with billing_app.app_context():
        db.session.add(OAuthIdentity(user_id=paying_user, provider='google', provider_subject='g-1',
                                     email='user@example.com'))
        db.session.commit()
    token = sign_in(billing_app)
    client = web_client(billing_app)

    resp = delete_account(client)
    assert resp.status_code == 302 and resp.headers['Location'] == '/login'
    assert 'Your account has been deleted.' in client.get('/login').get_data(as_text=True)
    assert sorted(removals.removed) == ['access-a', 'access-b']
    assert deleted_customers == ['cus_1']
    with billing_app.app_context():
        for model in (User, PlaidItem, Subscription, AppSession, OAuthIdentity):
            assert db.session.scalar(db.select(db.func.count()).select_from(model)) == 0, model
    # Logged out on the web, and the desktop app's session no longer works.
    assert client.get('/account').status_code == 302
    resp = billing_app.test_client().get('/v1/me', headers={'Authorization': f'Bearer {token}'})
    assert resp.status_code == 401


def test_delete_without_subscription_or_banks(accounts_app):
    add_user(accounts_app)
    client = web_client(accounts_app)
    assert delete_account(client).headers['Location'] == '/login'
    assert not user_exists(accounts_app)


def test_delete_google_only_user_needs_only_the_email(accounts_app):
    user_id = add_user(accounts_app, password=None)
    client = accounts_app.test_client()
    with client.session_transaction() as session:
        with accounts_app.app_context():
            session['_user_id'] = db.session.get(User, user_id).get_id()
            session['_fresh'] = True
    assert delete_account(client, password=None).headers['Location'] == '/login'
    assert not user_exists(accounts_app)


def test_delete_email_is_case_insensitive(accounts_app):
    add_user(accounts_app)
    assert delete_account(web_client(accounts_app), email='  User@Example.COM ').headers['Location'] == '/login'
    assert not user_exists(accounts_app)


@pytest.mark.parametrize('email,password,message', [
    ('', PASSWORD, 'Type your email address'),
    ('other@example.com', PASSWORD, "doesn&#39;t match"),
    ('user@example.com', 'wrong password', 'Current password is incorrect.'),
    ('user@example.com', None, 'Current password is incorrect.'),
])
def test_delete_refused_without_confirmation(billing_app, paying_user, removals, deleted_customers,
                                             email, password, message):
    client = web_client(billing_app)
    resp = delete_account(client, email=email, password=password)
    assert resp.status_code == 302 and resp.headers['Location'] == '/account#delete-heading'
    assert message in client.get('/account').get_data(as_text=True)
    assert user_exists(billing_app)
    assert removals.removed == [] and deleted_customers == []


def test_delete_kept_when_plaid_removal_fails(billing_app, paying_user, removals, deleted_customers):
    removals.fail['access-b'] = plaid_exception('INTERNAL_SERVER_ERROR', status=500)
    client = web_client(billing_app)
    resp = delete_account(client)
    assert resp.headers['Location'] == '/account#delete-heading'
    assert "couldn&#39;t disconnect your banks" in client.get('/account').get_data(as_text=True)
    assert user_exists(billing_app)
    assert item_ids(billing_app) == ['item-b']
    assert deleted_customers == []

    del removals.fail['access-b']
    assert delete_account(client).headers['Location'] == '/login'
    assert not user_exists(billing_app)


def test_delete_kept_when_stripe_fails(billing_app, paying_user, removals, monkeypatch):
    def fail(customer_id):
        raise stripe.APIConnectionError('network down')

    monkeypatch.setattr(billing, 'delete_customer', fail)
    client = web_client(billing_app)
    delete_account(client)
    assert "couldn&#39;t reach our payment provider" in client.get('/account').get_data(as_text=True)
    assert user_exists(billing_app)


def test_delete_refused_with_live_subscription_when_stripe_is_off(removals):
    app = make_app(**plaid_config())
    user_id = add_user(app)
    subscribe(app, user_id, 'active')
    delete_account(web_client(app))
    assert user_exists(app)


def test_delete_refused_with_banks_when_plaid_is_off(accounts_app):
    user_id = add_user(accounts_app)
    with accounts_app.app_context():
        db.session.add(PlaidItem(user_id=user_id, item_id='item-a', access_token_encrypted='x'))
        db.session.commit()
    delete_account(web_client(accounts_app))
    assert user_exists(accounts_app)


def test_delete_customer_wrapper(billing_app, monkeypatch):
    calls = []

    class Customers:
        def delete(self, customer_id):
            calls.append(customer_id)
            if customer_id == 'cus_gone':
                raise stripe.InvalidRequestError('No such customer', 'id', code='resource_missing')
            if customer_id == 'cus_bad':
                raise stripe.InvalidRequestError('Bad', 'id', code='parameter_invalid')

    monkeypatch.setattr(billing, '_stripe', lambda: SimpleNamespace(v1=SimpleNamespace(customers=Customers())))
    with billing_app.app_context():
        billing.delete_customer('cus_1')
        billing.delete_customer('cus_gone')
        with pytest.raises(stripe.InvalidRequestError):
            billing.delete_customer('cus_bad')
    assert calls == ['cus_1', 'cus_gone', 'cus_bad']


def test_account_page_shows_delete_form(accounts_app):
    add_user(accounts_app)
    html = web_client(accounts_app).get('/account').get_data(as_text=True)
    assert 'Delete my account' in html and 'name="confirm_email"' in html
    # The change-password form also has a current_password field; the delete form adds a second.
    assert html.count('name="current_password"') == 2
    ids = re.findall(r'\bid="([^"]+)"', html)
    assert len(ids) == len(set(ids))
    assert 'for="delete_current_password"' in html and 'for="delete_confirm_email"' in html


def test_account_page_delete_form_without_password_field_for_google_users(accounts_app):
    user_id = add_user(accounts_app, password=None)
    client = accounts_app.test_client()
    with client.session_transaction() as session:
        with accounts_app.app_context():
            session['_user_id'] = db.session.get(User, user_id).get_id()
            session['_fresh'] = True
    html = client.get('/account').get_data(as_text=True)
    assert 'Delete my account' in html and 'name="current_password"' not in html
