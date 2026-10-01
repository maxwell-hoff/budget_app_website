import hashlib
import hmac
from datetime import datetime, timezone

from flask import current_app
from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from extensions import db


def utcnow():
    return datetime.now(timezone.utc)


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
