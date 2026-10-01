import json
import re
import time

import pytest
from itsdangerous.timed import TimestampSigner

import mailer
import tokens
from extensions import db
from models import User, utcnow
from serve import create_app
from tests.conftest import make_app, signed_in_email

PASSWORD = 'correct horse battery'
NEW_PASSWORD = 'a brand new passphrase'


def link_path(message):
    """Pull the site-relative path out of the link in an email body."""
    url = re.search(r'https?://\S+', message['text']).group(0)
    return '/' + url.split('/', 3)[3]


def login(client, email='user@example.com', password=PASSWORD):
    return client.post('/login', data={'email': email, 'password': password})


def get_user(app, email='user@example.com'):
    with app.app_context():
        user = db.session.scalar(db.select(User).filter_by(email=email))
        db.session.expunge(user)
        return user


def make_token_at(app, maker, user_id, seconds_ago):
    with app.app_context():
        user = db.session.get(User, user_id)
        original = TimestampSigner.get_timestamp
        TimestampSigner.get_timestamp = lambda self: int(time.time()) - seconds_ago
        try:
            return maker(user)
        finally:
            TimestampSigner.get_timestamp = original


# --- Flag off ---------------------------------------------------------------

@pytest.mark.parametrize('method,path', [
    ('get', '/forgot-password'), ('post', '/forgot-password'),
    ('get', '/reset-password/abc'), ('post', '/reset-password/abc'),
    ('get', '/verify-email/abc'), ('post', '/verify-email/resend'),
])
def test_routes_404_when_flag_off(client, method, path):
    assert getattr(client, method)(path).status_code == 404


# --- send_email ---------------------------------------------------------------

def test_console_backend_prints_email(capsys):
    app = make_app(EMAIL_BACKEND='console')
    with app.app_context():
        mailer.send_email('a@example.com', 'Hello', 'Body text')
    out = capsys.readouterr().out
    assert 'EMAIL to a@example.com' in out and 'Subject: Hello' in out and 'Body text' in out


def test_resend_backend_posts_to_api(monkeypatch):
    sent = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"id": "abc"}'

    def fake_urlopen(request, timeout):
        sent['url'] = request.full_url
        sent['headers'] = dict(request.header_items())
        sent['body'] = json.loads(request.data)
        return FakeResponse()

    monkeypatch.setattr(mailer.urllib.request, 'urlopen', fake_urlopen)
    app = make_app(EMAIL_BACKEND='resend', RESEND_API_KEY='re_test', EMAIL_FROM='WB <noreply@example.com>')
    with app.app_context():
        mailer.send_email('a@example.com', 'Hello', 'Body text')

    assert sent['url'] == 'https://api.resend.com/emails'
    assert sent['headers']['Authorization'] == 'Bearer re_test'
    assert sent['body'] == {
        'from': 'WB <noreply@example.com>', 'to': ['a@example.com'],
        'subject': 'Hello', 'text': 'Body text',
    }


def test_email_failure_does_not_break_signup(monkeypatch):
    app = make_app(ACCOUNTS_ENABLED=True, EMAIL_BACKEND='resend', RESEND_API_KEY='re_test')

    def boom(*args, **kwargs):
        raise OSError('network down')

    monkeypatch.setattr(mailer.urllib.request, 'urlopen', boom)
    resp = app.test_client().post('/signup', data={
        'email': 'a@example.com', 'password': PASSWORD, 'confirm': PASSWORD,
    })
    assert resp.status_code == 302


def test_email_backend_defaults(monkeypatch):
    monkeypatch.delenv('EMAIL_BACKEND', raising=False)
    monkeypatch.delenv('RESEND_API_KEY', raising=False)
    assert create_app().config['EMAIL_BACKEND'] == 'console'
    monkeypatch.setenv('RESEND_API_KEY', 're_x')
    assert create_app().config['EMAIL_BACKEND'] == 'resend'


# --- Email verification -------------------------------------------------------

def test_signup_sends_verification_email(accounts_app, accounts_client, outbox):
    resp = accounts_client.post('/signup', data={
        'email': 'new@example.com', 'password': PASSWORD, 'confirm': PASSWORD,
    }, follow_redirects=True)
    assert b'We sent a link to new@example.com' in resp.data
    assert b'Resend verification link' in resp.data
    assert len(outbox) == 1
    assert outbox[0]['to'] == 'new@example.com'
    assert '/verify-email/' in outbox[0]['text']


def test_verify_link_marks_email_verified(accounts_app, accounts_client, outbox):
    accounts_client.post('/signup', data={'email': 'new@example.com', 'password': PASSWORD, 'confirm': PASSWORD})
    path = link_path(outbox[0])

    resp = accounts_client.get(path)
    assert resp.status_code == 200
    assert b'Email verified' in resp.data
    assert get_user(accounts_app, 'new@example.com').email_verified_at is not None


def test_verify_link_reuse_is_harmless(accounts_app, accounts_client, outbox):
    accounts_client.post('/signup', data={'email': 'new@example.com', 'password': PASSWORD, 'confirm': PASSWORD})
    path = link_path(outbox[0])
    accounts_client.get(path)
    first = get_user(accounts_app, 'new@example.com').email_verified_at

    resp = accounts_client.get(path)
    assert resp.status_code == 200
    assert get_user(accounts_app, 'new@example.com').email_verified_at == first


def test_verify_link_works_when_logged_out(accounts_app, make_user):
    user_id = make_user()
    with accounts_app.app_context():
        token = tokens.make_verify_token(db.session.get(User, user_id))
    resp = accounts_app.test_client().get(f'/verify-email/{token}')
    assert resp.status_code == 200


def test_expired_verify_link_rejected(accounts_app, accounts_client, make_user):
    user_id = make_user()
    token = make_token_at(accounts_app, tokens.make_verify_token, user_id, tokens.VERIFY_MAX_AGE + 60)
    resp = accounts_client.get(f'/verify-email/{token}')
    assert resp.status_code == 400
    assert b'expired' in resp.data
    assert get_user(accounts_app).email_verified_at is None


def test_verify_link_almost_expired_still_works(accounts_app, accounts_client, make_user):
    user_id = make_user()
    token = make_token_at(accounts_app, tokens.make_verify_token, user_id, tokens.VERIFY_MAX_AGE - 60)
    assert accounts_client.get(f'/verify-email/{token}').status_code == 200


def test_tampered_verify_link_rejected(accounts_app, accounts_client, make_user):
    user_id = make_user()
    with accounts_app.app_context():
        token = tokens.make_verify_token(db.session.get(User, user_id))
    assert accounts_client.get(f'/verify-email/{token[:-2]}xx').status_code == 400


def test_verify_link_stops_working_if_email_changes(accounts_app, accounts_client, make_user):
    user_id = make_user()
    with accounts_app.app_context():
        user = db.session.get(User, user_id)
        token = tokens.make_verify_token(user)
        user.email = 'changed@example.com'
        db.session.commit()
    assert accounts_client.get(f'/verify-email/{token}').status_code == 400


def test_reset_token_is_not_a_verify_token(accounts_app, accounts_client, make_user):
    user_id = make_user()
    with accounts_app.app_context():
        token = tokens.make_reset_token(db.session.get(User, user_id))
    assert accounts_client.get(f'/verify-email/{token}').status_code == 400


def test_resend_verification(accounts_client, make_user, outbox):
    make_user()
    login(accounts_client)
    resp = accounts_client.post('/verify-email/resend', follow_redirects=True)
    assert b'We sent a new verification link' in resp.data
    assert len(outbox) == 1


def test_resend_verification_requires_login(accounts_client, outbox):
    resp = accounts_client.post('/verify-email/resend')
    assert resp.status_code == 302
    assert '/login' in resp.headers['Location']
    assert outbox == []


def test_resend_skipped_when_already_verified(accounts_app, accounts_client, make_user, outbox):
    user_id = make_user()
    with accounts_app.app_context():
        db.session.get(User, user_id).email_verified_at = utcnow()
        db.session.commit()
    login(accounts_client)
    resp = accounts_client.post('/verify-email/resend', follow_redirects=True)
    assert b'already verified' in resp.data
    assert outbox == []


# --- Password reset -----------------------------------------------------------

def test_forgot_password_sends_email_for_known_account(accounts_client, make_user, outbox):
    make_user()
    resp = accounts_client.post('/forgot-password', data={'email': ' USER@example.com'})
    assert resp.status_code == 200
    assert b'If an account exists' in resp.data
    assert len(outbox) == 1
    assert outbox[0]['to'] == 'user@example.com'
    assert '/reset-password/' in outbox[0]['text']


def test_forgot_password_same_response_for_unknown_email(accounts_client, outbox):
    resp = accounts_client.post('/forgot-password', data={'email': 'nobody@example.com'})
    assert resp.status_code == 200
    assert b'If an account exists' in resp.data
    assert outbox == []


def test_reset_password_full_flow(accounts_app, accounts_client, make_user, outbox):
    make_user()
    accounts_client.post('/forgot-password', data={'email': 'user@example.com'})
    path = link_path(outbox[0])

    page = accounts_client.get(path)
    assert page.status_code == 200
    assert b'Choose a new password' in page.data

    resp = accounts_client.post(path, data={'password': NEW_PASSWORD, 'confirm': NEW_PASSWORD}, follow_redirects=True)
    assert b'Your password has been updated.' in resp.data
    assert signed_in_email(accounts_client) == 'user@example.com'
    assert get_user(accounts_app).email_verified_at is not None

    fresh = accounts_app.test_client()
    assert b'Invalid email or password.' in login(fresh, password=PASSWORD).data
    assert login(fresh, password=NEW_PASSWORD).status_code == 302


def test_reset_link_is_single_use(accounts_client, make_user, outbox):
    make_user()
    accounts_client.post('/forgot-password', data={'email': 'user@example.com'})
    path = link_path(outbox[0])
    accounts_client.post(path, data={'password': NEW_PASSWORD, 'confirm': NEW_PASSWORD})

    assert accounts_client.get(path).status_code == 400
    resp = accounts_client.post(path, data={'password': 'yet another password', 'confirm': 'yet another password'})
    assert resp.status_code == 400


def test_expired_reset_link_rejected(accounts_app, accounts_client, make_user):
    user_id = make_user()
    token = make_token_at(accounts_app, tokens.make_reset_token, user_id, tokens.RESET_MAX_AGE + 60)
    assert accounts_client.get(f'/reset-password/{token}').status_code == 400
    resp = accounts_client.post(f'/reset-password/{token}', data={'password': NEW_PASSWORD, 'confirm': NEW_PASSWORD})
    assert resp.status_code == 400
    assert login(accounts_app.test_client()).status_code == 302  # old password still works


def test_tampered_reset_link_rejected(accounts_app, accounts_client, make_user):
    user_id = make_user()
    with accounts_app.app_context():
        token = tokens.make_reset_token(db.session.get(User, user_id))
    assert accounts_client.get(f'/reset-password/{token[:-2]}xx').status_code == 400


def test_reset_validation_errors_keep_token_usable(accounts_client, make_user, outbox):
    make_user()
    accounts_client.post('/forgot-password', data={'email': 'user@example.com'})
    path = link_path(outbox[0])
    resp = accounts_client.post(path, data={'password': 'short', 'confirm': 'short'})
    assert resp.status_code == 200
    assert b'Password must be 8 to 128 characters.' in resp.data
    assert accounts_client.get(path).status_code == 200


def test_reset_lets_passwordless_user_set_password(accounts_app, accounts_client, make_user):
    user_id = make_user(email='google-only@example.com', password=None)
    with accounts_app.app_context():
        token = tokens.make_reset_token(db.session.get(User, user_id))
    accounts_client.post(f'/reset-password/{token}', data={'password': NEW_PASSWORD, 'confirm': NEW_PASSWORD})
    assert login(accounts_app.test_client(), email='google-only@example.com', password=NEW_PASSWORD).status_code == 302


def test_reset_logs_out_other_sessions(accounts_app, make_user, outbox):
    make_user()
    other_device = accounts_app.test_client()
    login(other_device)
    assert signed_in_email(other_device) == 'user@example.com'

    resetter = accounts_app.test_client()
    resetter.post('/forgot-password', data={'email': 'user@example.com'})
    resetter.post(link_path(outbox[0]), data={'password': NEW_PASSWORD, 'confirm': NEW_PASSWORD})

    assert signed_in_email(resetter) == 'user@example.com'
    assert signed_in_email(other_device) is None


def test_reset_links_use_public_base_url_not_host_header(make_user):
    app = make_app(ACCOUNTS_ENABLED=True, PUBLIC_BASE_URL='https://workbenchbudgeting.com')
    with app.app_context():
        user = User(email='user@example.com')
        user.set_password(PASSWORD)
        db.session.add(user)
        db.session.commit()
    app.test_client().post(
        '/forgot-password', data={'email': 'user@example.com'}, headers={'Host': 'evil.example.com'},
    )
    text = app.extensions['mail_outbox'][0]['text']
    assert 'https://workbenchbudgeting.com/reset-password/' in text
    assert 'evil.example.com' not in text


def test_forgot_password_is_rate_limited():
    client = make_app(ACCOUNTS_ENABLED=True, RATELIMIT_ENABLED=True).test_client()
    statuses = [client.post('/forgot-password', data={'email': 'x@example.com'}).status_code for _ in range(6)]
    assert statuses[:5] == [200] * 5
    assert statuses[5] == 429


def test_forgot_password_requires_csrf(make_user):
    app = make_app(ACCOUNTS_ENABLED=True, WTF_CSRF_ENABLED=True)
    resp = app.test_client().post('/forgot-password', data={'email': 'user@example.com'})
    assert b'If an account exists' not in resp.data
    assert app.extensions.get('mail_outbox', []) == []
