"""Step 13a: whole-app paid access (access_until, /v1/me) and the 1-week free trial."""
import types
from datetime import datetime, timedelta, timezone

import pytest

import billing
import config
from billing import PAST_DUE_GRACE, access_until, has_paid_access
from extensions import db
from models import Subscription, User
from tests import test_app_sessions as sessions
from tests.conftest import make_app
from tests.test_billing import (
    PERIOD_END, STRIPE_CONFIG, FakeStripe, add_subscription_row, add_user, event, logged_in_client, send,
    subscription_event,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)
TRIAL_START = PERIOD_END - 7 * 86400  # 2026-10-25T00:00:00Z


def user_with(**fields):
    sub = Subscription(stripe_customer_id='cus_1', **fields) if fields else None
    return types.SimpleNamespace(subscription=sub)


@pytest.fixture
def stripe_app():
    return make_app(ACCOUNTS_ENABLED=True, **STRIPE_CONFIG)


@pytest.fixture
def fake_stripe(monkeypatch):
    fake = FakeStripe()
    for name in ('create_customer', 'create_checkout_session', 'fetch_subscription'):
        monkeypatch.setattr(billing, name, getattr(fake, name))
    return fake


def stored(app, user_id):
    with app.app_context():
        return db.session.get(User, user_id).subscription


# --- access_until: every subscription state ---

ACCESS_STATES = {
    # name: (subscription fields or None, has access, expected access_until)
    'no subscription': (None, False, None),
    'checkout started': (dict(status=None), False, None),
    'active': (dict(status='active', current_period_end=NOW + 20 * DAY), True, NOW + 20 * DAY),
    'active, renewal webhook late': (dict(status='active', current_period_end=NOW - DAY), True, NOW - DAY),
    'active, no period end yet': (dict(status='active'), True, None),
    'active, cancel at period end': (dict(status='active', cancel_at_period_end=True, current_period_end=NOW + 9 * DAY),
                                     True, NOW + 9 * DAY),
    'trialing': (dict(status='trialing', current_period_end=NOW + 4 * DAY), True, NOW + 4 * DAY),
    'trialing, canceled': (dict(status='trialing', cancel_at_period_end=True, current_period_end=NOW + 4 * DAY),
                           True, NOW + 4 * DAY),
    'past due, in grace': (dict(status='past_due', current_period_start=NOW - 2 * DAY, current_period_end=NOW + 28 * DAY),
                           True, NOW - 2 * DAY + PAST_DUE_GRACE),
    'past due, grace over': (dict(status='past_due', current_period_start=NOW - 8 * DAY, current_period_end=NOW + 22 * DAY),
                             False, None),
    'canceled by customer, paid time left': (dict(status='canceled', cancellation_reason='cancellation_requested',
                                                  current_period_end=NOW + 3 * DAY), True, NOW + 3 * DAY),
    'canceled by customer, over': (dict(status='canceled', cancellation_reason='cancellation_requested',
                                        current_period_end=NOW - DAY), False, None),
    'canceled for non-payment': (dict(status='canceled', cancellation_reason='payment_failed',
                                      current_period_end=NOW + 20 * DAY), False, None),
    'unpaid': (dict(status='unpaid', current_period_end=NOW + 20 * DAY), False, None),
    'incomplete': (dict(status='incomplete', current_period_end=NOW + 20 * DAY), False, None),
    'incomplete_expired': (dict(status='incomplete_expired', current_period_end=NOW + 20 * DAY), False, None),
    'paused': (dict(status='paused', current_period_end=NOW + 20 * DAY), False, None),
}


@pytest.mark.parametrize('name', list(ACCESS_STATES))
def test_access_until_for_each_state(name):
    fields, access, until = ACCESS_STATES[name]
    user = user_with(**fields) if fields is not None else user_with()
    assert has_paid_access(user, NOW) is access
    assert access_until(user, NOW) == until


def test_access_until_handles_naive_sqlite_datetimes():
    user = user_with(status='trialing', current_period_end=(NOW + DAY).replace(tzinfo=None))
    assert access_until(user, NOW) == NOW + DAY


# --- /v1/me: paid_access, access_until, trial_available for every state ---

def _shift(fields, now):
    """ACCESS_STATES are relative to NOW; /v1/me uses the real clock."""
    return {key: value + (now - NOW) if isinstance(value, datetime) else value for key, value in fields.items()}


@pytest.mark.parametrize('name', list(ACCESS_STATES))
def test_me_for_each_state(name):
    fields, access, until = ACCESS_STATES[name]
    app = make_app(ACCOUNTS_ENABLED=True)
    user_id = sessions.add_user(app)
    real_now = datetime.now(timezone.utc).replace(microsecond=0)
    if fields is not None:
        add_subscription_row(app, user_id, customer_id='cus_1', **_shift(fields, real_now))
    body = sessions.me(app.test_client(), sessions.sign_in(app)).get_json()
    assert body['paid_access'] is access
    expected = (until + (real_now - NOW)).strftime('%Y-%m-%dT%H:%M:%SZ') if until else None
    assert body['access_until'] == expected
    assert body['trial_available'] is True
    assert 'plaid_access' not in body


def test_me_trialing_reports_trial_end_and_no_second_trial():
    app = make_app(ACCOUNTS_ENABLED=True)
    user_id = sessions.add_user(app)
    trial_end = datetime(2030, 1, 8, 9, 30, tzinfo=timezone.utc)
    add_subscription_row(app, user_id, customer_id='cus_1', stripe_subscription_id='sub_1', status='trialing',
                         current_period_end=trial_end, trial_used_at=trial_end - 7 * DAY)
    body = sessions.me(app.test_client(), sessions.sign_in(app)).get_json()
    assert body['paid_access'] is True
    assert body['access_until'] == '2030-01-08T09:30:00Z'
    assert body['trial_available'] is False
    assert body['subscription']['status'] == 'trialing'


def test_me_trial_unavailable_when_trials_are_off():
    app = make_app(ACCOUNTS_ENABLED=True, TRIAL_DAYS=0)
    sessions.add_user(app)
    assert sessions.me(app.test_client(), sessions.sign_in(app)).get_json()['trial_available'] is False


# --- trial_available ---

def test_trial_available(stripe_app):
    with stripe_app.app_context():
        assert billing.trial_available(user_with()) is True
        assert billing.trial_available(user_with(status=None)) is True
        assert billing.trial_available(user_with(status='canceled')) is True  # paid before, never trialed
        assert billing.trial_available(user_with(status='canceled', trial_used_at=NOW)) is False
        stripe_app.config['TRIAL_DAYS'] = 0
        assert billing.trial_available(user_with()) is False


# --- Checkout ---

def test_first_checkout_includes_the_trial(stripe_app, fake_stripe):
    add_user(stripe_app)
    resp = logged_in_client(stripe_app).post('/billing/checkout')
    assert resp.status_code == 303
    assert fake_stripe.trial_days == [7]


def test_no_second_trial_after_one_was_used(stripe_app, fake_stripe):
    user_id = add_user(stripe_app)
    add_subscription_row(stripe_app, user_id, customer_id='cus_1', stripe_subscription_id='sub_old',
                         status='canceled', trial_used_at=NOW)
    client = logged_in_client(stripe_app)
    page = client.get('/account').data
    assert b'Subscribe for $8.99/month' in page
    assert b'free week' not in page
    assert client.post('/billing/checkout').status_code == 303
    assert fake_stripe.trial_days == [0]


def test_trial_days_zero_turns_trials_off(fake_stripe):
    app = make_app(ACCOUNTS_ENABLED=True, TRIAL_DAYS=0, **STRIPE_CONFIG)
    add_user(app)
    client = logged_in_client(app)
    page = client.get('/account').data
    assert b'Subscribe for $8.99/month' in page
    assert b'free trial' not in page and b'free week' not in page
    client.post('/billing/checkout')
    assert fake_stripe.trial_days == [0]


def test_other_trial_lengths(fake_stripe):
    app = make_app(ACCOUNTS_ENABLED=True, TRIAL_DAYS=14, **STRIPE_CONFIG)
    add_user(app)
    client = logged_in_client(app)
    assert b'Start your 14-day free trial' in client.get('/account').data
    client.post('/billing/checkout')
    assert fake_stripe.trial_days == [14]


def test_checkout_session_params_with_trial(stripe_app, monkeypatch):
    seen = {}

    class Sessions:
        def create(self, params):
            seen.update(params)
            return types.SimpleNamespace(url='https://checkout.example')

    client = types.SimpleNamespace(v1=types.SimpleNamespace(checkout=types.SimpleNamespace(sessions=Sessions())))
    monkeypatch.setattr(billing, '_stripe', lambda: client)
    with stripe_app.test_request_context():
        billing.create_checkout_session('cus_1', User(id=7, email='payer@example.com'), 7)
    assert seen['subscription_data'] == {'metadata': {'user_id': '7'}, 'trial_period_days': 7}
    assert 'payment_method_collection' not in seen  # Stripe's default, `always`: card up front


def test_checkout_success_message_while_trialing(stripe_app, fake_stripe):
    user_id = add_user(stripe_app)
    add_subscription_row(stripe_app, user_id, customer_id='cus_1', stripe_subscription_id='sub_1', status='trialing',
                         current_period_end=datetime.now(timezone.utc) + 7 * DAY, trial_used_at=NOW)
    page = logged_in_client(stripe_app).get('/account?checkout=success').data
    assert b'Your free trial has started.' in page


# --- trial_used_at ---

def test_trialing_subscription_records_trial_used(stripe_app, fake_stripe):
    user_id = add_user(stripe_app)
    add_subscription_row(stripe_app, user_id, customer_id='cus_1')
    sub = fake_stripe.set_subscription('sub_1', 'cus_1', status='trialing', trial_start=TRIAL_START, trial_end=PERIOD_END)
    send(stripe_app.test_client(), subscription_event('customer.subscription.created', sub))
    row = stored(stripe_app, user_id)
    assert row.status == 'trialing'
    assert row.trial_used_at == datetime(2026, 10, 25)  # SQLite returns naive UTC

    # Converting to paid, canceling, and resubscribing never clear it.
    for status in ('active', 'canceled'):
        sub = fake_stripe.set_subscription('sub_1', 'cus_1', status=status, trial_start=TRIAL_START, trial_end=PERIOD_END)
        send(stripe_app.test_client(), subscription_event('customer.subscription.updated', sub))
    sub = fake_stripe.set_subscription('sub_2', 'cus_1')
    send(stripe_app.test_client(), subscription_event('customer.subscription.created', sub))
    row = stored(stripe_app, user_id)
    assert (row.stripe_subscription_id, row.status) == ('sub_2', 'active')
    assert row.trial_used_at == datetime(2026, 10, 25)


def test_subscription_without_trial_leaves_trial_available(stripe_app, fake_stripe):
    user_id = add_user(stripe_app)
    add_subscription_row(stripe_app, user_id, customer_id='cus_1')
    sub = fake_stripe.set_subscription('sub_1', 'cus_1')
    send(stripe_app.test_client(), subscription_event('customer.subscription.created', sub))
    assert stored(stripe_app, user_id).trial_used_at is None


def test_trial_on_an_ignored_subscription_still_counts(stripe_app, fake_stripe):
    user_id = add_user(stripe_app)
    add_subscription_row(stripe_app, user_id, customer_id='cus_1', stripe_subscription_id='sub_live', status='active')
    other = fake_stripe.set_subscription('sub_other', 'cus_1', status='trialing', trial_start=TRIAL_START)
    send(stripe_app.test_client(), subscription_event('customer.subscription.created', other))
    row = stored(stripe_app, user_id)
    assert row.stripe_subscription_id == 'sub_live'
    assert row.trial_used_at is not None


# --- customer.subscription.trial_will_end reminder ---

@pytest.fixture
def trialing_user(stripe_app, fake_stripe):
    user_id = add_user(stripe_app)
    add_subscription_row(stripe_app, user_id, customer_id='cus_1', stripe_subscription_id='sub_1', status='trialing')
    fake_stripe.set_subscription('sub_1', 'cus_1', status='trialing', trial_start=TRIAL_START, trial_end=PERIOD_END)
    return user_id


def outbox(app):
    return app.extensions.setdefault('mail_outbox', [])


def test_trial_will_end_sends_one_reminder(stripe_app, fake_stripe, trialing_user):
    stripe_app.config['PUBLIC_BASE_URL'] = 'https://workbenchbudgeting.com'
    evt = event('customer.subscription.trial_will_end', fake_stripe.subscriptions['sub_1'])
    client = stripe_app.test_client()

    assert send(client, evt).status_code == 200
    assert len(outbox(stripe_app)) == 1
    mail = outbox(stripe_app)[0]
    assert mail['to'] == 'payer@example.com'
    assert 'free trial' in mail['subject']
    assert 'ends on November 1, 2026' in mail['text']
    assert '$8.99/month' in mail['text']
    assert 'https://workbenchbudgeting.com/account' in mail['text']

    resp = send(client, evt)  # Stripe delivering the same event again
    assert resp.get_json()['duplicate'] is True
    assert len(outbox(stripe_app)) == 1


def test_no_reminder_when_the_trial_was_canceled(stripe_app, fake_stripe, trialing_user):
    sub = fake_stripe.set_subscription('sub_1', 'cus_1', status='trialing', cancel_at_period_end=True,
                                       trial_start=TRIAL_START, trial_end=PERIOD_END)
    send(stripe_app.test_client(), event('customer.subscription.trial_will_end', sub))
    assert outbox(stripe_app) == []
    assert stored(stripe_app, trialing_user).cancel_at_period_end is True


def test_no_reminder_when_no_longer_trialing(stripe_app, fake_stripe, trialing_user):
    stale = dict(fake_stripe.subscriptions['sub_1'])
    fake_stripe.set_subscription('sub_1', 'cus_1', status='canceled', trial_start=TRIAL_START, trial_end=PERIOD_END)
    send(stripe_app.test_client(), event('customer.subscription.trial_will_end', stale))
    assert outbox(stripe_app) == []


def test_no_reminder_for_an_unknown_customer(stripe_app, fake_stripe):
    sub = fake_stripe.set_subscription('sub_9', 'cus_unknown', status='trialing', trial_start=TRIAL_START)
    assert send(stripe_app.test_client(), event('customer.subscription.trial_will_end', sub)).status_code == 200
    assert outbox(stripe_app) == []


def test_failed_reminder_makes_stripe_retry(stripe_app, fake_stripe, trialing_user, monkeypatch):
    def down(to, subject, text):
        raise OSError('email provider down')

    monkeypatch.setattr(billing, 'send_email', down)
    stripe_app.config['PROPAGATE_EXCEPTIONS'] = False
    evt = event('customer.subscription.trial_will_end', fake_stripe.subscriptions['sub_1'])
    assert send(stripe_app.test_client(), evt).status_code == 500

    monkeypatch.undo()
    for name in ('create_customer', 'create_checkout_session', 'fetch_subscription'):
        monkeypatch.setattr(billing, name, getattr(fake_stripe, name))
    assert send(stripe_app.test_client(), evt).status_code == 200
    assert len(outbox(stripe_app)) == 1


# --- TRIAL_DAYS setting ---

@pytest.mark.parametrize('raw, parsed', [(None, 7), ('', 7), ('7', 7), ('0', 0), ('14', 14), (' 3 ', 3),
                                         ('-1', '-1'), ('seven', 'seven')])
def test_trial_days_env(monkeypatch, raw, parsed):
    if raw is None:
        monkeypatch.delenv('TRIAL_DAYS', raising=False)
    else:
        monkeypatch.setenv('TRIAL_DAYS', raw)
    assert config.trial_days() == parsed


@pytest.mark.parametrize('value', ['seven', '-1', -1, 731, True])
def test_bad_trial_days_stops_startup_once_billing_is_on(value):
    with pytest.raises(RuntimeError, match='TRIAL_DAYS'):
        make_app(ACCOUNTS_ENABLED=True, TRIAL_DAYS=value, **STRIPE_CONFIG)


def test_bad_trial_days_ignored_while_accounts_are_off():
    make_app(TRIAL_DAYS='seven', **STRIPE_CONFIG)
    make_app(ACCOUNTS_ENABLED=True, TRIAL_DAYS='seven')  # Stripe not configured
