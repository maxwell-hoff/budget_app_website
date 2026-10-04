import hashlib
import hmac
import itertools
import json
import time
import types
from datetime import datetime, timezone

import pytest

import billing
from extensions import db
from models import StripeEvent, Subscription, User
from tests.conftest import make_app

WEBHOOK_SECRET = 'whsec_test_secret'
STRIPE_CONFIG = {
    'STRIPE_SECRET_KEY': 'sk_test_dummy',
    'STRIPE_PRICE_ID': 'price_test_899',
    'STRIPE_WEBHOOK_SECRET': WEBHOOK_SECRET,
}
PERIOD_END = 1793491200  # 2026-11-01T00:00:00Z
PASSWORD = 'correct horse battery'


class FakeStripe:
    """Stands in for the three Stripe API calls billing.py makes."""

    def __init__(self):
        self.customers = []
        self.sessions = []
        self.subscriptions = {}
        self.fetches = []
        self._ids = itertools.count(1)

    def create_customer(self, user):
        customer_id = f'cus_{next(self._ids)}'
        self.customers.append((customer_id, user.email))
        return customer_id

    def create_checkout_session(self, customer_id, user):
        self.sessions.append((customer_id, user.id))
        return f'https://checkout.stripe.com/c/pay/cs_test_{len(self.sessions)}'

    def fetch_subscription(self, subscription_id):
        self.fetches.append(subscription_id)
        return self.subscriptions[subscription_id]

    def set_subscription(self, sub_id, customer_id, status='active', cancel_at_period_end=False,
                         legacy_shape=False, period_end=PERIOD_END, cancel_at=None, cancellation_reason=None):
        period_start = period_end - 30 * 86400
        sub = {
            'id': sub_id, 'object': 'subscription', 'customer': customer_id, 'status': status,
            'cancel_at_period_end': cancel_at_period_end, 'cancel_at': cancel_at,
            'cancellation_details': {'reason': cancellation_reason, 'comment': None, 'feedback': None},
            'items': {'object': 'list', 'data': [
                {'id': 'si_1', 'current_period_start': period_start, 'current_period_end': period_end},
            ]},
        }
        if legacy_shape:
            sub['current_period_start'] = period_start
            sub['current_period_end'] = period_end
            sub['items']['data'][0].pop('current_period_start')
            sub['items']['data'][0].pop('current_period_end')
        self.subscriptions[sub_id] = sub
        return sub


@pytest.fixture
def stripe_app():
    return make_app(ACCOUNTS_ENABLED=True, **STRIPE_CONFIG)


@pytest.fixture
def fake_stripe(monkeypatch):
    fake = FakeStripe()
    for name in ('create_customer', 'create_checkout_session', 'fetch_subscription'):
        monkeypatch.setattr(billing, name, getattr(fake, name))
    return fake


def add_user(app, email='payer@example.com'):
    with app.app_context():
        user = User(email=email)
        user.set_password(PASSWORD)
        db.session.add(user)
        db.session.commit()
        return user.id


def logged_in_client(app, email='payer@example.com'):
    client = app.test_client()
    client.post('/login', data={'email': email, 'password': PASSWORD})
    return client


def add_subscription_row(app, user_id, customer_id='cus_existing', **fields):
    with app.app_context():
        db.session.add(Subscription(user_id=user_id, stripe_customer_id=customer_id, **fields))
        db.session.commit()


def row(app, user_id):
    with app.app_context():
        sub = db.session.scalar(db.select(Subscription).filter_by(user_id=user_id))
        if sub is None:
            return None
        return {
            'customer': sub.stripe_customer_id, 'subscription': sub.stripe_subscription_id,
            'status': sub.status, 'period_end': sub.current_period_end,
            'cancel_at_period_end': sub.cancel_at_period_end,
        }


def sign(payload, secret=WEBHOOK_SECRET, timestamp=None):
    timestamp = int(time.time()) if timestamp is None else timestamp
    signature = hmac.new(secret.encode(), f'{timestamp}.{payload}'.encode(), hashlib.sha256).hexdigest()
    return f't={timestamp},v1={signature}'


_event_ids = itertools.count(1)


def event(event_type, obj, event_id=None):
    return {
        'id': event_id or f'evt_{next(_event_ids)}', 'object': 'event', 'type': event_type,
        'api_version': '2026-09-30.endive', 'data': {'object': obj},
    }


def send(client, evt, secret=WEBHOOK_SECRET, timestamp=None, signature=None):
    payload = json.dumps(evt)
    header = signature if signature is not None else sign(payload, secret, timestamp)
    return client.post('/stripe/webhook', data=payload, headers={'Stripe-Signature': header},
                       content_type='application/json')


def checkout_completed(customer_id, sub_id, user_id=None):
    return event('checkout.session.completed', {
        'id': 'cs_test_1', 'object': 'checkout.session', 'mode': 'subscription',
        'customer': customer_id, 'subscription': sub_id,
        'client_reference_id': str(user_id) if user_id else None,
    })


def subscription_event(event_type, sub):
    return event(event_type, sub)


# --- Feature flags ---

@pytest.mark.parametrize('path', ['/billing/checkout', '/stripe/webhook'])
def test_routes_404_when_accounts_disabled(path):
    app = make_app(**STRIPE_CONFIG)
    assert app.test_client().post(path).status_code == 404


@pytest.mark.parametrize('missing', ['STRIPE_SECRET_KEY', 'STRIPE_PRICE_ID', 'STRIPE_WEBHOOK_SECRET'])
def test_routes_404_when_stripe_not_configured(missing):
    app = make_app(ACCOUNTS_ENABLED=True, **{**STRIPE_CONFIG, missing: ''})
    client = app.test_client()
    assert client.post('/billing/checkout').status_code == 404
    assert client.post('/stripe/webhook').status_code == 404


def test_account_page_without_stripe_shows_coming_soon(accounts_app, make_user):
    make_user(email='payer@example.com', password=PASSWORD)
    page = logged_in_client(accounts_app).get('/account').data
    assert b'Bank syncing subscriptions are coming soon.' in page
    assert b'/billing/checkout' not in page


# --- Checkout ---

def test_checkout_requires_login(stripe_app, fake_stripe):
    resp = stripe_app.test_client().post('/billing/checkout')
    assert resp.status_code == 302
    assert '/login' in resp.headers['Location']
    assert fake_stripe.sessions == []


def test_checkout_requires_csrf_token(fake_stripe):
    app = make_app(ACCOUNTS_ENABLED=True, WTF_CSRF_ENABLED=True, **STRIPE_CONFIG)
    add_user(app)
    client = app.test_client()
    with client.session_transaction() as sess, app.app_context():
        user = db.session.scalar(db.select(User))
        sess['_user_id'] = user.get_id()
        sess['_fresh'] = True
    assert client.post('/billing/checkout').status_code == 400
    assert fake_stripe.customers == []


def test_account_page_shows_subscribe_button(stripe_app, fake_stripe):
    add_user(stripe_app)
    page = logged_in_client(stripe_app).get('/account').data
    assert b'Not subscribed' in page
    assert b'action="/billing/checkout"' in page
    assert b'Subscribe for $8.99/month' in page


def test_checkout_creates_customer_and_redirects_to_stripe(stripe_app, fake_stripe):
    user_id = add_user(stripe_app)
    client = logged_in_client(stripe_app)

    resp = client.post('/billing/checkout')
    assert resp.status_code == 303
    assert resp.headers['Location'] == 'https://checkout.stripe.com/c/pay/cs_test_1'
    assert fake_stripe.customers == [('cus_1', 'payer@example.com')]
    assert fake_stripe.sessions == [('cus_1', user_id)]
    assert row(stripe_app, user_id) == {
        'customer': 'cus_1', 'subscription': None, 'status': None, 'period_end': None,
        'cancel_at_period_end': False,
    }


def test_checkout_reuses_the_customer(stripe_app, fake_stripe):
    user_id = add_user(stripe_app)
    client = logged_in_client(stripe_app)
    client.post('/billing/checkout')
    client.post('/billing/checkout')  # e.g. they backed out of the first Checkout page
    assert len(fake_stripe.customers) == 1
    assert fake_stripe.sessions == [('cus_1', user_id), ('cus_1', user_id)]


@pytest.mark.parametrize('status', sorted(billing.LIVE_STATUSES))
def test_checkout_refused_while_subscribed(stripe_app, fake_stripe, status):
    user_id = add_user(stripe_app)
    add_subscription_row(stripe_app, user_id, stripe_subscription_id='sub_1', status=status)
    resp = logged_in_client(stripe_app).post('/billing/checkout', follow_redirects=True)
    assert b'You already have a subscription.' in resp.data
    assert fake_stripe.sessions == []


def test_checkout_allowed_again_after_cancellation(stripe_app, fake_stripe):
    user_id = add_user(stripe_app)
    add_subscription_row(stripe_app, user_id, stripe_subscription_id='sub_old', status='canceled')
    resp = logged_in_client(stripe_app).post('/billing/checkout')
    assert resp.status_code == 303
    assert fake_stripe.customers == []
    assert fake_stripe.sessions == [('cus_existing', user_id)]


def test_checkout_success_page_message(stripe_app, fake_stripe):
    add_user(stripe_app)
    page = logged_in_client(stripe_app).get('/account?checkout=success').data
    assert b'It can take a few seconds to show up here' in page


# --- Webhook signature ---

def test_webhook_rejects_missing_signature(stripe_app, fake_stripe):
    resp = stripe_app.test_client().post('/stripe/webhook', data='{}', content_type='application/json')
    assert resp.status_code == 400


def test_webhook_rejects_wrong_secret(stripe_app, fake_stripe):
    user_id = add_user(stripe_app)
    add_subscription_row(stripe_app, user_id, customer_id='cus_1')
    fake_stripe.set_subscription('sub_1', 'cus_1')
    resp = send(stripe_app.test_client(), checkout_completed('cus_1', 'sub_1'), secret='whsec_attacker')
    assert resp.status_code == 400
    assert row(stripe_app, user_id)['status'] is None
    assert fake_stripe.fetches == []


def test_webhook_rejects_tampered_payload(stripe_app, fake_stripe):
    evt = checkout_completed('cus_1', 'sub_1')
    header = sign(json.dumps(evt))
    evt['data']['object']['customer'] = 'cus_other'
    assert send(stripe_app.test_client(), evt, signature=header).status_code == 400


def test_webhook_rejects_old_timestamp(stripe_app, fake_stripe):
    evt = checkout_completed('cus_1', 'sub_1')
    resp = send(stripe_app.test_client(), evt, timestamp=int(time.time()) - 600)
    assert resp.status_code == 400


def test_webhook_needs_no_csrf_token():
    app = make_app(ACCOUNTS_ENABLED=True, WTF_CSRF_ENABLED=True, **STRIPE_CONFIG)
    assert send(app.test_client(), event('customer.created', {'id': 'cus_1'})).status_code == 200


# --- Webhook events ---

def test_checkout_completed_activates_subscription(stripe_app, fake_stripe):
    user_id = add_user(stripe_app)
    add_subscription_row(stripe_app, user_id, customer_id='cus_1')
    fake_stripe.set_subscription('sub_1', 'cus_1')

    resp = send(stripe_app.test_client(), checkout_completed('cus_1', 'sub_1', user_id))
    assert resp.status_code == 200
    assert row(stripe_app, user_id) == {
        'customer': 'cus_1', 'subscription': 'sub_1', 'status': 'active',
        'period_end': datetime(2026, 11, 1),  # SQLite returns naive UTC
        'cancel_at_period_end': False,
    }


def test_legacy_api_shape_period_end(stripe_app, fake_stripe):
    user_id = add_user(stripe_app)
    add_subscription_row(stripe_app, user_id, customer_id='cus_1')
    fake_stripe.set_subscription('sub_1', 'cus_1', legacy_shape=True)
    send(stripe_app.test_client(), subscription_event('customer.subscription.created', fake_stripe.subscriptions['sub_1']))
    assert row(stripe_app, user_id)['period_end'] == datetime(2026, 11, 1)


def test_full_lifecycle_subscribe_cancel_end(stripe_app, fake_stripe):
    user_id = add_user(stripe_app)
    client = logged_in_client(stripe_app)
    client.post('/billing/checkout')

    sub = fake_stripe.set_subscription('sub_1', 'cus_1')
    send(client, subscription_event('customer.subscription.created', sub))
    send(client, checkout_completed('cus_1', 'sub_1', user_id))
    assert row(stripe_app, user_id)['status'] == 'active'
    page = client.get('/account').data
    assert b'Active' in page
    assert b'Renews on November 1, 2026.' in page
    assert b'/billing/checkout' not in page

    sub = fake_stripe.set_subscription('sub_1', 'cus_1', cancel_at_period_end=True)
    send(client, subscription_event('customer.subscription.updated', sub))
    assert row(stripe_app, user_id)['cancel_at_period_end'] is True
    assert b'Ends on November 1, 2026.' in client.get('/account').data

    sub = fake_stripe.set_subscription('sub_1', 'cus_1', status='canceled', cancel_at_period_end=True)
    send(client, subscription_event('customer.subscription.deleted', sub))
    assert row(stripe_app, user_id)['status'] == 'canceled'
    assert row(stripe_app, user_id)['cancel_at_period_end'] is False
    assert b'Subscribe for $8.99/month' in client.get('/account').data


def test_cancel_at_date_counts_as_ending(stripe_app, fake_stripe):
    user_id = add_user(stripe_app)
    add_subscription_row(stripe_app, user_id, customer_id='cus_1')
    sub = fake_stripe.set_subscription('sub_1', 'cus_1', cancel_at=PERIOD_END)
    send(stripe_app.test_client(), subscription_event('customer.subscription.updated', sub))
    assert row(stripe_app, user_id)['cancel_at_period_end'] is True


def test_row_follows_stripe_not_the_event_body(stripe_app, fake_stripe):
    """An old 'active' event delivered after the subscription was canceled must not revive it."""
    user_id = add_user(stripe_app)
    add_subscription_row(stripe_app, user_id, customer_id='cus_1')
    stale = fake_stripe.set_subscription('sub_1', 'cus_1')
    fake_stripe.set_subscription('sub_1', 'cus_1', status='canceled')
    send(stripe_app.test_client(), subscription_event('customer.subscription.updated', stale))
    assert row(stripe_app, user_id)['status'] == 'canceled'


@pytest.mark.parametrize('shape', ['legacy', 'parent'])
def test_invoice_payment_failed_marks_past_due(stripe_app, fake_stripe, shape):
    user_id = add_user(stripe_app)
    add_subscription_row(stripe_app, user_id, customer_id='cus_1', stripe_subscription_id='sub_1', status='active')
    fake_stripe.set_subscription('sub_1', 'cus_1', status='past_due')
    invoice = {'id': 'in_1', 'object': 'invoice', 'customer': 'cus_1'}
    if shape == 'legacy':
        invoice['subscription'] = 'sub_1'
    else:
        invoice['parent'] = {'type': 'subscription_details', 'subscription_details': {'subscription': 'sub_1'}}

    send(stripe_app.test_client(), event('invoice.payment_failed', invoice))
    assert row(stripe_app, user_id)['status'] == 'past_due'
    assert b'Payment failed' in logged_in_client(stripe_app).get('/account').data


def test_invoice_paid_updates_renewal_date(stripe_app, fake_stripe):
    user_id = add_user(stripe_app)
    add_subscription_row(stripe_app, user_id, customer_id='cus_1', stripe_subscription_id='sub_1', status='past_due')
    fake_stripe.set_subscription('sub_1', 'cus_1', period_end=PERIOD_END + 30 * 86400)
    invoice = {'id': 'in_2', 'object': 'invoice', 'customer': 'cus_1', 'subscription': 'sub_1'}
    send(stripe_app.test_client(), event('invoice.paid', invoice))
    assert row(stripe_app, user_id)['status'] == 'active'
    assert row(stripe_app, user_id)['period_end'] == datetime(2026, 12, 1)


def test_invoice_without_subscription_is_ignored(stripe_app, fake_stripe):
    resp = send(stripe_app.test_client(), event('invoice.paid', {'id': 'in_3', 'customer': 'cus_1'}))
    assert resp.status_code == 200
    assert fake_stripe.fetches == []


def test_duplicate_events_are_ignored(stripe_app, fake_stripe):
    user_id = add_user(stripe_app)
    add_subscription_row(stripe_app, user_id, customer_id='cus_1')
    fake_stripe.set_subscription('sub_1', 'cus_1')
    evt = checkout_completed('cus_1', 'sub_1', user_id)
    client = stripe_app.test_client()

    assert send(client, evt).status_code == 200
    resp = send(client, evt)
    assert resp.status_code == 200
    assert resp.get_json()['duplicate'] is True
    assert fake_stripe.fetches == ['sub_1']
    with stripe_app.app_context():
        assert db.session.query(StripeEvent).count() == 1


def test_unknown_customer_is_ignored(stripe_app, fake_stripe):
    fake_stripe.set_subscription('sub_1', 'cus_unknown')
    resp = send(stripe_app.test_client(), subscription_event('customer.subscription.created',
                                                             fake_stripe.subscriptions['sub_1']))
    assert resp.status_code == 200
    with stripe_app.app_context():
        assert db.session.query(Subscription).count() == 0


def test_unknown_customer_linked_through_client_reference_id(stripe_app, fake_stripe):
    user_id = add_user(stripe_app)
    fake_stripe.set_subscription('sub_1', 'cus_new')
    send(stripe_app.test_client(), checkout_completed('cus_new', 'sub_1', user_id))
    assert row(stripe_app, user_id)['customer'] == 'cus_new'
    assert row(stripe_app, user_id)['status'] == 'active'


def test_customer_is_not_moved_to_a_user_who_already_has_one(stripe_app, fake_stripe):
    user_id = add_user(stripe_app)
    add_subscription_row(stripe_app, user_id, customer_id='cus_1')
    fake_stripe.set_subscription('sub_9', 'cus_other')
    send(stripe_app.test_client(), checkout_completed('cus_other', 'sub_9', user_id))
    assert row(stripe_app, user_id)['customer'] == 'cus_1'
    assert row(stripe_app, user_id)['subscription'] is None


def test_old_subscription_events_do_not_overwrite_a_new_one(stripe_app, fake_stripe):
    user_id = add_user(stripe_app)
    add_subscription_row(stripe_app, user_id, customer_id='cus_1', stripe_subscription_id='sub_new', status='active')
    old = fake_stripe.set_subscription('sub_old', 'cus_1', status='canceled')
    send(stripe_app.test_client(), subscription_event('customer.subscription.deleted', old))
    assert row(stripe_app, user_id)['subscription'] == 'sub_new'
    assert row(stripe_app, user_id)['status'] == 'active'


def test_resubscribing_replaces_a_canceled_subscription(stripe_app, fake_stripe):
    user_id = add_user(stripe_app)
    add_subscription_row(stripe_app, user_id, customer_id='cus_1', stripe_subscription_id='sub_old', status='canceled')
    fake_stripe.set_subscription('sub_new', 'cus_1')
    send(stripe_app.test_client(), checkout_completed('cus_1', 'sub_new', user_id))
    assert row(stripe_app, user_id)['subscription'] == 'sub_new'
    assert row(stripe_app, user_id)['status'] == 'active'


def test_non_subscription_checkout_is_ignored(stripe_app, fake_stripe):
    evt = event('checkout.session.completed', {'id': 'cs_2', 'mode': 'payment', 'customer': 'cus_1'})
    assert send(stripe_app.test_client(), evt).status_code == 200
    assert fake_stripe.fetches == []


def test_unhandled_event_types_are_acknowledged(stripe_app, fake_stripe):
    evt = event('customer.created', {'id': 'cus_1'})
    assert send(stripe_app.test_client(), evt).status_code == 200
    with stripe_app.app_context():
        assert db.session.get(StripeEvent, evt['id']).type == 'customer.created'


def test_stripe_errors_return_500_so_stripe_retries(stripe_app, fake_stripe, monkeypatch):
    user_id = add_user(stripe_app)
    add_subscription_row(stripe_app, user_id, customer_id='cus_1')

    def boom(subscription_id):
        raise billing.stripe.APIConnectionError('network down')

    monkeypatch.setattr(billing, 'fetch_subscription', boom)
    stripe_app.config['PROPAGATE_EXCEPTIONS'] = False
    evt = checkout_completed('cus_1', 'sub_1', user_id)
    assert send(stripe_app.test_client(), evt).status_code == 500
    with stripe_app.app_context():
        assert db.session.get(StripeEvent, evt['id']) is None


def test_stripe_wrappers_send_the_right_params(stripe_app, monkeypatch):
    calls = {}

    class Recorder:
        def __init__(self, name, result):
            self.name, self.result = name, result

        def create(self, params):
            calls[self.name] = params
            return self.result

        def retrieve(self, subscription_id):
            calls[self.name] = subscription_id
            return self.result

    client = types.SimpleNamespace(v1=types.SimpleNamespace(
        customers=Recorder('customer', types.SimpleNamespace(id='cus_real')),
        checkout=types.SimpleNamespace(sessions=Recorder('session', types.SimpleNamespace(url='https://checkout.example'))),
        subscriptions=Recorder('subscription', billing.stripe.StripeObject.construct_from({'id': 'sub_1', 'status': 'active'}, 'k')),
    ))
    monkeypatch.setattr(billing, '_stripe', lambda: client)
    stripe_app.config['PUBLIC_BASE_URL'] = 'https://workbenchbudgeting.com'

    with stripe_app.test_request_context():
        user = User(id=7, email='payer@example.com')
        assert billing.create_customer(user) == 'cus_real'
        assert billing.create_checkout_session('cus_real', user) == 'https://checkout.example'
        assert billing.fetch_subscription('sub_1') == {'id': 'sub_1', 'status': 'active'}

    assert calls['customer'] == {'email': 'payer@example.com', 'metadata': {'user_id': '7'}}
    assert calls['session'] == {
        'mode': 'subscription',
        'customer': 'cus_real',
        'client_reference_id': '7',
        'line_items': [{'price': 'price_test_899', 'quantity': 1}],
        'subscription_data': {'metadata': {'user_id': '7'}},
        'success_url': 'https://workbenchbudgeting.com/account?checkout=success',
        'cancel_url': 'https://workbenchbudgeting.com/account?checkout=canceled',
    }
    assert calls['subscription'] == 'sub_1'


def test_period_bounds_are_utc():
    sub = {'items': {'data': [{'current_period_start': PERIOD_END - 86400, 'current_period_end': PERIOD_END}]}}
    assert billing._period_bound(sub, 'current_period_end', max) == datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert billing._period_bound(sub, 'current_period_start', min) == datetime(2026, 10, 31, tzinfo=timezone.utc)
    assert billing._period_bound({'items': {'data': []}}, 'current_period_end', max) is None


def test_sync_records_period_start_and_cancellation_reason(stripe_app, fake_stripe):
    user_id = add_user(stripe_app)
    add_subscription_row(stripe_app, user_id, customer_id='cus_1', stripe_subscription_id='sub_1', status='active')
    sub = fake_stripe.set_subscription('sub_1', 'cus_1', status='canceled', cancellation_reason='payment_failed')
    send(stripe_app.test_client(), subscription_event('customer.subscription.deleted', sub))
    with stripe_app.app_context():
        saved = db.session.scalar(db.select(Subscription))
        assert saved.current_period_start == datetime(2026, 10, 2)
        assert saved.cancellation_reason == 'payment_failed'

    # A reason only matters once the subscription is canceled.
    sub = fake_stripe.set_subscription('sub_1', 'cus_1', status='active', cancellation_reason='cancellation_requested')
    send(stripe_app.test_client(), subscription_event('customer.subscription.updated', sub))
    with stripe_app.app_context():
        assert db.session.scalar(db.select(Subscription)).cancellation_reason is None
