import json
import logging
from datetime import datetime, timezone

import stripe
from flask import Blueprint, abort, current_app, flash, jsonify, redirect, request, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from sqlalchemy.exc import IntegrityError

from auth import external_url
from extensions import db, limiter
from models import StripeEvent, Subscription, User

logger = logging.getLogger(__name__)

# Stripe statuses where the customer has a subscription that is (or may become) billable.
# A new Checkout is refused in these states so nobody pays twice.
LIVE_STATUSES = frozenset({'active', 'trialing', 'past_due', 'unpaid', 'incomplete', 'paused'})

WEBHOOK_TOLERANCE_SECONDS = 300

# Only registered when ACCOUNTS_ENABLED is on and Stripe is configured (see create_app).
bp = Blueprint('billing', __name__)


class CheckoutForm(FlaskForm):
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


# --- Checkout ---

@bp.route('/billing/checkout', methods=['POST'])
@login_required
@limiter.limit('10 per hour')
def checkout():
    if not CheckoutForm().validate_on_submit():
        abort(400)
    user = current_user._get_current_object()
    if is_subscribed(user.subscription):
        flash('You already have a subscription.')
        return redirect(url_for('account.account'))

    if user.subscription is None:
        db.session.add(Subscription(user=user, stripe_customer_id=create_customer(user)))
        try:
            db.session.commit()
        except IntegrityError:
            # A simultaneous request created the row first; use that customer instead.
            db.session.rollback()
    return redirect(create_checkout_session(user.subscription.stripe_customer_id, user), code=303)


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
    row.current_period_end = _period_end(sub)
    row.cancel_at_period_end = sub['status'] != 'canceled' and bool(sub.get('cancel_at_period_end') or sub.get('cancel_at'))


def _period_end(sub):
    # API versions before 2025-03-31 put it on the subscription; later ones on each item.
    timestamp = sub.get('current_period_end')
    if not timestamp:
        items = (sub.get('items') or {}).get('data') or []
        timestamp = max((item.get('current_period_end') or 0 for item in items), default=0)
    return datetime.fromtimestamp(timestamp, tz=timezone.utc) if timestamp else None
