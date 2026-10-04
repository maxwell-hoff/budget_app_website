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
from models import StripeEvent, Subscription, User, utcnow

logger = logging.getLogger(__name__)

# Stripe statuses where the customer has a subscription that is (or may become) billable.
# A new Checkout is refused in these states so nobody pays twice.
LIVE_STATUSES = frozenset({'active', 'trialing', 'past_due', 'unpaid', 'incomplete', 'paused'})

WEBHOOK_TOLERANCE_SECONDS = 300

# How long bank syncing keeps working after a renewal payment fails, while Stripe retries.
PAST_DUE_GRACE = timedelta(days=7)

# Only registered when ACCOUNTS_ENABLED is on and Stripe is configured (see create_app).
bp = Blueprint('billing', __name__)


class BillingForm(FlaskForm):
    pass


def init_billing(app):
    app.config['STRIPE_ENABLED'] = all(
        app.config.get(name) for name in ('STRIPE_SECRET_KEY', 'STRIPE_PRICE_ID', 'STRIPE_WEBHOOK_SECRET')
    )


def is_subscribed(subscription):
    return subscription is not None and subscription.status in LIVE_STATUSES


# --- Stripe API calls (replaced in tests) ---

def _stripe():
    return stripe.StripeClient(current_app.config['STRIPE_SECRET_KEY'])


def create_customer(user):
    customer = _stripe().v1.customers.create(params={'email': user.email, 'metadata': {'user_id': str(user.id)}})
    return customer.id


def create_checkout_session(customer_id, user):
    session = _stripe().v1.checkout.sessions.create(params={
        'mode': 'subscription',
        'customer': customer_id,
        'client_reference_id': str(user.id),
        'line_items': [{'price': current_app.config['STRIPE_PRICE_ID'], 'quantity': 1}],
        'subscription_data': {'metadata': {'user_id': str(user.id)}},
        'success_url': external_url('account.account', checkout='success'),
        'cancel_url': external_url('account.account', checkout='canceled'),
    })
    return session.url


def fetch_subscription(subscription_id):
    return _stripe().v1.subscriptions.retrieve(subscription_id).to_dict()


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


def has_plaid_access(user, now=None):
    """The only place that decides whether a user may use Plaid bank syncing."""
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


def subscription_summary(user, now=None):
    """What the account page shows: a state name, the date that goes with it, and buttons."""
    sub = user.subscription
    access = has_plaid_access(user, now)
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
        url = create_checkout_session(user.subscription.stripe_customer_id, user)
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


_HANDLERS = {
    'checkout.session.completed': _on_checkout_completed,
    'customer.subscription.created': _on_subscription_event,
    'customer.subscription.updated': _on_subscription_event,
    'customer.subscription.deleted': _on_subscription_event,
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
    the event body) keeps the row right even when events arrive out of order."""
    sub = fetch_subscription(subscription_id)
    if row.stripe_subscription_id not in (None, sub['id']) and is_subscribed(row):
        logger.warning(
            'Ignoring subscription %s for customer %s; %s is still %s',
            sub['id'], row.stripe_customer_id, row.stripe_subscription_id, row.status,
        )
        return
    row.stripe_subscription_id = sub['id']
    row.status = sub['status']
    row.current_period_start = _period_bound(sub, 'current_period_start', min)
    row.current_period_end = _period_bound(sub, 'current_period_end', max)
    row.cancel_at_period_end = sub['status'] != 'canceled' and bool(sub.get('cancel_at_period_end') or sub.get('cancel_at'))
    row.cancellation_reason = (sub.get('cancellation_details') or {}).get('reason') if sub['status'] == 'canceled' else None


def _period_bound(sub, field, pick):
    # API versions before 2025-03-31 put it on the subscription; later ones on each item.
    timestamp = sub.get(field)
    if not timestamp:
        items = (sub.get('items') or {}).get('data') or []
        timestamp = pick((item.get(field) for item in items if item.get(field)), default=None)
    return datetime.fromtimestamp(timestamp, tz=timezone.utc) if timestamp else None
