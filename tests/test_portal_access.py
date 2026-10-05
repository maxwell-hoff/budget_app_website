import types
from datetime import datetime, timedelta, timezone

import pytest

import billing
from billing import PAST_DUE_GRACE, has_paid_access
from extensions import db
from models import Subscription, User
from tests.conftest import make_app
from tests.test_billing import STRIPE_CONFIG, add_subscription_row, add_user, logged_in_client

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)


def user_with(**fields):
    sub = Subscription(stripe_customer_id='cus_1', **fields) if fields else None
    return types.SimpleNamespace(subscription=sub)


# --- has_paid_access: every status branch ---

def test_no_user_or_subscription():
    assert has_paid_access(None, NOW) is False
    assert has_paid_access(user_with(), NOW) is False
    assert has_paid_access(user_with(status=None), NOW) is False  # customer created, never subscribed


@pytest.mark.parametrize('status', ['active', 'trialing'])
def test_active_and_trialing(status):
    assert has_paid_access(user_with(status=status, current_period_end=NOW + 10 * DAY), NOW) is True
    # Stripe is the source of truth: a late renewal webhook doesn't cut access off.
    assert has_paid_access(user_with(status=status, current_period_end=NOW - DAY), NOW) is True


@pytest.mark.parametrize('failed_ago, expected', [
    (timedelta(0), True),
    (PAST_DUE_GRACE - timedelta(seconds=1), True),
    (PAST_DUE_GRACE, False),
    (PAST_DUE_GRACE + DAY, False),
])
def test_past_due_grace_period(failed_ago, expected):
    user = user_with(status='past_due', current_period_start=NOW - failed_ago, current_period_end=NOW + 25 * DAY)
    assert has_paid_access(user, NOW) is expected


def test_past_due_without_period_start():
    assert has_paid_access(user_with(status='past_due', current_period_end=NOW + DAY), NOW) is False


@pytest.mark.parametrize('end_offset, expected', [(DAY, True), (timedelta(seconds=1), True), (timedelta(0), False), (-DAY, False)])
def test_canceled_by_customer_keeps_paid_time(end_offset, expected):
    user = user_with(status='canceled', cancellation_reason='cancellation_requested', current_period_end=NOW + end_offset)
    assert has_paid_access(user, NOW) is expected


@pytest.mark.parametrize('reason', ['payment_failed', 'payment_disputed', None])
def test_canceled_for_other_reasons_ends_access(reason):
    user = user_with(status='canceled', cancellation_reason=reason, current_period_end=NOW + 20 * DAY)
    assert has_paid_access(user, NOW) is False


def test_canceled_without_period_end():
    assert has_paid_access(user_with(status='canceled', cancellation_reason='cancellation_requested'), NOW) is False


@pytest.mark.parametrize('status', ['unpaid', 'incomplete', 'incomplete_expired', 'paused', 'something_new'])
def test_other_statuses_have_no_access(status):
    user = user_with(status=status, current_period_start=NOW, current_period_end=NOW + 20 * DAY)
    assert has_paid_access(user, NOW) is False


def test_naive_datetimes_from_sqlite_are_utc():
    user = user_with(status='canceled', cancellation_reason='cancellation_requested',
                     current_period_end=(NOW + DAY).replace(tzinfo=None))
    assert has_paid_access(user, NOW) is True
    user = user_with(status='past_due', current_period_start=(NOW - DAY).replace(tzinfo=None))
    assert has_paid_access(user, NOW) is True


def test_uses_the_current_time_by_default():
    assert has_paid_access(user_with(status='canceled', cancellation_reason='cancellation_requested',
                                      current_period_end=datetime.now(timezone.utc) + DAY)) is True


# --- Account page: the right status and buttons for each state ---

@pytest.fixture
def stripe_app():
    return make_app(ACCOUNTS_ENABLED=True, **STRIPE_CONFIG)


def _now():
    return datetime.now(timezone.utc)


def _fmt(value):
    return f'{value:%B} {value.day}, {value.year}'


PITCH = 'Use the desktop app with your own budgets, including bank syncing through Plaid.'
USED = dict(trial_used_at=lambda: _now() - 60 * DAY)

ACCOUNT_STATES = {
    'never subscribed': (None, 'Not subscribed', ['subscribe'], PITCH),
    'checkout started': (dict(status=None), 'Not subscribed', ['subscribe'], PITCH),
    'active': (dict(status='active', current_period_end=lambda: _now() + 20 * DAY, **USED), 'Active', ['manage'],
               'Renews on {end}.'),
    'trialing': (dict(status='trialing', current_period_end=lambda: _now() + 5 * DAY, **USED), 'Free trial', ['manage'],
                 'Free trial: ends on {end}, then $8.99/month.'),
    'trial canceled': (dict(status='trialing', cancel_at_period_end=True, current_period_end=lambda: _now() + 5 * DAY,
                            **USED),
                       'Free trial', ['manage'], "Free trial: ends on {end}. You won't be charged, and the app stays on until then."),
    'ending': (dict(status='active', cancel_at_period_end=True, current_period_end=lambda: _now() + 9 * DAY),
               'Active', ['manage'], 'Ends on {end}. The app stays on until then.'),
    'past due, in grace': (dict(status='past_due', current_period_start=lambda: _now() - 2 * DAY),
                           'Payment failed', ['manage'], 'Update your payment method by {grace} to keep using the app.'),
    'past due, grace over': (dict(status='past_due', current_period_start=lambda: _now() - 10 * DAY),
                             'Payment failed', ['manage'], 'The app is paused until your payment method is updated.'),
    'canceled, paid time left': (dict(status='canceled', cancellation_reason='cancellation_requested',
                                      current_period_end=lambda: _now() + 4 * DAY),
                                 'Canceled', ['manage', 'subscribe'], 'The app stays on until {end}.'),
    'canceled, over': (dict(status='canceled', cancellation_reason='cancellation_requested',
                            current_period_end=lambda: _now() - DAY),
                       'Not subscribed', ['manage', 'subscribe'], PITCH),
    'canceled for non-payment': (dict(status='canceled', cancellation_reason='payment_failed',
                                      current_period_end=lambda: _now() + 20 * DAY),
                                 'Not subscribed', ['manage', 'subscribe'], PITCH),
    'unpaid': (dict(status='unpaid'), 'Unpaid', ['manage'], 'The app is paused. Use Manage subscription to fix your payment.'),
    'incomplete': (dict(status='incomplete'), 'Incomplete', ['manage'], 'The app is paused.'),
}


@pytest.mark.parametrize('name', list(ACCOUNT_STATES))
def test_account_page_for_each_state(stripe_app, name):
    fields, badge, buttons, note = ACCOUNT_STATES[name]
    user_id = add_user(stripe_app)
    if fields is not None:
        values = {key: value() if callable(value) else value for key, value in fields.items()}
        if values.get('status'):
            values['stripe_subscription_id'] = 'sub_1'
        add_subscription_row(stripe_app, user_id, customer_id='cus_1', **values)
        end, start = values.get('current_period_end'), values.get('current_period_start')
        note = note.format(end=_fmt(end) if end else '', grace=_fmt(start + PAST_DUE_GRACE) if start else '')

    page = logged_in_client(stripe_app).get('/account').data.decode()
    assert f'>{badge}</span>' in page
    assert note in page
    assert ('action="/billing/portal"' in page) is ('manage' in buttons)
    assert ('action="/billing/checkout"' in page) is ('subscribe' in buttons)
    if badge in ('Active', 'Free trial'):
        assert 'Workbench Budgeting, $8.99/month (includes bank syncing)' in page
    assert 'Bank syncing' not in page


def test_account_page_without_stripe_has_no_billing_buttons(accounts_app, make_user):
    make_user(email='payer@example.com', password='correct horse battery')
    page = logged_in_client(accounts_app).get('/account').data
    assert b'/billing/' not in page
    assert b'Subscriptions are coming soon.' in page


# --- Customer Portal ---

@pytest.fixture
def portal_calls(monkeypatch):
    calls = []

    def create_portal_session(customer_id):
        calls.append(customer_id)
        return 'https://billing.stripe.com/p/session/test_1'

    monkeypatch.setattr(billing, 'create_portal_session', create_portal_session)
    return calls


def test_portal_404_when_accounts_disabled():
    assert make_app(**STRIPE_CONFIG).test_client().post('/billing/portal').status_code == 404


def test_portal_404_when_stripe_not_configured(accounts_client):
    assert accounts_client.post('/billing/portal').status_code == 404


def test_portal_requires_login(stripe_app, portal_calls):
    resp = stripe_app.test_client().post('/billing/portal')
    assert resp.status_code == 302
    assert '/login' in resp.headers['Location']
    assert portal_calls == []


def test_portal_requires_csrf_token(portal_calls):
    app = make_app(ACCOUNTS_ENABLED=True, WTF_CSRF_ENABLED=True, **STRIPE_CONFIG)
    user_id = add_user(app)
    add_subscription_row(app, user_id, customer_id='cus_1', stripe_subscription_id='sub_1', status='active')
    client = app.test_client()
    with client.session_transaction() as sess, app.app_context():
        sess['_user_id'] = db.session.get(User, user_id).get_id()
    assert client.post('/billing/portal').status_code == 400
    assert portal_calls == []


def test_portal_redirects_to_stripe(stripe_app, portal_calls):
    user_id = add_user(stripe_app)
    add_subscription_row(stripe_app, user_id, customer_id='cus_1', stripe_subscription_id='sub_1', status='active')
    resp = logged_in_client(stripe_app).post('/billing/portal')
    assert resp.status_code == 303
    assert resp.headers['Location'] == 'https://billing.stripe.com/p/session/test_1'
    assert portal_calls == ['cus_1']


def test_portal_without_subscription(stripe_app, portal_calls):
    add_user(stripe_app)
    resp = logged_in_client(stripe_app).post('/billing/portal', follow_redirects=True)
    assert b"You don&#39;t have a subscription to manage yet." in resp.data
    assert portal_calls == []


def test_portal_stripe_error_shows_message(stripe_app, monkeypatch):
    user_id = add_user(stripe_app)
    add_subscription_row(stripe_app, user_id, customer_id='cus_1', stripe_subscription_id='sub_1', status='active')

    def fail(customer_id):
        raise billing.stripe.InvalidRequestError('No configuration provided', param=None)

    monkeypatch.setattr(billing, 'create_portal_session', fail)
    resp = logged_in_client(stripe_app).post('/billing/portal', follow_redirects=True)
    assert resp.status_code == 200
    assert b'We couldn&#39;t reach our payment provider.' in resp.data


def test_checkout_stripe_error_shows_message(stripe_app, monkeypatch):
    user_id = add_user(stripe_app)

    def fail(user):
        raise billing.stripe.InvalidRequestError('the product tax code is missing', param=None)

    monkeypatch.setattr(billing, 'create_customer', fail)
    resp = logged_in_client(stripe_app).post('/billing/checkout', follow_redirects=True)
    assert resp.status_code == 200
    assert b'We couldn&#39;t reach our payment provider.' in resp.data
    with stripe_app.app_context():
        assert db.session.get(User, user_id).subscription is None


def test_portal_session_params(stripe_app, monkeypatch):
    seen = {}

    class Sessions:
        def create(self, params):
            seen.update(params)
            return types.SimpleNamespace(url='https://billing.stripe.com/p/session/x')

    client = types.SimpleNamespace(v1=types.SimpleNamespace(billing_portal=types.SimpleNamespace(sessions=Sessions())))
    monkeypatch.setattr(billing, '_stripe', lambda: client)
    stripe_app.config['PUBLIC_BASE_URL'] = 'https://workbenchbudgeting.com'
    with stripe_app.test_request_context():
        assert billing.create_portal_session('cus_1') == 'https://billing.stripe.com/p/session/x'
    assert seen == {'customer': 'cus_1', 'return_url': 'https://workbenchbudgeting.com/account'}
