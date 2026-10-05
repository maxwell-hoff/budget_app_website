import json
import logging
from datetime import datetime, timedelta, timezone

import stripe
from flask import Blueprint, abort, current_app, flash, jsonify, redirect, request, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from sqlalchemy.exc import IntegrityError

from auth import external_url
from extensions import db, limiter
from mailer import send_email
from models import StripeEvent, Subscription, User, utcnow

logger = logging.getLogger(__name__)

# Stripe statuses where the customer has a subscription that is (or may become) billable.
# A new Checkout is refused in these states so nobody pays twice.
LIVE_STATUSES = frozenset({'active', 'trialing', 'past_due', 'unpaid', 'incomplete', 'paused'})
# Stripe statuses a subscription never leaves; resubscribing creates a new subscription.
ENDED_STATUSES = frozenset({'canceled', 'incomplete_expired'})

WEBHOOK_TOLERANCE_SECONDS = 300

# How long the app keeps working after a renewal payment fails, while Stripe retries.
PAST_DUE_GRACE = timedelta(days=7)

# Stripe's limit for subscription_data.trial_period_days.
MAX_TRIAL_DAYS = 730

# Only registered when ACCOUNTS_ENABLED is on and Stripe is configured (see create_app).
bp = Blueprint('billing', __name__)


class BillingForm(FlaskForm):
    pass


def init_billing(app):
    app.config['STRIPE_ENABLED'] = all(
        app.config.get(name) for name in ('STRIPE_SECRET_KEY', 'STRIPE_PRICE_ID', 'STRIPE_WEBHOOK_SECRET')
    )
    trial = app.config.get('TRIAL_DAYS')
    if app.config['STRIPE_ENABLED'] and app.config['ACCOUNTS_ENABLED']:
        if not isinstance(trial, int) or isinstance(trial, bool) or not 0 <= trial <= MAX_TRIAL_DAYS:
            raise RuntimeError(f'TRIAL_DAYS must be a whole number from 0 to {MAX_TRIAL_DAYS}.')


def is_subscribed(subscription):
    return subscription is not None and subscription.status in LIVE_STATUSES


# --- Stripe API calls (replaced in tests) ---

def _stripe():
    return stripe.StripeClient(current_app.config['STRIPE_SECRET_KEY'])


def create_customer(user):
    customer = _stripe().v1.customers.create(params={'email': user.email, 'metadata': {'user_id': str(user.id)}})
    return customer.id


def create_checkout_session(customer_id, user, trial_days=0):
    subscription_data = {'metadata': {'user_id': str(user.id)}}
    if trial_days:
        # Checkout collects a card up front even with a trial (payment_method_collection
        # defaults to `always`), so Stripe charges when the trial ends unless canceled.
        subscription_data['trial_period_days'] = trial_days
    session = _stripe().v1.checkout.sessions.create(params={
        'mode': 'subscription',
        'customer': customer_id,
        'client_reference_id': str(user.id),
        'line_items': [{'price': current_app.config['STRIPE_PRICE_ID'], 'quantity': 1}],
        'subscription_data': subscription_data,
        'success_url': external_url('account.account', checkout='success'),
        'cancel_url': external_url('account.account', checkout='canceled'),
    })
    return session.url


def fetch_subscription(subscription_id):
    return _stripe().v1.subscriptions.retrieve(subscription_id).to_dict()


def delete_customer(customer_id):
    """Deleting the customer also cancels their subscriptions immediately."""
    try:
        _stripe().v1.customers.delete(customer_id)
    except stripe.InvalidRequestError as exc:
        if exc.code != 'resource_missing':
            raise


def create_portal_session(customer_id):
    session = _stripe().v1.billing_portal.sessions.create(params={
        'customer': customer_id,
        'return_url': external_url('account.account'),
    })
    return session.url


# --- Access ---

def _aware(value):
    # SQLite hands back naive datetimes; everything is stored in UTC.
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def past_due_grace_end(subscription):
    start = _aware(subscription.current_period_start)
    return start + PAST_DUE_GRACE if start else None


def has_paid_access(user, now=None):
    """The only place that decides whether a user has paid access: the whole desktop app
    (beyond the free Sample profile), bank syncing included."""
    sub = user.subscription if user is not None else None
    if sub is None or not sub.status:
        return False
    now = now or utcnow()
    if sub.status in ('active', 'trialing'):
        return True
    if sub.status == 'past_due':
        # Stripe starts the new period when the renewal invoice is created, so the period
        # start is when the failed payment was first attempted.
        grace_end = past_due_grace_end(sub)
        return grace_end is not None and now < grace_end
    if sub.status == 'canceled':
        # Canceled at the customer's request keeps the time they paid for; canceled for
        # non-payment doesn't (Stripe has already moved the period end forward by then).
        end = _aware(sub.current_period_end)
        return sub.cancellation_reason == 'cancellation_requested' and end is not None and now < end
    return False


def access_until(user, now=None):
    """Until when access is already guaranteed with no further payment, or None without
    access. Lets the desktop keep working offline. For `active`/`trialing` it's the period
    end (the trial's end while trialing), which can be in the past for a moment while a
    renewal webhook is late; None if Stripe hasn't reported a period end."""
    if not has_paid_access(user, now):
        return None
    sub = user.subscription
    if sub.status == 'past_due':
        return past_due_grace_end(sub)
    return _aware(sub.current_period_end)


def trial_days():
    return current_app.config.get('TRIAL_DAYS') or 0


def trial_available(user):
    """Trials are on and this user has never had one (one per account)."""
    sub = user.subscription
    return trial_days() > 0 and (sub is None or sub.trial_used_at is None)


def subscription_ended(user, now=None):
    """No access and no way back to it without a new subscription. Unlike `unpaid` or a
    lapsed `past_due`, which a payment can still fix."""
    sub = user.subscription
    if has_paid_access(user, now):
        return False
    return sub is None or sub.status is None or sub.status in ENDED_STATUSES


def subscription_summary(user, now=None):
    """What the account page shows: a state name, the date that goes with it, and buttons."""
    sub = user.subscription
    access = has_paid_access(user, now)
    status = sub.status if sub else None
    if status in ('active', 'trialing'):
        state = 'ending' if sub.cancel_at_period_end else 'active'
        until = _aware(sub.current_period_end)
    elif status == 'past_due':
        state, until = 'past_due', past_due_grace_end(sub)
    elif status == 'canceled' and access:
        state, until = 'canceled', _aware(sub.current_period_end)
    elif status in LIVE_STATUSES:
        state, until = 'paused', None
    else:
        state, until = 'none', None
    return {
        'state': state,
        'status': status,
        'access': access,
        'until': until,
        'can_subscribe': not is_subscribed(sub),
        'can_manage': bool(sub and sub.stripe_subscription_id),
        'trial_days': trial_days() if trial_available(user) else 0,
    }


# --- Checkout and Customer Portal ---

PAYMENT_PROVIDER_ERROR = "We couldn't reach our payment provider. Please try again in a few minutes."


@bp.route('/billing/checkout', methods=['POST'])
@login_required
@limiter.limit('10 per hour')
def checkout():
    if not BillingForm().validate_on_submit():
        abort(400)
    user = current_user._get_current_object()
    if is_subscribed(user.subscription):
        flash('You already have a subscription.')
        return redirect(url_for('account.account'))

    try:
        if user.subscription is None:
            db.session.add(Subscription(user=user, stripe_customer_id=create_customer(user)))
            try:
                db.session.commit()
            except IntegrityError:
                # A simultaneous request created the row first; use that customer instead.
                db.session.rollback()
        trial = trial_days() if trial_available(user) else 0
        url = create_checkout_session(user.subscription.stripe_customer_id, user, trial)
    except stripe.StripeError:
        logger.exception('Stripe Checkout failed for user %s', user.id)
        flash(PAYMENT_PROVIDER_ERROR)
        return redirect(url_for('account.account'))
    return redirect(url, code=303)


@bp.route('/billing/portal', methods=['POST'])
@login_required
@limiter.limit('20 per hour')
def portal():
    if not BillingForm().validate_on_submit():
        abort(400)
    user = current_user._get_current_object()
    if user.subscription is None:
        flash("You don't have a subscription to manage yet.")
        return redirect(url_for('account.account'))
    try:
        url = create_portal_session(user.subscription.stripe_customer_id)
    except stripe.StripeError:
        logger.exception('Stripe Customer Portal failed for user %s', user.id)
        flash(PAYMENT_PROVIDER_ERROR)
        return redirect(url_for('account.account'))
    return redirect(url, code=303)


# --- Webhooks ---

@bp.route('/stripe/webhook', methods=['POST'])
def webhook():
    payload = request.get_data()
    try:
        stripe.WebhookSignature.verify_header(
            payload, request.headers.get('Stripe-Signature'), current_app.config['STRIPE_WEBHOOK_SECRET'],
            tolerance=WEBHOOK_TOLERANCE_SECONDS,
        )
        event = json.loads(payload)
    except (stripe.SignatureVerificationError, ValueError):
        abort(400)

    event_id, event_type = event.get('id'), event.get('type')
    if not event_id or not event_type:
        abort(400)
    if db.session.get(StripeEvent, event_id):
        return jsonify(received=True, duplicate=True)

    handler = _HANDLERS.get(event_type)
    if handler:
        handler(event['data']['object'])
    db.session.add(StripeEvent(id=event_id, type=event_type))
    try:
        db.session.commit()
    except IntegrityError:
        # The same event was processed concurrently by another worker.
        db.session.rollback()
    return jsonify(received=True)


def _on_checkout_completed(session):
    if session.get('mode') != 'subscription' or not session.get('subscription'):
        return
    row = _row_for_customer(_id(session.get('customer')), session.get('client_reference_id'))
    if row:
        _sync(row, _id(session['subscription']))


def _on_subscription_event(subscription):
    user_id = (subscription.get('metadata') or {}).get('user_id')
    row = _row_for_customer(_id(subscription.get('customer')), user_id)
    if row:
        _sync(row, subscription['id'])


def _on_invoice_event(invoice):
    subscription_id = _invoice_subscription_id(invoice)
    if not subscription_id:
        return
    row = _row_for_customer(_id(invoice.get('customer')))
    if row:
        _sync(row, subscription_id)


def _on_trial_will_end(subscription):
    """Stripe sends this 3 days before a trial ends. Remind the user they'll be charged,
    unless they've already canceled. A failed email raises, so Stripe resends the event."""
    user_id = (subscription.get('metadata') or {}).get('user_id')
    row = _row_for_customer(_id(subscription.get('customer')), user_id)
    if row is None:
        return
    sub = _sync(row, subscription['id'])
    if sub is None or row.stripe_subscription_id != sub['id']:
        return
    if sub['status'] != 'trialing' or row.cancel_at_period_end:
        return
    trial_end = _timestamp(sub.get('trial_end')) or row.current_period_end
    send_email(row.user.email, 'Your Workbench Budgeting free trial ends soon', _trial_reminder_text(trial_end))


def _trial_reminder_text(trial_end):
    end = f'{trial_end:%B} {trial_end.day}, {trial_end.year}' if trial_end else 'in a few days'
    return (
        f'Your free trial of Workbench Budgeting ends on {end}.\n\n'
        'After that, your subscription continues at $8.99/month, charged to the card you '
        'entered. You don\'t need to do anything to keep using the app.\n\n'
        'If you don\'t want to continue, cancel before the trial ends and you won\'t be '
        'charged. You can cancel from your account page:\n\n'
        f"{external_url('account.account')}\n"
    )


_HANDLERS = {
    'checkout.session.completed': _on_checkout_completed,
    'customer.subscription.created': _on_subscription_event,
    'customer.subscription.updated': _on_subscription_event,
    'customer.subscription.deleted': _on_subscription_event,
    'customer.subscription.trial_will_end': _on_trial_will_end,
    'invoice.paid': _on_invoice_event,
    'invoice.payment_failed': _on_invoice_event,
}


def _id(value):
    """Stripe fields hold either an ID or the expanded object."""
    if isinstance(value, dict):
        return value.get('id')
    return value


def _invoice_subscription_id(invoice):
    # API versions before 2025-03-31 put it at the top level; later ones under `parent`.
    details = (invoice.get('parent') or {}).get('subscription_details') or {}
    return _id(invoice.get('subscription') or details.get('subscription'))


def _row_for_customer(customer_id, user_id=None):
    row = db.session.scalar(db.select(Subscription).filter_by(stripe_customer_id=customer_id)) if customer_id else None
    if row or not customer_id:
        return row
    # A customer created outside /billing/checkout: link it through the user ID we put in
    # the Checkout Session or subscription metadata, unless that user already has one.
    user = db.session.get(User, int(user_id)) if str(user_id or '').isdigit() else None
    if user is None or user.subscription is not None:
        logger.warning('Stripe customer %s does not match any user', customer_id)
        return None
    row = Subscription(user=user, stripe_customer_id=customer_id)
    db.session.add(row)
    return row


def _sync(row, subscription_id):
    """Copy the subscription's current state from Stripe. Fetching it (instead of trusting
    the event body) keeps the row right even when events arrive out of order. Returns the
    fetched subscription."""
    sub = fetch_subscription(subscription_id)
    if row.trial_used_at is None and (sub.get('trial_start') or sub.get('trial_end')):
        # Recorded even for a subscription ignored below: any trial uses up the user's one.
        row.trial_used_at = _timestamp(sub.get('trial_start')) or utcnow()
    if row.stripe_subscription_id not in (None, sub['id']) and is_subscribed(row):
        logger.warning(
            'Ignoring subscription %s for customer %s; %s is still %s',
            sub['id'], row.stripe_customer_id, row.stripe_subscription_id, row.status,
        )
        return sub
    row.stripe_subscription_id = sub['id']
    row.status = sub['status']
    row.current_period_start = _period_bound(sub, 'current_period_start', min)
    row.current_period_end = _period_bound(sub, 'current_period_end', max)
    row.cancel_at_period_end = sub['status'] != 'canceled' and bool(sub.get('cancel_at_period_end') or sub.get('cancel_at'))
    row.cancellation_reason = (sub.get('cancellation_details') or {}).get('reason') if sub['status'] == 'canceled' else None
    _remove_plaid_items_if_ended(row.user)
    return sub


def _remove_plaid_items_if_ended(user):
    if not current_app.config.get('PLAID_ENABLED'):
        return
    import plaid_api  # plaid_api imports this module
    # Failures are logged and the items kept for `flask plaid-remove-lapsed`; they don't
    # fail the webhook, which would make Stripe resend an event that was handled.
    plaid_api.remove_items_if_subscription_ended(user)


def _period_bound(sub, field, pick):
    # API versions before 2025-03-31 put it on the subscription; later ones on each item.
    timestamp = sub.get(field)
    if not timestamp:
        items = (sub.get('items') or {}).get('data') or []
        timestamp = pick((item.get(field) for item in items if item.get(field)), default=None)
    return _timestamp(timestamp)


def _timestamp(value):
    return datetime.fromtimestamp(value, tz=timezone.utc) if value else None
