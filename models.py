import hashlib
import hmac
from datetime import datetime, timezone

from flask import current_app
from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from extensions import db


def utcnow():
    return datetime.now(timezone.utc)


def as_utc(value):
    # SQLite hands back naive datetimes; everything is stored in UTC.
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def normalize_email(email):
    return (email or '').strip().lower()


class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(320), unique=True, nullable=False)
    # Nullable so accounts created through Google sign-in (step 7) need no password.
    password_hash = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    email_verified_at = db.Column(db.DateTime(timezone=True), nullable=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    def password_fingerprint(self):
        """Short keyed digest of the password hash. It changes whenever the password
        changes, which makes reset tokens single-use and ends other sessions."""
        key = current_app.config['SECRET_KEY'].encode()
        return hmac.new(key, (self.password_hash or '').encode(), hashlib.sha256).hexdigest()[:16]

    def get_id(self):
        # Flask-Login stores this in the session cookie; load_user rejects it once the
        # password changes.
        return f'{self.id}:{self.password_fingerprint()}'

    def identity(self, provider):
        return next((i for i in self.identities if i.provider == provider), None)


class OAuthIdentity(db.Model):
    """A third-party sign-in (only Google so far) linked to a user."""

    __tablename__ = 'oauth_identities'
    __table_args__ = (
        db.UniqueConstraint('provider', 'provider_subject'),
        db.UniqueConstraint('user_id', 'provider'),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    provider = db.Column(db.String(32), nullable=False)
    # Google's stable `sub` claim; emails can change, this can't.
    provider_subject = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(320), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    user = db.relationship('User', backref=db.backref('identities', cascade='all, delete-orphan'))


class Subscription(db.Model):
    """A user's Stripe customer and their current (or most recent) subscription."""

    __tablename__ = 'subscriptions'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, unique=True)
    stripe_customer_id = db.Column(db.String(255), nullable=False, unique=True)
    stripe_subscription_id = db.Column(db.String(255), nullable=True, unique=True)
    # Stripe's subscription status (active, past_due, canceled, ...); null until the
    # customer's first subscription exists.
    status = db.Column(db.String(32), nullable=True)
    current_period_start = db.Column(db.DateTime(timezone=True), nullable=True)
    current_period_end = db.Column(db.DateTime(timezone=True), nullable=True)
    cancel_at_period_end = db.Column(db.Boolean, nullable=False, default=False)
    # Stripe's cancellation_details.reason: cancellation_requested, payment_failed, payment_disputed.
    cancellation_reason = db.Column(db.String(32), nullable=True)
    # When the user's one free trial started; set the first time Stripe reports a
    # subscription with a trial, and never cleared, so resubscribing gets no second trial.
    trial_used_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    user = db.relationship(
        'User', backref=db.backref('subscription', uselist=False, cascade='all, delete-orphan'),
    )


class AuthCode(db.Model):
    """One-time code from /app-login that the desktop app trades (with its PKCE verifier)
    for an app session."""

    __tablename__ = 'auth_codes'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    code_hash = db.Column(db.String(64), nullable=False, unique=True)
    code_challenge = db.Column(db.String(43), nullable=False)
    redirect_uri = db.Column(db.String(255), nullable=False)
    device_name = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    used_at = db.Column(db.DateTime(timezone=True), nullable=True)

    user = db.relationship('User', backref=db.backref('auth_codes', cascade='all, delete-orphan'))


class AppSession(db.Model):
    """A desktop app sign-in. The app holds the bearer token; only its hash is stored."""

    __tablename__ = 'app_sessions'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    device_name = db.Column(db.String(100), nullable=True)
    # User.password_fingerprint() when the session was created; the session stops working
    # when the password changes or is removed, like web sessions do.
    password_fingerprint = db.Column(db.String(16), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    last_used_at = db.Column(db.DateTime(timezone=True), nullable=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    revoked_at = db.Column(db.DateTime(timezone=True), nullable=True)

    user = db.relationship('User', backref=db.backref('app_sessions', cascade='all, delete-orphan'))


class PlaidItem(db.Model):
    """A bank connection (Plaid Item). The access token never leaves the server."""

    __tablename__ = 'plaid_items'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    item_id = db.Column(db.String(255), nullable=False, unique=True)
    # Fernet ciphertext; key from PLAID_TOKEN_KEY (see plaid_api.encrypt_token).
    access_token_encrypted = db.Column(db.Text, nullable=False)
    institution_id = db.Column(db.String(64), nullable=True)
    institution_name = db.Column(db.String(255), nullable=True)
    # ok, relink_required, or error.
    status = db.Column(db.String(32), nullable=False, default='ok')
    transactions_cursor = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    last_synced_at = db.Column(db.DateTime(timezone=True), nullable=True)

    user = db.relationship('User', backref=db.backref('plaid_items', cascade='all, delete-orphan'))


class StripeEvent(db.Model):
    """Stripe webhook events already processed; Stripe can deliver an event more than once."""

    __tablename__ = 'stripe_events'

    id = db.Column(db.String(255), primary_key=True)
    type = db.Column(db.String(255), nullable=False)
    processed_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
