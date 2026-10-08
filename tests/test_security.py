import logging
import re

import pytest

from extensions import db
from models import AuthCode, User
from security_log import email_hash, log_event
from tests.conftest import last_code, log_in_as, make_app, signup_with_code
from tests.test_app_sessions import (
    PASSWORD, DesktopApp, add_user, approve, exchange, password_login, sign_in,
)
from tests.test_email_flows import link_path
from tests.test_google import claims, google, google_login  # noqa: F401  (google is a fixture)
from tests.test_pages import html_pages
from tests.test_plaid_api import ACCESS_TOKEN, fake_plaid, link, plaid_app, subscriber  # noqa: F401  (fixtures)


@pytest.fixture
def events(caplog):
    """The security events logged so far, as dicts (`event`, `user`, `ip`, ...)."""
    caplog.set_level(logging.INFO, logger='security')

    def _events():
        parsed = []
        for record in caplog.records:
            if record.name == 'security':
                message = record.getMessage()
                assert message.startswith('security ')
                parsed.append(dict(part.split('=', 1) for part in message.split()[1:]))
        return parsed
    _events.text = lambda: caplog.text
    return _events


def names(events):
    return [e['event'] for e in events()]


def assert_no_secrets(events, *secrets):
    text = events.text()
    for secret in secrets:
        assert secret and secret not in text, secret


# --- Headers ------------------------------------------------------------------

BASE_HEADERS = {
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'strict-origin-when-cross-origin',
    'X-Frame-Options': 'DENY',
}


def csp_directives(resp):
    policy = resp.headers['Content-Security-Policy']
    return dict(
        (part.split()[0], ' '.join(part.split()[1:])) for part in policy.split('; ')
    )


@pytest.mark.parametrize('path,status', [
    ('/', 200), ('/about', 200), ('/privacy', 200), ('/healthz', 200),
    ('/static/styles.css', 200), ('/no-such-page', 404),
])
def test_security_headers_on_public_responses(client, path, status):
    resp = client.get(path)
    assert resp.status_code == status
    for name, value in BASE_HEADERS.items():
        assert resp.headers[name] == value
    directives = csp_directives(resp)
    assert directives['frame-ancestors'] == "'none'"
    assert directives['object-src'] == "'none'"
    assert directives['default-src'] == "'self'"
    resp.close()


def test_security_headers_on_account_pages_and_api(accounts_client):
    for resp in (accounts_client.get('/login'), accounts_client.get('/v1/me')):
        for name, value in BASE_HEADERS.items():
            assert resp.headers[name] == value
        assert "frame-ancestors 'none'" in resp.headers['Content-Security-Policy']


def test_hsts_only_when_cookies_are_secure():
    plain = make_app(SESSION_COOKIE_SECURE=False).test_client().get('/')
    assert 'Strict-Transport-Security' not in plain.headers
    secure = make_app(SESSION_COOKIE_SECURE=True).test_client().get('/')
    assert secure.headers['Strict-Transport-Security'] == 'max-age=31536000'


def test_csp_allows_only_own_scripts_and_google_fonts(client):
    directives = csp_directives(client.get('/'))
    assert re.fullmatch(r"'self' 'nonce-[A-Za-z0-9_-]{22}'", directives['script-src'])
    assert 'unsafe-inline' not in directives['script-src']
    assert directives['style-src'] == "'self' 'unsafe-inline' https://fonts.googleapis.com"
    assert directives['font-src'] == "'self' https://fonts.gstatic.com"


def test_nonce_differs_per_response(client):
    first = csp_directives(client.get('/'))['script-src']
    second = csp_directives(client.get('/'))['script-src']
    assert first != second


def test_responses_without_scripts_get_no_nonce(client):
    assert csp_directives(client.get('/healthz'))['script-src'] == "'self'"


def assert_scripts_carry_nonce(client, pages):
    """Every inline script has the response's nonce (or the CSP would block it), and no
    page uses things the CSP blocks: external scripts or inline event handlers."""
    for path in pages:
        resp = client.get(path)
        html = resp.data.decode()
        nonce = re.search(r"'nonce-([^']+)'", resp.headers['Content-Security-Policy'])
        tags = re.findall(r'<script\b[^>]*>', html)
        for tag in tags:
            assert nonce and tag == f'<script nonce="{nonce.group(1)}">', (path, tag)
        assert not re.search(r'\son[a-z]+\s*=\s*"', html), path
        assert 'javascript:' not in html, path


def test_every_page_script_works_under_csp(client):
    pages = html_pages(client)
    assert {'/', '/about', '/privacy'} <= set(pages)
    assert_scripts_carry_nonce(client, pages)


def test_every_account_page_script_works_under_csp(accounts_client, accounts_app):
    signed_out = html_pages(accounts_client)
    assert '/login' in signed_out
    assert_scripts_carry_nonce(accounts_client, signed_out)

    add_user(accounts_app)
    password_login(accounts_client)
    signed_in = html_pages(accounts_client)
    assert '/account' in signed_in
    assert_scripts_carry_nonce(accounts_client, list(signed_in) + [DesktopApp().login_path()])


# --- /app-login needs a verified email --------------------------------------------

# Entering a sign-in code verifies the email, so an unverified user can only be logged in
# by a session from before sign-in codes; the tests put one in directly.
def add_unverified_user(app, email='user@example.com'):
    with app.app_context():
        user = User(email=email)
        user.set_password(PASSWORD)
        db.session.add(user)
        db.session.commit()
        return user.id


def auth_code_count(app):
    with app.app_context():
        return db.session.query(AuthCode).count()


def test_unverified_user_sees_verify_page_and_no_code(accounts_app, events):
    user_id = add_unverified_user(accounts_app)
    client = accounts_app.test_client()
    log_in_as(accounts_app, client, user_id)
    desktop = DesktopApp()

    page = client.get(desktop.login_path())
    assert page.status_code == 403
    assert b'Verify your email first' in page.data
    assert b'Resend verification email' in page.data
    assert b'Continue</button>' not in page.data

    resp = client.post(desktop.login_path())
    assert resp.status_code == 403
    assert 'Location' not in resp.headers
    assert auth_code_count(accounts_app) == 0
    assert {'event': 'app_code_refused', 'user': str(user_id), 'ip': '127.0.0.1',
            'reason': 'email_unverified'} in events()


def test_unverified_page_keeps_cancel_and_switch_account(accounts_app):
    client = accounts_app.test_client()
    log_in_as(accounts_app, client, add_unverified_user(accounts_app))
    desktop = DesktopApp()
    html = client.get(desktop.login_path()).data.decode()
    assert 'error=access_denied' in html
    assert 'Use a different account' in html


def test_resend_from_app_login_returns_there(accounts_app, outbox):
    client = accounts_app.test_client()
    log_in_as(accounts_app, client, add_unverified_user(accounts_app))
    desktop = DesktopApp()
    page = client.get(desktop.login_path()).data.decode()
    action = re.search(r'<form method="post" action="([^"]*verify-email/resend[^"]*)"', page).group(1)

    resp = client.post(action.replace('&amp;', '&'))
    assert resp.status_code == 302
    assert resp.headers['Location'].startswith('/app-login?')
    assert len(outbox) == 1 and '/verify-email/' in outbox[0]['text']

    followed = client.get(resp.headers['Location'])
    assert b'We sent a new verification link' in followed.data


def test_resend_ignores_offsite_next(accounts_app, outbox):
    client = accounts_app.test_client()
    log_in_as(accounts_app, client, add_unverified_user(accounts_app))
    resp = client.post('/verify-email/resend', query_string={'next': 'https://evil.example/'})
    assert resp.headers['Location'] == '/account'


def test_verifying_then_refreshing_app_login_issues_a_code(accounts_app, outbox):
    client = accounts_app.test_client()
    log_in_as(accounts_app, client, add_unverified_user(accounts_app))
    desktop = DesktopApp()
    assert client.get(desktop.login_path()).status_code == 403

    client.post('/verify-email/resend')
    client.get(link_path(outbox[-1]))
    code = approve(client, desktop)
    assert exchange(accounts_app.test_client(), code, desktop.verifier).status_code == 200


def test_verified_user_flow_unchanged(accounts_app):
    add_user(accounts_app)
    assert sign_in(accounts_app)


# --- Security logs ---------------------------------------------------------------

def test_email_hash_is_keyed_and_short(accounts_app):
    with accounts_app.app_context():
        digest = email_hash('Someone@Example.com ')
        assert digest == email_hash('someone@example.com')
        assert re.fullmatch(r'[0-9a-f]{12}', digest)
    with make_app(SECRET_KEY='another key').app_context():
        assert email_hash('someone@example.com') != digest


def test_signup_login_logout_events(accounts_app, events):
    client = accounts_app.test_client()
    signup_with_code(client)
    signup_code = last_code(accounts_app)
    client.post('/logout')
    client.post('/login', data={'email': 'new@example.com', 'password': 'wrong password!'})
    client.post('/login', data={'email': 'nobody@example.com', 'password': PASSWORD})
    password_login(client, email='new@example.com')

    logged = events()
    assert names(events) == [
        'signup', 'login_code_sent', 'login_code_accepted', 'email_verified', 'login', 'logout',
        'login_failed', 'login_failed', 'login_code_sent', 'login_code_accepted', 'login',
    ]
    assert logged[0] == {'event': 'signup', 'user': '1', 'ip': '127.0.0.1', 'method': 'password'}
    assert logged[1] == {'event': 'login_code_sent', 'user': '1', 'ip': '127.0.0.1', 'purpose': 'signup'}
    assert logged[2] == {'event': 'login_code_accepted', 'user': '1', 'ip': '127.0.0.1', 'purpose': 'signup'}
    assert logged[3] == {'event': 'email_verified', 'user': '1', 'ip': '127.0.0.1', 'method': 'login_code'}
    assert logged[6] == {'event': 'login_failed', 'user': '1', 'ip': '127.0.0.1', 'reason': 'bad_password'}
    with accounts_app.app_context():
        unknown_hash = email_hash('nobody@example.com')
    assert logged[7] == {'event': 'login_failed', 'ip': '127.0.0.1', 'reason': 'unknown_email',
                         'email_hash': unknown_hash}
    assert logged[8] == {'event': 'login_code_sent', 'user': '1', 'ip': '127.0.0.1', 'purpose': 'login'}
    assert logged[10] == {'event': 'login', 'user': '1', 'ip': '127.0.0.1', 'method': 'password'}
    assert_no_secrets(events, PASSWORD, 'wrong password!', 'new@example.com', 'nobody@example.com',
                      signup_code, last_code(accounts_app))


def test_failed_login_is_logged_at_warning(accounts_app, caplog):
    caplog.set_level(logging.INFO, logger='security')
    accounts_app.test_client().post('/login', data={'email': 'nobody@example.com', 'password': PASSWORD})
    assert [r.levelno for r in caplog.records if r.name == 'security'] == [logging.WARNING]


def test_reset_verify_and_password_change_events(accounts_app, outbox, events):
    user_id = add_unverified_user(accounts_app)
    client = accounts_app.test_client()
    client.post('/forgot-password', data={'email': 'user@example.com'})
    client.post('/forgot-password', data={'email': 'nobody@example.com'})
    reset_link = link_path(outbox[0])
    client.post(reset_link, data={'password': 'a new password 1', 'confirm': 'a new password 1'})
    client.post('/account', data={
        'current_password': 'a new password 1', 'password': 'a new password 2', 'confirm': 'a new password 2',
    })

    other_id = add_unverified_user(accounts_app, 'other@example.com')
    other = accounts_app.test_client()
    log_in_as(accounts_app, other, other_id)
    other.post('/verify-email/resend')
    verify_link = link_path(outbox[-1])
    other.get(verify_link)

    logged = events()
    assert [(e['event'], e.get('user'), e.get('method') or e.get('reason')) for e in logged] == [
        ('password_reset_requested', str(user_id), None),
        ('password_reset_requested', None, 'unknown_email'),
        ('password_reset', str(user_id), None),
        ('email_verified', str(user_id), 'password_reset'),
        ('password_changed', str(user_id), None),
        ('email_verified', str(other_id), 'link'),
    ]
    assert_no_secrets(
        events, PASSWORD, 'a new password 1', 'a new password 2', 'user@example.com', 'nobody@example.com',
        reset_link.rsplit('/', 1)[1], verify_link.rsplit('/', 1)[1],
    )


def test_desktop_sign_in_events(accounts_app, events):
    user_id = add_user(accounts_app)
    desktop = DesktopApp()
    client = accounts_app.test_client()
    password_login(client)
    code = approve(client, desktop)
    resp = exchange(accounts_app.test_client(), code, desktop.verifier)
    token = resp.get_json()['session_token']
    exchange(accounts_app.test_client(), code, desktop.verifier)  # reused code
    accounts_app.test_client().post('/v1/auth/logout', headers={'Authorization': f'Bearer {token}'})

    logged = [e for e in events() if e['event'].startswith('app_')]
    assert [(e['event'], e.get('user')) for e in logged] == [
        ('app_code_issued', str(user_id)),
        ('app_session_created', str(user_id)),
        ('app_token_rejected', None),
        ('app_session_revoked', str(user_id)),
    ]
    assert logged[1]['session'] == logged[3]['session']
    assert_no_secrets(events, code, desktop.verifier, token, PASSWORD, 'user@example.com')


def test_plaid_link_and_remove_events(plaid_app, fake_plaid, subscriber, events):  # noqa: F811
    user_id, headers = subscriber
    assert link(plaid_app, headers, public_token='public-sandbox-secret').status_code == 200
    assert plaid_app.test_client().delete('/v1/plaid/items/item-1', headers=headers).status_code == 204

    logged = [e for e in events() if e['event'].startswith('plaid_')]
    assert logged == [
        {'event': 'plaid_item_linked', 'user': str(user_id), 'ip': '127.0.0.1', 'item': 'item-1'},
        {'event': 'plaid_item_removed', 'user': str(user_id), 'ip': '127.0.0.1', 'item': 'item-1',
         'reason': 'user_request'},
    ]
    assert_no_secrets(events, ACCESS_TOKEN, 'public-sandbox-secret', headers['Authorization'].split()[1])


def test_account_deletion_events(plaid_app, fake_plaid, subscriber, events):  # noqa: F811
    user_id, headers = subscriber
    link(plaid_app, headers)
    with plaid_app.app_context():
        # Without Stripe configured, only a subscription that's over can be left behind.
        db.session.get(User, user_id).subscription.status = 'canceled'
        db.session.commit()
    browser = plaid_app.test_client()
    password_login(browser)
    resp = browser.post('/account/delete', data={'confirm_email': 'user@example.com', 'current_password': PASSWORD})
    assert resp.status_code == 302

    logged = [e for e in events() if e['event'] in ('plaid_item_removed', 'account_deleted')]
    assert logged == [
        {'event': 'plaid_item_removed', 'user': str(user_id), 'ip': '127.0.0.1', 'item': 'item-1',
         'reason': 'account_deleted'},
        {'event': 'account_deleted', 'user': str(user_id), 'ip': '127.0.0.1'},
    ]
    assert_no_secrets(events, ACCESS_TOKEN, PASSWORD, 'user@example.com')


def test_google_events(google, events):  # noqa: F811
    app = google.app
    google_login(google, app.test_client(), claims(sub='sub-new', email='new@example.com'))

    with app.app_context():
        user = User(email='user@example.com')  # an unverified sign-up the real owner takes over
        user.set_password(PASSWORD)
        db.session.add(user)
        db.session.commit()
        squatted_id = user.id
    google_login(google, app.test_client(), claims(sub='sub-owner'))

    linker_id = add_user(app, email='linker@example.com')
    browser = app.test_client()
    password_login(browser, email='linker@example.com')
    google_login(google, browser, claims(sub='sub-linker', email='linker@gmail.com'))
    browser.post('/account/google/unlink')
    google_login(google, app.test_client(), claims(sub='sub-x', email='x@example.com', verified=False))

    assert [(e['event'], e.get('user'), e.get('method') or e.get('reason')) for e in events()] == [
        ('signup', '1', 'google'),
        ('google_linked', '1', None),
        ('login', '1', 'google'),
        ('email_verified', str(squatted_id), 'google'),
        ('password_removed', str(squatted_id), 'unverified_email_claimed_by_google'),
        ('google_linked', str(squatted_id), None),
        ('login', str(squatted_id), 'google'),
        ('login_code_sent', str(linker_id), None),
        ('login_code_accepted', str(linker_id), None),
        ('login', str(linker_id), 'password'),
        ('google_linked', str(linker_id), None),
        ('login', str(linker_id), 'google'),
        ('google_unlinked', str(linker_id), None),
        ('google_login_failed', None, 'email_unverified'),
    ]
    assert_no_secrets(events, PASSWORD, 'new@example.com', 'linker@gmail.com', 'x@example.com', 'sub-new')


def test_rate_limit_hits_are_logged(events):
    client = make_app(ACCOUNTS_ENABLED=True, RATELIMIT_ENABLED=True).test_client()
    statuses = [client.post('/login', data={'email': 'a@example.com', 'password': 'x'}).status_code
                for _ in range(6)]
    assert statuses[-1] == 429
    limited = [e for e in events() if e['event'] == 'rate_limited']
    assert limited == [{'event': 'rate_limited', 'ip': '127.0.0.1', 'endpoint': 'auth.login',
                        'path': '/login', 'limit': '5_per_1_minute'}]


def test_events_logged_outside_requests_have_no_ip(accounts_app, events):
    with accounts_app.app_context():
        log_event('plaid_item_removed', 7, item='item-9', reason='subscription_ended')
    assert events() == [{'event': 'plaid_item_removed', 'user': '7', 'item': 'item-9', 'reason': 'subscription_ended'}]


def test_values_stay_on_one_line(accounts_app, events):
    with accounts_app.test_request_context():
        log_event('test', note='two\nlines and spaces')
    assert events()[0]['note'] == 'two_lines_and_spaces'
