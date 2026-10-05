"""Live check of the free trial in Stripe test mode, using test clocks.

Needs a local server and the Stripe CLI forwarding webhooks to it, both using the same
database as this script (instance/app.db unless DATABASE_URL is set):

    ACCOUNTS_ENABLED=true python serve.py
    stripe listen --forward-to 127.0.0.1:5001/stripe/webhook --events checkout.session.completed,customer.subscription.created,customer.subscription.updated,customer.subscription.deleted,customer.subscription.trial_will_end,invoice.paid,invoice.payment_failed

STRIPE_SECRET_KEY / STRIPE_PRICE_ID come from .env, and STRIPE_WEBHOOK_SECRET must be the
whsec_... that `stripe listen` printed (the server checks it).

Checkout can't run on a test clock, so the script creates what Checkout would: a customer
(on a test clock, with Stripe's test Visa card) and a subscription with
trial_period_days = TRIAL_DAYS and metadata.user_id. Everything after that is real Stripe
webhooks handled by the server:

1. Trial that converts: subscribe -> `trialing` with access (and /v1/me) -> advance 4 days
   -> `customer.subscription.trial_will_end` handled (the reminder email is printed in
   the server's console) -> advance past day 7 -> `active` and the first $8.99 invoice paid.
2. Trial canceled during the trial (as the Customer Portal does, at period end): access
   until the trial's end, then `canceled` and no access.

It also creates (but doesn't complete) a real Checkout Session with the trial, to show
Stripe accepts it; open the printed URL to see the trial on the Checkout page.

Access is checked at the test clock's time, since the clock runs ahead of the real time
the server uses. Test clocks (and their customers) are deleted at the end, and so are
the local test users unless --keep.

Run from the repo root:
    python scripts/stripe_trial_check.py [--keep]
"""
import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import stripe

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import billing  # noqa: E402
from app_auth import create_app_session  # noqa: E402
from extensions import db  # noqa: E402
from models import StripeEvent, Subscription, User  # noqa: E402
from serve import create_app  # noqa: E402

DAY = 86400
failures = []


def check(label, ok, detail=''):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{f' ({detail})' if detail else ''}")
    if not ok:
        failures.append(label)


def wait_for(label, predicate, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        db.session.expire_all()
        result = predicate()
        if result:
            return result
        time.sleep(2)
    check(label, False, f'nothing after {timeout} s; are the server and `stripe listen` running?')
    return None


def advance(client, clock, to):
    client.v1.test_helpers.test_clocks.advance(clock.id, params={'frozen_time': to})
    for _ in range(120):
        clock = client.v1.test_helpers.test_clocks.retrieve(clock.id)
        if clock.status == 'ready':
            return clock
        time.sleep(2)
    raise SystemExit(f'Test clock {clock.id} did not finish advancing')


def at(timestamp):
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def start_trial(app, client, label):
    """Creates a local user, a test-clock customer, and a trialing subscription."""
    email = f'trial-check-{label}-{int(time.time())}@example.com'
    user = User(email=email, email_verified_at=datetime.now(timezone.utc))
    db.session.add(user)
    db.session.commit()

    start = int(time.time())
    clock = client.v1.test_helpers.test_clocks.create(params={'frozen_time': start, 'name': f'13a {label}'})
    customer = client.v1.customers.create(params={
        'email': email, 'test_clock': clock.id, 'metadata': {'user_id': str(user.id)},
        'payment_method': 'pm_card_visa', 'invoice_settings': {'default_payment_method': 'pm_card_visa'},
    })
    db.session.add(Subscription(user=user, stripe_customer_id=customer.id))
    db.session.commit()
    sub = client.v1.subscriptions.create(params={
        'customer': customer.id,
        'items': [{'price': app.config['STRIPE_PRICE_ID']}],
        'trial_period_days': app.config['TRIAL_DAYS'],
        'metadata': {'user_id': str(user.id)},
    })
    print(f'\n{label}: user {user.id} {email}, customer {customer.id}, subscription {sub.id}, clock {clock.id}')
    return user, clock, sub, start


def me(app, user):
    token, _ = create_app_session(user, 'stripe_trial_check')
    return app.test_client().get('/v1/me', headers={'Authorization': f'Bearer {token}'}).get_json()


def converting_trial(app, client):
    user, clock, sub, start = start_trial(app, client, 'converts')
    trial_end = at(sub.trial_end)
    row = wait_for('trialing row', lambda: db.session.get(User, user.id).subscription.status == 'trialing'
                   and db.session.get(User, user.id).subscription)
    if row:
        check('status trialing', row.status == 'trialing')
        check('trial_used_at recorded', row.trial_used_at is not None, str(row.trial_used_at))
        check('access while trialing', billing.has_paid_access(user, at(start)))
        body = me(app, db.session.get(User, user.id))
        check('/v1/me paid_access', body['paid_access'] is True)
        check('/v1/me access_until is the trial end', body['access_until'] == trial_end.strftime('%Y-%m-%dT%H:%M:%SZ'),
              body['access_until'])
        check('/v1/me trial_available false', body['trial_available'] is False)

    advance(client, clock, start + 4 * DAY + 3600)
    evt = wait_for('trial_will_end handled', lambda: db.session.scalar(
        db.select(StripeEvent).filter_by(type='customer.subscription.trial_will_end')
        .where(StripeEvent.processed_at >= at(start))))
    if evt:
        check('trial_will_end handled', True, f'{evt.id}; the reminder to {user.email} is in the server console')

    clock = advance(client, clock, start + 7 * DAY + 2 * 3600)
    row = wait_for('active after the trial', lambda: (s := db.session.get(User, user.id).subscription).status == 'active'
                   and s)
    if row:
        check('status active after the trial', row.status == 'active')
        check('access after the trial', billing.has_paid_access(user, at(clock.frozen_time)))
        invoice = client.v1.subscriptions.retrieve(sub.id, params={'expand': ['latest_invoice']}).latest_invoice
        check('first $8.99 invoice paid', invoice.status == 'paid' and invoice.amount_paid == 899,
              f'{invoice.id} {invoice.status} {invoice.amount_paid}')
        check('trial_available stays false', not billing.trial_available(db.session.get(User, user.id)))
    return user, clock


def canceled_trial(app, client):
    user, clock, sub, start = start_trial(app, client, 'cancels')
    trial_end = at(sub.trial_end)
    wait_for('trialing row', lambda: db.session.get(User, user.id).subscription.status == 'trialing')
    client.v1.subscriptions.update(sub.id, params={'cancel_at_period_end': True})
    row = wait_for('cancel recorded', lambda: (s := db.session.get(User, user.id).subscription).cancel_at_period_end and s)
    if row:
        check('still access after canceling', billing.has_paid_access(user, at(start + DAY)))
        check('access_until is the trial end', billing.access_until(user, at(start + DAY)) == trial_end,
              str(billing.access_until(user, at(start + DAY))))

    clock = advance(client, clock, start + 7 * DAY + 2 * 3600)
    row = wait_for('canceled at the trial end', lambda: (s := db.session.get(User, user.id).subscription).status == 'canceled'
                   and s)
    if row:
        check('status canceled at the trial end', row.status == 'canceled', row.cancellation_reason)
        check('no access after the trial end', not billing.has_paid_access(user, at(clock.frozen_time)))
        check('no second trial', not billing.trial_available(db.session.get(User, user.id)))
        invoices = client.v1.invoices.list(params={'subscription': sub.id}).data
        check('never charged', all(inv.amount_paid == 0 for inv in invoices), f'{len(invoices)} invoice(s)')
    return user, clock


def checkout_session(app):
    user = User(email=f'trial-check-checkout-{int(time.time())}@example.com')
    db.session.add(user)
    db.session.commit()
    with app.test_request_context():
        customer_id = billing.create_customer(user)
        url = billing.create_checkout_session(customer_id, user, app.config['TRIAL_DAYS'])
    print(f'\nCheckout Session with a {app.config["TRIAL_DAYS"]}-day trial accepted by Stripe:\n  {url}')
    return user, customer_id


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--keep', action='store_true', help='keep the local test users')
    args = parser.parse_args()

    app = create_app({'ACCOUNTS_ENABLED': True})
    if not app.config['STRIPE_ENABLED'] or not app.config['STRIPE_SECRET_KEY'].startswith('sk_test_'):
        sys.exit('Needs test-mode Stripe keys (STRIPE_SECRET_KEY=sk_test_..., STRIPE_PRICE_ID, STRIPE_WEBHOOK_SECRET).')
    if not app.config['TRIAL_DAYS']:
        sys.exit('TRIAL_DAYS is 0, so there is no trial to check.')
    client = stripe.StripeClient(app.config['STRIPE_SECRET_KEY'])
    price = client.v1.prices.retrieve(app.config['STRIPE_PRICE_ID'], params={'expand': ['product']})
    print(f'Price {price.id}: {price.unit_amount / 100:.2f} {price.currency.upper()}/{price.recurring.interval}, '
          f'product "{price.product.name}"')

    users, clocks, customers = [], [], []
    with app.app_context():
        try:
            for scenario in (converting_trial, canceled_trial):
                user, clock = scenario(app, client)
                users.append(user.id)
                clocks.append(clock.id)
            user, customer_id = checkout_session(app)
            users.append(user.id)
            customers.append(customer_id)
        finally:
            for clock_id in clocks:
                client.v1.test_helpers.test_clocks.delete(clock_id)
            for customer_id in customers:
                client.v1.customers.delete(customer_id)
            if not args.keep:
                for user_id in users:
                    user = db.session.get(User, user_id)
                    if user:
                        db.session.delete(user)
                db.session.commit()

    print(f"\n{'All checks passed.' if not failures else f'{len(failures)} check(s) failed: {failures}'}")
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
