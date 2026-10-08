import re

import pytest

from extensions import db
from models import User, normalize_email
from serve import create_app

PASSWORD = 'correct horse battery'
CODE_SUBJECT = 'Your Workbench Budgeting sign-in code'


def signed_in_email(client):
    """Email shown on /account, or None if the client isn't logged in."""
    resp = client.get('/account')
    match = re.search(rb'<dt>Email</dt>\s*<dd>([^<]+)</dd>', resp.data)
    return match.group(1).decode() if match else None


def code_emails(app, email=None):
    return [m for m in app.extensions.get('mail_outbox', [])
            if m['subject'] == CODE_SUBJECT and (email is None or m['to'] == normalize_email(email))]


def last_code(app, email=None):
    """The code in the newest sign-in code email (to `email`, if given)."""
    return re.search(r'^ +([0-9]{6})$', code_emails(app, email)[-1]['text'], re.M).group(1)


def enter_code(client, email=None):
    return client.post('/login/code', data={'code': last_code(client.application, email)})


def password_login(client, email='user@example.com', password=PASSWORD, next_url=None):
    """Logs in through /login and the emailed code. Returns the last response: the
    redirect after the code, or /login's own response if the password was wrong."""
    resp = client.post('/login', query_string={'next': next_url} if next_url else None,
                       data={'email': email, 'password': password})
    if resp.status_code != 302 or resp.headers['Location'] != '/login/code':
        return resp
    return enter_code(client, email)


def signup_with_code(client, email='new@example.com', password=PASSWORD, next_url=None):
    """Signs up and enters the emailed code; returns the last response."""
    resp = client.post('/signup', query_string={'next': next_url} if next_url else None,
                       data={'email': email, 'password': password, 'confirm': password})
    if resp.status_code != 302 or resp.headers['Location'] != '/login/code':
        return resp
    return enter_code(client, email)


def log_in_as(app, client, user_id):
    """Puts a user's session straight into the client, skipping the form and the code."""
    with app.app_context():
        session_id = db.session.get(User, user_id).get_id()
    with client.session_transaction() as sess:
        sess['_user_id'] = session_id
        sess['_fresh'] = True


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
        'TRIAL_DAYS': 7,
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
