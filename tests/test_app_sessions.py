import hashlib
import html
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from app_auth import is_loopback_redirect_uri, pkce_challenge
from extensions import db
from models import AppSession, AuthCode, Subscription, User, utcnow
from tests.conftest import make_app
from tests.test_google import claims, google, google_login  # noqa: F401  (google is a fixture)

PASSWORD = 'correct horse battery'
REDIRECT_URI = 'http://127.0.0.1:5002/auth/callback'


class DesktopApp:
    """Plays the desktop app's side of the sign-in flow (step 14 builds the real one)."""

    def __init__(self, redirect_uri=REDIRECT_URI, device_name="Max's MacBook"):
        self.verifier = secrets.token_urlsafe(48)
        self.state = secrets.token_urlsafe(16)
        self.redirect_uri = redirect_uri
        self.device_name = device_name

    def login_params(self, **overrides):
        params = {
            'redirect_uri': self.redirect_uri,
            'code_challenge': pkce_challenge(self.verifier),
            'code_challenge_method': 'S256',
            'state': self.state,
            'device_name': self.device_name,
        }
        params.update(overrides)
        return {k: v for k, v in params.items() if v is not None}

    def login_path(self, **overrides):
        return '/app-login?' + urlencode(self.login_params(**overrides))

    def code_from(self, resp):
        """Checks the redirect to the app's callback, like the app's /auth/callback does."""
        assert resp.status_code == 302
        location = resp.headers['Location']
        assert location.startswith(self.redirect_uri + '?')
        query = parse_qs(urlsplit(location).query)
        assert query['state'] == [self.state]
        return query['code'][0]


def add_user(app, email='user@example.com', password=PASSWORD):
    with app.app_context():
        user = User(email=email, email_verified_at=utcnow())
        if password:
            user.set_password(password)
        db.session.add(user)
        db.session.commit()
        return user.id


def password_login(client, next_url=None, email='user@example.com', password=PASSWORD):
    return client.post('/login', query_string={'next': next_url} if next_url else None,
                       data={'email': email, 'password': password})


def approve(client, desktop):
    """Confirms the sign-in on /app-login and returns the one-time code."""
    assert client.get(desktop.login_path()).status_code == 200
    return desktop.code_from(client.post(desktop.login_path()))


def exchange(client, code, verifier, **extra):
    return client.post('/v1/auth/token', json={'code': code, 'code_verifier': verifier, **extra})


def sign_in(app, desktop=None):
    """Logs in on the web and signs the desktop app in; returns its session token."""
    desktop = desktop or DesktopApp()
    client = app.test_client()
    password_login(client)
    resp = exchange(app.test_client(), approve(client, desktop), desktop.verifier)
    assert resp.status_code == 200
    return resp.get_json()['session_token']


def same_url(a, b):
    """Same path and query parameters, however they're percent-encoded."""
    a, b = urlsplit(a), urlsplit(b)
    return a.path == b.path and parse_qs(a.query) == parse_qs(b.query)


def me(client, token):
    return client.get('/v1/me', headers={'Authorization': f'Bearer {token}'})


def assert_api_error(resp, status, code):
    assert resp.status_code == status
    assert resp.is_json
    body = resp.get_json()
    assert body['error']['code'] == code
    assert body['error']['message']


@pytest.fixture
def user_id(accounts_app):
    return add_user(accounts_app)


# --- Availability -------------------------------------------------------------

@pytest.mark.parametrize('method,path', [
    ('get', '/app-login'), ('post', '/app-login'),
    ('post', '/v1/auth/token'), ('get', '/v1/me'), ('post', '/v1/auth/logout'),
])
def test_routes_404_when_flag_off(client, method, path):
    assert getattr(client, method)(path).status_code == 404


# --- The full flow ------------------------------------------------------------

def test_full_flow_after_password_login(accounts_app, user_id):
    desktop = DesktopApp()
    browser = accounts_app.test_client()

    # Not logged in on the web yet: /app-login sends the browser to log in first.
    resp = browser.get(desktop.login_path())
    assert resp.status_code == 302
    login_url = urlsplit(resp.headers['Location'])
    assert login_url.path == '/login'
    next_url = parse_qs(login_url.query)['next'][0]
    assert same_url(next_url, desktop.login_path())

    resp = password_login(browser, next_url)
    assert resp.status_code == 302
    assert same_url(resp.headers['Location'], desktop.login_path())

    page = browser.get(desktop.login_path())
    assert page.status_code == 200
    assert b'user@example.com' in page.data
    assert b'Max&#39;s MacBook' in page.data
    with accounts_app.app_context():
        assert db.session.query(AuthCode).count() == 0  # nothing issued until Continue

    code = desktop.code_from(browser.post(desktop.login_path()))

    resp = exchange(accounts_app.test_client(), code, desktop.verifier)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['user'] == {'id': user_id, 'email': 'user@example.com'}
    expires_at = datetime.strptime(body['expires_at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    assert timedelta(days=179) < expires_at - utcnow() <= timedelta(days=180)

    resp = me(accounts_app.test_client(), body['session_token'])
    assert resp.status_code == 200
    assert resp.get_json() == {
        'user': {'id': user_id, 'email': 'user@example.com', 'email_verified': True},
        'plaid_access': False,
        'subscription': None,
        'account_url': 'http://localhost/account',
    }


def test_full_flow_after_google_login(google):  # noqa: F811
    desktop = DesktopApp()
    browser = google.app.test_client()

    resp = browser.get(desktop.login_path())
    next_url = parse_qs(urlsplit(resp.headers['Location']).query)['next'][0]
    # The log-in page's Google button carries the same `next`.
    login_page = browser.get('/login', query_string={'next': next_url}).data.decode()
    button = html.unescape(re.search(r'href="(/auth/google[^"]+)"', login_page).group(1))
    assert same_url(parse_qs(urlsplit(button).query)['next'][0], desktop.login_path())

    resp = google_login(google, browser, claims(email='new.person@gmail.com'), next_url=next_url)
    assert resp.status_code == 302
    assert same_url(resp.headers['Location'], desktop.login_path())

    code = approve(browser, desktop)
    token = exchange(google.app.test_client(), code, desktop.verifier).get_json()['session_token']

    body = me(google.app.test_client(), token).get_json()
    assert body['user']['email'] == 'new.person@gmail.com'
    assert body['user']['email_verified'] is True


def test_me_reports_plaid_access_and_subscription(accounts_app, user_id):
    with accounts_app.app_context():
        db.session.add(Subscription(
            user_id=user_id, stripe_customer_id='cus_1', stripe_subscription_id='sub_1', status='active',
            current_period_start=utcnow() - timedelta(days=3),
            current_period_end=datetime(2026, 11, 1, 12, 30, tzinfo=timezone.utc),
            cancel_at_period_end=True,
        ))
        db.session.commit()
    body = me(accounts_app.test_client(), sign_in(accounts_app)).get_json()
    assert body['plaid_access'] is True
    assert body['subscription'] == {
        'status': 'active', 'current_period_end': '2026-11-01T12:30:00Z', 'cancel_at_period_end': True,
    }


def test_me_without_access_for_lapsed_subscription(accounts_app, user_id):
    with accounts_app.app_context():
        db.session.add(Subscription(user_id=user_id, stripe_customer_id='cus_1', status='unpaid'))
        db.session.commit()
    body = me(accounts_app.test_client(), sign_in(accounts_app)).get_json()
    assert body['plaid_access'] is False
    assert body['subscription']['status'] == 'unpaid'


def test_account_url_uses_public_base_url():
    app = make_app(ACCOUNTS_ENABLED=True, PUBLIC_BASE_URL='https://workbenchbudgeting.com')
    add_user(app)
    assert me(app.test_client(), sign_in(app)).get_json()['account_url'] == 'https://workbenchbudgeting.com/account'


# --- /app-login -----------------------------------------------------------------

@pytest.mark.parametrize('uri', [
    'http://127.0.0.1:5002/auth/callback',
    'http://localhost:5002/auth/callback',
    'http://127.0.0.1:61234/auth/callback',
])
def test_loopback_redirect_uris_accepted(uri):
    assert is_loopback_redirect_uri(uri)


@pytest.mark.parametrize('uri', [
    'https://127.0.0.1:5002/auth/callback',
    'http://evil.example.com:5002/auth/callback',
    'http://127.0.0.1.evil.example.com:5002/auth/callback',
    'http://evil.example.com@127.0.0.1:5002/auth/callback',
    'http://127.0.0.1:5002@evil.example.com/auth/callback',
    'http://LOCALHOST:5002/auth/callback',
    'http://127.0.0.1/auth/callback',
    'http://127.0.0.1:0/auth/callback',
    'http://127.0.0.1:99999/auth/callback',
    'http://127.0.0.1:abc/auth/callback',
    'http://[::1]:5002/auth/callback',
    'http://127.0.0.1:5002/other',
    'http://127.0.0.1:5002/auth/callback/',
    'http://127.0.0.1:5002/auth/callback?x=1',
    'http://127.0.0.1:5002/auth/callback#x',
    '//127.0.0.1:5002/auth/callback',
    '',
])
def test_other_redirect_uris_rejected(uri):
    assert not is_loopback_redirect_uri(uri)


@pytest.mark.parametrize('overrides', [
    {'redirect_uri': 'https://evil.example.com/auth/callback'},
    {'redirect_uri': None},
    {'state': None},
    {'state': 'x' * 257},
    {'code_challenge': None},
    {'code_challenge': 'too-short'},
    {'code_challenge': 'a' * 42 + '+'},
    {'code_challenge_method': 'plain'},
    {'code_challenge_method': None},
])
@pytest.mark.parametrize('method', ['get', 'post'])
def test_invalid_requests_show_an_error_and_never_redirect(accounts_app, user_id, overrides, method):
    browser = accounts_app.test_client()
    password_login(browser)
    resp = getattr(browser, method)(DesktopApp().login_path(**overrides))
    assert resp.status_code == 400
    assert b"This sign-in link isn" in resp.data
    with accounts_app.app_context():
        assert db.session.query(AuthCode).count() == 0


def test_device_name_is_optional_and_trimmed(accounts_app, user_id):
    browser = accounts_app.test_client()
    password_login(browser)
    desktop = DesktopApp(device_name=None)
    assert b' on <strong>' not in browser.get(desktop.login_path()).data
    desktop.code_from(browser.post(desktop.login_path()))

    desktop = DesktopApp(device_name='  Work \n laptop ' + 'x' * 200)
    desktop.code_from(browser.post(desktop.login_path()))
    with accounts_app.app_context():
        names = [c.device_name for c in db.session.scalars(db.select(AuthCode).order_by(AuthCode.id))]
    assert names == [None, ('Work laptop ' + 'x' * 200)[:100]]


def test_post_requires_csrf_token():
    app = make_app(ACCOUNTS_ENABLED=True, WTF_CSRF_ENABLED=True)
    add_user(app)
    browser = app.test_client()
    with app.app_context():
        user = db.session.scalar(db.select(User))
        with browser.session_transaction() as sess:
            sess['_user_id'] = user.get_id()
            sess['_fresh'] = True
    desktop = DesktopApp()
    page = browser.get(desktop.login_path())
    assert page.status_code == 200
    assert browser.post(desktop.login_path()).status_code == 400

    token = re.search(rb'name="csrf_token" type="hidden" value="([^"]+)"', page.data).group(1).decode()
    desktop.code_from(browser.post(desktop.login_path(), data={'csrf_token': token}))


def test_cancel_link_reports_access_denied(accounts_app, user_id):
    browser = accounts_app.test_client()
    password_login(browser)
    desktop = DesktopApp()
    page = browser.get(desktop.login_path()).data.decode()
    cancel = html.unescape(re.search(r'<a href="([^"]+)">Cancel</a>', page).group(1))
    assert cancel.startswith(REDIRECT_URI + '?')
    assert parse_qs(urlsplit(cancel).query) == {'error': ['access_denied'], 'state': [desktop.state]}


def test_use_a_different_account(accounts_app, user_id):
    add_user(accounts_app, email='other@example.com')
    browser = accounts_app.test_client()
    password_login(browser)
    desktop = DesktopApp()
    page = browser.get(desktop.login_path()).data.decode()
    action = html.unescape(re.search(r'<form method="post" action="(/logout[^"]+)"', page).group(1))

    resp = browser.post(action)
    assert resp.status_code == 302
    assert same_url(resp.headers['Location'], desktop.login_path())
    assert browser.get(desktop.login_path()).headers['Location'].startswith('/login?next=')

    password_login(browser, email='other@example.com')
    token = exchange(accounts_app.test_client(), approve(browser, desktop), desktop.verifier).get_json()['session_token']
    assert me(accounts_app.test_client(), token).get_json()['user']['email'] == 'other@example.com'


def test_logout_ignores_offsite_next(accounts_app, user_id):
    browser = accounts_app.test_client()
    password_login(browser)
    resp = browser.post('/logout', query_string={'next': 'https://evil.example.com/'})
    assert resp.headers['Location'] == '/'


# --- POST /v1/auth/token --------------------------------------------------------

def _approved_code(app, desktop=None):
    desktop = desktop or DesktopApp()
    browser = app.test_client()
    password_login(browser)
    return desktop, approve(browser, desktop)


def test_codes_and_tokens_are_stored_hashed(accounts_app, user_id):
    desktop, code = _approved_code(accounts_app)
    token = exchange(accounts_app.test_client(), code, desktop.verifier).get_json()['session_token']
    with accounts_app.app_context():
        auth_code = db.session.scalar(db.select(AuthCode))
        session = db.session.scalar(db.select(AppSession))
        assert auth_code.code_hash == hashlib.sha256(code.encode()).hexdigest()
        assert session.token_hash == hashlib.sha256(token.encode()).hexdigest()
        stored = [getattr(row, c.name) for row in (auth_code, session) for c in row.__table__.columns]
        assert code not in stored and token not in stored
        assert session.device_name == "Max's MacBook"
        assert session.user_id == user_id


def test_code_is_single_use(accounts_app, user_id):
    desktop, code = _approved_code(accounts_app)
    client = accounts_app.test_client()
    assert exchange(client, code, desktop.verifier).status_code == 200
    assert_api_error(exchange(client, code, desktop.verifier), 400, 'bad_request')


def test_wrong_verifier_rejected_and_burns_the_code(accounts_app, user_id):
    desktop, code = _approved_code(accounts_app)
    client = accounts_app.test_client()
    assert_api_error(exchange(client, code, secrets.token_urlsafe(48)), 400, 'bad_request')
    assert_api_error(exchange(client, code, desktop.verifier), 400, 'bad_request')


def test_expired_code_rejected(accounts_app, user_id):
    desktop, code = _approved_code(accounts_app)
    with accounts_app.app_context():
        db.session.scalar(db.select(AuthCode)).expires_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
    assert_api_error(exchange(accounts_app.test_client(), code, desktop.verifier), 400, 'bad_request')


def test_codes_expire_after_five_minutes(accounts_app, user_id):
    _approved_code(accounts_app)
    with accounts_app.app_context():
        row = db.session.scalar(db.select(AuthCode))
        assert row.expires_at - row.created_at == timedelta(minutes=5)


def test_old_codes_are_cleaned_up(accounts_app, user_id):
    with accounts_app.app_context():
        db.session.add(AuthCode(
            user_id=user_id, code_hash='old', code_challenge='c' * 43, redirect_uri=REDIRECT_URI,
            expires_at=utcnow() - timedelta(days=2),
        ))
        db.session.commit()
    _approved_code(accounts_app)
    with accounts_app.app_context():
        assert db.session.query(AuthCode).filter_by(code_hash='old').count() == 0


@pytest.mark.parametrize('body', [
    None, [], 'text', {}, {'code': 'x'}, {'code_verifier': 'x' * 43},
    {'code': 1, 'code_verifier': 'x' * 43}, {'code': 'x', 'code_verifier': 'x' * 43, 'device_name': 5},
    {'code': 'unknown', 'code_verifier': 'x' * 43}, {'code': 'x', 'code_verifier': 'short'},
])
def test_bad_token_requests(accounts_client, body):
    resp = accounts_client.post('/v1/auth/token', json=body) if body is not None else accounts_client.post(
        '/v1/auth/token', data='not json', content_type='application/json')
    assert_api_error(resp, 400, 'bad_request')


def test_device_name_from_token_request_wins(accounts_app, user_id):
    desktop, code = _approved_code(accounts_app)
    exchange(accounts_app.test_client(), code, desktop.verifier, device_name='  Studio iMac ')
    with accounts_app.app_context():
        assert db.session.scalar(db.select(AppSession)).device_name == 'Studio iMac'


def test_token_endpoint_rate_limited():
    app = make_app(ACCOUNTS_ENABLED=True, RATELIMIT_ENABLED=True)
    client = app.test_client()
    for _ in range(20):
        assert client.post('/v1/auth/token', json={}).status_code == 400
    resp = client.post('/v1/auth/token', json={})
    assert_api_error(resp, 429, 'rate_limited')
    assert 1 <= int(resp.headers['Retry-After']) <= 61


# --- Sessions -------------------------------------------------------------------

@pytest.mark.parametrize('headers', [
    {}, {'Authorization': ''}, {'Authorization': 'Bearer'}, {'Authorization': 'Bearer nope'},
    {'Authorization': 'Basic dXNlcjpwYXNz'},
])
def test_me_requires_a_valid_token(accounts_client, headers):
    resp = accounts_client.get('/v1/me', headers=headers)
    assert_api_error(resp, 401, 'unauthorized')
    assert resp.headers['WWW-Authenticate'] == 'Bearer'


def test_logout_revokes_the_token(accounts_app, user_id):
    token = sign_in(accounts_app)
    other = sign_in(accounts_app)
    client = accounts_app.test_client()
    resp = client.post('/v1/auth/logout', headers={'Authorization': f'Bearer {token}'})
    assert resp.status_code == 204
    assert resp.data == b''
    assert_api_error(me(client, token), 401, 'unauthorized')
    assert_api_error(client.post('/v1/auth/logout', headers={'Authorization': f'Bearer {token}'}), 401, 'unauthorized')
    assert me(client, other).status_code == 200  # other devices stay signed in
    with accounts_app.app_context():
        assert db.session.scalar(db.select(AppSession).filter_by(revoked_at=None)) is not None
        assert db.session.query(AppSession).filter(AppSession.revoked_at.isnot(None)).count() == 1


def test_expired_token_rejected(accounts_app, user_id):
    token = sign_in(accounts_app)
    with accounts_app.app_context():
        db.session.scalar(db.select(AppSession)).expires_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
    assert_api_error(me(accounts_app.test_client(), token), 401, 'unauthorized')


def test_password_change_ends_app_sessions(accounts_app, user_id):
    token = sign_in(accounts_app)
    browser = accounts_app.test_client()
    password_login(browser)
    resp = browser.post('/account', data={
        'current_password': PASSWORD, 'password': 'a brand new password', 'confirm': 'a brand new password',
    })
    assert resp.status_code == 302
    assert_api_error(me(accounts_app.test_client(), token), 401, 'unauthorized')


def test_password_removed_by_google_link_ends_app_sessions(google):  # noqa: F811
    with google.app.app_context():
        user = User(email='user@example.com')  # never verified
        user.set_password(PASSWORD)
        db.session.add(user)
        db.session.commit()
    token = sign_in(google.app)
    assert me(google.app.test_client(), token).status_code == 200

    google_login(google, google.app.test_client(), claims())  # the real owner signs in with Google
    assert_api_error(me(google.app.test_client(), token), 401, 'unauthorized')


def test_deleted_user_tokens_stop_working(accounts_app, user_id):
    token = sign_in(accounts_app)
    with accounts_app.app_context():
        db.session.delete(db.session.get(User, user_id))
        db.session.commit()
        assert db.session.query(AppSession).count() == 0
    assert_api_error(me(accounts_app.test_client(), token), 401, 'unauthorized')


def test_last_used_at_is_updated(accounts_app, user_id):
    token = sign_in(accounts_app)
    long_ago = utcnow() - timedelta(hours=1)
    with accounts_app.app_context():
        db.session.scalar(db.select(AppSession)).last_used_at = long_ago
        db.session.commit()
    me(accounts_app.test_client(), token)
    with accounts_app.app_context():
        last_used = db.session.scalar(db.select(AppSession)).last_used_at.replace(tzinfo=timezone.utc)
    assert last_used > long_ago + timedelta(minutes=59)
