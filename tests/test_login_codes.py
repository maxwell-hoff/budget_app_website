import logging
from datetime import timedelta

import pytest

import login_codes
import mailer
from extensions import db
from models import LoginCode, User, utcnow
from tests.conftest import (
    PASSWORD, code_emails, enter_code, last_code, log_in_as, make_app, password_login, signed_in_email,
)
from tests.test_app_sessions import DesktopApp, add_user, approve, exchange, same_url
from tests.test_email_flows import link_path
from tests.test_google import claims, google, google_login  # noqa: F401  (google is a fixture)

NEW_PASSWORD = 'a brand new passphrase'


def start_login(client, email='user@example.com', password=PASSWORD, next_url=None):
    return client.post('/login', query_string={'next': next_url} if next_url else None,
                       data={'email': email, 'password': password})


def submit(client, code):
    return client.post('/login/code', data={'code': code})


def wrong_code(app):
    return '000000' if last_code(app) != '000000' else '111111'


def later(monkeypatch, delta):
    """Moves login_codes' clock forward."""
    now = utcnow() + delta
    monkeypatch.setattr(login_codes, 'utcnow', lambda: now)


@pytest.fixture
def events(caplog):
    caplog.set_level(logging.INFO, logger='security')

    def _events():
        return [dict(part.split('=', 1) for part in r.getMessage().split()[1:])
                for r in caplog.records if r.name == 'security']
    return _events


@pytest.fixture
def user_id(accounts_app):
    return add_user(accounts_app)


# --- Availability -------------------------------------------------------------

@pytest.mark.parametrize('method,path', [
    ('get', '/login/code'), ('post', '/login/code'), ('post', '/login/code/resend'),
])
def test_routes_404_when_flag_off(client, method, path):
    assert getattr(client, method)(path).status_code == 404


# --- Password login needs the code ----------------------------------------------

def test_password_alone_no_longer_logs_in(accounts_app, accounts_client, user_id):
    resp = start_login(accounts_client)
    assert resp.status_code == 302
    assert resp.headers['Location'] == '/login/code'
    assert signed_in_email(accounts_client) is None

    page = accounts_client.get('/login/code')
    assert page.status_code == 200
    assert b'Check your email' in page.data
    assert 'u•••@example.com'.encode() in page.data
    assert b'user@example.com' not in page.data
    assert b'autocomplete="one-time-code"' in page.data and b'inputmode="numeric"' in page.data


def test_right_code_logs_in_and_goes_to_next(accounts_app, accounts_client, user_id):
    start_login(accounts_client, next_url='/about')
    resp = enter_code(accounts_client)
    assert resp.status_code == 302
    assert resp.headers['Location'] == '/about'
    assert signed_in_email(accounts_client) == 'user@example.com'


def test_offsite_next_is_ignored(accounts_client, user_id):
    start_login(accounts_client, next_url='https://evil.example/')
    assert enter_code(accounts_client).headers['Location'] == '/account'


def test_code_with_spaces_is_accepted(accounts_app, accounts_client, user_id):
    start_login(accounts_client)
    code = last_code(accounts_app)
    assert submit(accounts_client, f' {code[:3]} {code[3:]} ').status_code == 302
    assert signed_in_email(accounts_client) == 'user@example.com'


def test_wrong_password_sends_no_code(accounts_app, accounts_client, user_id):
    resp = start_login(accounts_client, password='wrong password')
    assert b'Invalid email or password.' in resp.data
    assert code_emails(accounts_app) == []
    with accounts_app.app_context():
        assert db.session.query(LoginCode).count() == 0


def test_unknown_email_sends_no_code(accounts_app, accounts_client):
    assert b'Invalid email or password.' in start_login(accounts_client, email='nobody@example.com').data
    assert code_emails(accounts_app) == []


def test_email_says_what_to_do(accounts_app, accounts_client, user_id):
    start_login(accounts_client)
    [message] = code_emails(accounts_app)
    assert message['to'] == 'user@example.com'
    assert message['subject'] == 'Your Workbench Budgeting sign-in code'
    assert last_code(accounts_app) in message['text']
    assert '10 minutes' in message['text']
    assert "If you didn't try to sign in" in message['text']
    assert '/forgot-password' in message['text']


def test_wrong_code_shows_an_error(accounts_app, accounts_client, user_id):
    start_login(accounts_client)
    wrong = wrong_code(accounts_app)
    resp = submit(accounts_client, wrong)
    assert resp.status_code == 200
    assert b"That code isn&#39;t right." in resp.data
    assert f'value="{wrong}"'.encode() not in resp.data
    assert signed_in_email(accounts_client) is None
    # The right code still works after a wrong one.
    assert enter_code(accounts_client).status_code == 302


@pytest.mark.parametrize('code', ['', '12345', '1234567', 'abcdef'])
def test_malformed_code_is_not_counted(accounts_app, accounts_client, user_id, code):
    start_login(accounts_client)
    resp = submit(accounts_client, code)
    assert b'Enter the 6-digit code from the email.' in resp.data
    with accounts_app.app_context():
        assert db.session.scalar(db.select(LoginCode)).attempts == 0


def test_five_wrong_codes_lock_out(accounts_app, accounts_client, user_id, events):
    start_login(accounts_client, next_url='/about')
    code = last_code(accounts_app)
    for _ in range(4):
        assert submit(accounts_client, wrong_code(accounts_app)).status_code == 200
    resp = submit(accounts_client, wrong_code(accounts_app))
    assert resp.status_code == 302
    assert resp.headers['Location'] == '/login?next=/about'
    assert b'Too many wrong codes' in accounts_client.get(resp.headers['Location']).data

    # The code and the pending login are gone, so even the right code fails now.
    resp = submit(accounts_client, code)
    assert resp.headers['Location'].startswith('/login')
    assert signed_in_email(accounts_client) is None
    with accounts_app.app_context():
        assert db.session.query(LoginCode).count() == 0

    logged = [e for e in events() if e['event'].startswith('login_code_')]
    assert [e['event'] for e in logged] == ['login_code_sent'] + ['login_code_failed'] * 4 + ['login_code_locked_out']
    assert [e.get('attempts') for e in logged[1:5]] == ['1', '2', '3', '4']


def test_attempts_are_not_reset_by_a_resend(accounts_app, accounts_client, user_id, monkeypatch):
    start_login(accounts_client)
    for _ in range(4):
        submit(accounts_client, wrong_code(accounts_app))
    later(monkeypatch, timedelta(minutes=2))
    accounts_client.post('/login/code/resend')
    resp = submit(accounts_client, wrong_code(accounts_app))
    assert resp.status_code == 302 and resp.headers['Location'].startswith('/login')


def test_expired_code(accounts_app, accounts_client, user_id, monkeypatch):
    start_login(accounts_client)
    code = last_code(accounts_app)
    later(monkeypatch, timedelta(minutes=10, seconds=1))
    resp = submit(accounts_client, code)
    assert resp.status_code == 200
    assert b'This code has expired.' in resp.data
    assert signed_in_email(accounts_client) is None

    # A new code works (within the pending login's 30 minutes).
    accounts_client.post('/login/code/resend')
    assert enter_code(accounts_client).status_code == 302
    assert signed_in_email(accounts_client) == 'user@example.com'


def test_code_almost_expired_still_works(accounts_app, accounts_client, user_id, monkeypatch):
    start_login(accounts_client)
    later(monkeypatch, timedelta(minutes=9, seconds=50))
    assert enter_code(accounts_client).status_code == 302


def test_pending_login_ends_after_30_minutes(accounts_app, accounts_client, user_id, monkeypatch):
    start_login(accounts_client)
    later(monkeypatch, timedelta(minutes=31))
    resp = accounts_client.get('/login/code')
    assert resp.status_code == 302 and resp.headers['Location'] == '/login'
    with accounts_app.app_context():
        assert db.session.query(LoginCode).count() == 0


def test_code_is_single_use(accounts_app, accounts_client, user_id):
    start_login(accounts_client)
    code = last_code(accounts_app)
    assert submit(accounts_client, code).status_code == 302
    accounts_client.post('/logout')

    resp = submit(accounts_client, code)
    assert resp.status_code == 302 and resp.headers['Location'] == '/login'
    assert signed_in_email(accounts_client) is None


def test_code_only_works_in_the_browser_that_asked(accounts_app, user_id):
    asker = accounts_app.test_client()
    start_login(asker)
    other = accounts_app.test_client()
    resp = submit(other, last_code(accounts_app))
    assert resp.headers['Location'] == '/login'
    assert signed_in_email(other) is None


def test_code_page_without_a_pending_login(accounts_client):
    resp = accounts_client.get('/login/code')
    assert resp.status_code == 302 and resp.headers['Location'] == '/login'


def test_code_page_when_already_logged_in(accounts_client, user_id):
    password_login(accounts_client)
    assert accounts_client.get('/login/code').headers['Location'] == '/account'


def test_new_login_replaces_the_previous_pending_one(accounts_app, accounts_client, user_id):
    start_login(accounts_client)
    first = last_code(accounts_app)
    start_login(accounts_client)
    with accounts_app.app_context():
        assert db.session.query(LoginCode).count() == 1
    if first != last_code(accounts_app):
        assert submit(accounts_client, first).status_code == 200
    assert enter_code(accounts_client).status_code == 302


def test_logout_drops_a_pending_login(accounts_client, user_id):
    start_login(accounts_client)
    accounts_client.post('/logout')
    assert accounts_client.get('/login/code').headers['Location'] == '/login'


# --- Resend ---------------------------------------------------------------------

def test_resend_replaces_the_code(accounts_app, accounts_client, user_id, monkeypatch):
    start_login(accounts_client)
    old = last_code(accounts_app)
    later(monkeypatch, timedelta(minutes=1))
    resp = accounts_client.post('/login/code/resend', follow_redirects=True)
    assert 'We sent a new code to u•••@example.com'.encode() in resp.data
    assert len(code_emails(accounts_app)) == 2
    new = last_code(accounts_app)
    with accounts_app.app_context():
        row = db.session.scalar(db.select(LoginCode))
        assert row.code_hmac == login_codes.code_hmac(row.user_id, new)
    if old != new:
        assert b"isn&#39;t right" in submit(accounts_client, old).data
    assert submit(accounts_client, new).status_code == 302


def test_resend_at_most_once_a_minute(accounts_app, accounts_client, user_id, monkeypatch):
    start_login(accounts_client)
    resp = accounts_client.post('/login/code/resend', follow_redirects=True)
    assert b'Wait a minute' in resp.data
    assert len(code_emails(accounts_app)) == 1
    later(monkeypatch, timedelta(seconds=61))
    accounts_client.post('/login/code/resend')
    assert len(code_emails(accounts_app)) == 2


def test_resend_is_rate_limited(monkeypatch):
    app = make_app(ACCOUNTS_ENABLED=True, RATELIMIT_ENABLED=True)
    add_user(app)
    client = app.test_client()
    start_login(client)
    statuses = []
    for minute in range(1, 7):
        later(monkeypatch, timedelta(minutes=minute))
        statuses.append(client.post('/login/code/resend').status_code)
    assert statuses == [302] * 5 + [429]
    assert len(code_emails(app)) == 6


def test_code_post_is_rate_limited():
    app = make_app(ACCOUNTS_ENABLED=True, RATELIMIT_ENABLED=True)
    statuses = [app.test_client().post('/login/code', data={'code': '123456'}).status_code for _ in range(11)]
    assert statuses[:10] == [302] * 10
    assert statuses[10] == 429


def test_resend_without_a_pending_login(accounts_client):
    resp = accounts_client.post('/login/code/resend')
    assert resp.headers['Location'] == '/login'


def test_code_forms_require_csrf():
    app = make_app(ACCOUNTS_ENABLED=True, WTF_CSRF_ENABLED=True)
    add_user(app)
    client = app.test_client()
    page = client.get('/login').data.decode()
    token = page.split('name="csrf_token" type="hidden" value="')[1].split('"')[0]
    client.post('/login', data={'email': 'user@example.com', 'password': PASSWORD, 'csrf_token': token})
    assert client.post('/login/code', data={'code': last_code(app)}).status_code == 200
    assert signed_in_email(client) is None
    assert client.post('/login/code/resend').status_code == 400
    assert client.post('/login/code', data={'code': last_code(app), 'csrf_token': token}).status_code == 302


# --- Email failures -----------------------------------------------------------------

def test_failed_email_says_so_and_logs_nobody_in(monkeypatch):
    app = make_app(ACCOUNTS_ENABLED=True)
    add_user(app)
    client = app.test_client()

    def boom(*args, **kwargs):
        raise OSError('provider down')

    real_send = mailer.send_email
    monkeypatch.setattr(mailer, 'send_email', boom)
    resp = start_login(client)
    assert resp.headers['Location'] == '/login/code'
    page = client.get('/login/code')
    assert b"We couldn&#39;t send the email with your code." in page.data
    assert signed_in_email(client) is None

    # No waiting needed after a failed send.
    monkeypatch.setattr(mailer, 'send_email', real_send)
    client.post('/login/code/resend')
    assert enter_code(client).status_code == 302


# --- Password changes void pending codes ------------------------------------------

def test_password_change_voids_a_pending_code(accounts_app, user_id):
    browser = accounts_app.test_client()
    start_login(browser)
    code = last_code(accounts_app)

    owner = accounts_app.test_client()
    password_login(owner)
    owner.post('/account', data={'current_password': PASSWORD, 'password': NEW_PASSWORD, 'confirm': NEW_PASSWORD})

    resp = submit(browser, code)
    assert resp.headers['Location'] == '/login'
    assert signed_in_email(browser) is None


def test_password_reset_voids_a_pending_code(accounts_app, user_id):
    browser = accounts_app.test_client()
    start_login(browser)
    code = last_code(accounts_app)
    with accounts_app.app_context():
        db.session.get(User, user_id).set_password(NEW_PASSWORD)
        db.session.commit()
    assert submit(browser, code).headers['Location'] == '/login'
    assert signed_in_email(browser) is None


def test_password_reset_still_logs_in_without_a_code(accounts_app, accounts_client, user_id, outbox):
    accounts_client.post('/forgot-password', data={'email': 'user@example.com'})
    resp = accounts_client.post(link_path(outbox[-1]), data={'password': NEW_PASSWORD, 'confirm': NEW_PASSWORD})
    assert resp.headers['Location'] == '/account'
    assert signed_in_email(accounts_client) == 'user@example.com'
    assert code_emails(accounts_app) == []


# --- Sign-up --------------------------------------------------------------------

def test_signup_needs_the_code_and_verifies_the_email(accounts_app, accounts_client):
    resp = accounts_client.post('/signup', query_string={'next': '/about'}, data={
        'email': 'New@Example.com', 'password': PASSWORD, 'confirm': PASSWORD,
    })
    assert resp.headers['Location'] == '/login/code'
    assert signed_in_email(accounts_client) is None
    page = accounts_client.get('/login/code').data
    assert b'Confirm your email' in page and b'finish creating your account' in page
    [message] = code_emails(accounts_app)
    assert message['to'] == 'new@example.com'
    assert "If you didn't create an account" in message['text']

    resp = enter_code(accounts_client)
    assert resp.headers['Location'] == '/about'
    assert signed_in_email(accounts_client) == 'new@example.com'
    with accounts_app.app_context():
        assert db.session.scalar(db.select(User)).email_verified_at is not None


def test_abandoned_signup_can_log_in_later_and_is_verified(accounts_app):
    accounts_app.test_client().post('/signup', data={
        'email': 'new@example.com', 'password': PASSWORD, 'confirm': PASSWORD,
    })
    client = accounts_app.test_client()
    password_login(client, email='new@example.com')
    assert signed_in_email(client) == 'new@example.com'
    with accounts_app.app_context():
        assert db.session.scalar(db.select(User)).email_verified_at is not None


def test_login_code_verifies_an_unverified_email(accounts_app, accounts_client, make_user):
    make_user()
    password_login(accounts_client)
    assert b'Verified' in accounts_client.get('/account').data


# --- Google and the desktop ----------------------------------------------------------

def test_google_sign_in_needs_no_code(google):  # noqa: F811
    client = google.app.test_client()
    resp = google_login(google, client, claims())
    assert resp.status_code == 302
    assert signed_in_email(client) == 'user@example.com'
    assert code_emails(google.app) == []
    with google.app.app_context():
        assert db.session.query(LoginCode).count() == 0


def test_desktop_sign_in_goes_through_the_code(accounts_app, user_id):
    desktop = DesktopApp()
    browser = accounts_app.test_client()
    resp = browser.get(desktop.login_path())
    assert resp.status_code == 302
    login_url = resp.headers['Location']
    assert login_url.startswith('/login?next=')

    resp = browser.post(login_url, data={'email': 'user@example.com', 'password': PASSWORD})
    assert resp.headers['Location'] == '/login/code'
    resp = enter_code(browser)
    assert same_url(resp.headers['Location'], desktop.login_path())

    code = approve(browser, desktop)
    assert exchange(accounts_app.test_client(), code, desktop.verifier).status_code == 200


# --- Storage and logs --------------------------------------------------------------

def test_codes_are_stored_only_as_hmacs(accounts_app, accounts_client, user_id):
    start_login(accounts_client)
    code = last_code(accounts_app)
    with accounts_app.app_context():
        row = db.session.scalar(db.select(LoginCode))
        values = [str(getattr(row, c.name)) for c in LoginCode.__table__.columns]
        assert not any(code in value for value in values)
        assert row.code_hmac == login_codes.code_hmac(user_id, code)
        assert len(row.code_hmac) == 64
    with make_app(SECRET_KEY='another key').app_context():
        assert login_codes.code_hmac(user_id, code) != row.code_hmac
    with accounts_client.session_transaction() as sess:
        assert code not in str(dict(sess))


def test_codes_are_never_logged(accounts_app, accounts_client, user_id, events, caplog):
    start_login(accounts_client)
    first = last_code(accounts_app)
    submit(accounts_client, wrong_code(accounts_app))
    enter_code(accounts_client)

    logged = [e for e in events() if e['event'].startswith('login')]
    assert logged == [
        {'event': 'login_code_sent', 'user': str(user_id), 'ip': '127.0.0.1', 'purpose': 'login'},
        {'event': 'login_code_failed', 'user': str(user_id), 'ip': '127.0.0.1', 'attempts': '1'},
        {'event': 'login_code_accepted', 'user': str(user_id), 'ip': '127.0.0.1', 'purpose': 'login'},
        {'event': 'login', 'user': str(user_id), 'ip': '127.0.0.1', 'method': 'password'},
    ]
    assert first not in caplog.text
    levels = {r.getMessage().split()[1]: r.levelno for r in caplog.records if r.name == 'security'}
    assert levels['event=login_code_failed'] == logging.WARNING
    assert levels['event=login_code_sent'] == logging.INFO


def test_deleting_the_user_deletes_their_codes(accounts_app, accounts_client, user_id):
    start_login(accounts_client)
    with accounts_app.app_context():
        db.session.delete(db.session.get(User, user_id))
        db.session.commit()
        assert db.session.query(LoginCode).count() == 0
    assert accounts_client.get('/login/code').headers['Location'] == '/login'


def test_old_rows_are_cleaned_up(accounts_app, user_id, monkeypatch):
    start_login(accounts_app.test_client())
    later(monkeypatch, timedelta(days=2))
    start_login(accounts_app.test_client())
    with accounts_app.app_context():
        assert db.session.query(LoginCode).count() == 1


def test_mask_email():
    assert login_codes.mask_email('max@example.com') == 'm•••@example.com'
    assert login_codes.mask_email('a@b.co') == 'a•••@b.co'


def test_unverified_session_from_before_codes_still_hits_the_app_login_gate(accounts_app, make_user):
    client = accounts_app.test_client()
    log_in_as(accounts_app, client, make_user())
    assert client.get(DesktopApp().login_path()).status_code == 403
