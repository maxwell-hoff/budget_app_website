import re

import pytest

from extensions import db
from models import User
from serve import create_app


def signed_in_email(client):
    """Email shown on /account, or None if the client isn't logged in."""
    resp = client.get('/account')
    match = re.search(rb'<dt>Email</dt>\s*<dd>([^<]+)</dd>', resp.data)
    return match.group(1).decode() if match else None


def make_app(**overrides):
    config = {
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite://',
        'ACCOUNTS_ENABLED': False,
        'WTF_CSRF_ENABLED': False,
        'RATELIMIT_ENABLED': False,
        'EMAIL_BACKEND': 'memory',
        'PUBLIC_BASE_URL': None,
        'GOOGLE_CLIENT_ID': '',
        'GOOGLE_CLIENT_SECRET': '',
        'STRIPE_SECRET_KEY': '',
        'STRIPE_PRICE_ID': '',
        'STRIPE_WEBHOOK_SECRET': '',
        'PLAID_CLIENT_ID': '',
        'PLAID_SECRET': '',
        'PLAID_ENVIRONMENT': 'sandbox',
        'PLAID_TOKEN_KEY': '',
    }
    config.update(overrides)
    app = create_app(config)
    with app.app_context():
        db.create_all()
    return app


@pytest.fixture
def app():
    return make_app()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def accounts_app():
    return make_app(ACCOUNTS_ENABLED=True)


@pytest.fixture
def accounts_client(accounts_app):
    return accounts_app.test_client()


@pytest.fixture
def outbox(accounts_app):
    return accounts_app.extensions.setdefault('mail_outbox', [])


@pytest.fixture
def make_user(accounts_app):
    def _make_user(email='user@example.com', password='correct horse battery'):
        with accounts_app.app_context():
            user = User(email=email)
            if password is not None:
                user.set_password(password)
            db.session.add(user)
            db.session.commit()
            return user.id
    return _make_user
