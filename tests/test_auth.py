import re

import pytest

from extensions import db
from models import User
from tests.conftest import make_app

PASSWORD = 'correct horse battery'


def signup(client, email='New@Example.com ', password=PASSWORD, confirm=None, **kwargs):
    return client.post('/signup', data={
        'email': email,
        'password': password,
        'confirm': password if confirm is None else confirm,
    }, **kwargs)


def login(client, email='user@example.com', password=PASSWORD, **kwargs):
    return client.post('/login', data={'email': email, 'password': password}, **kwargs)


def signed_in_email(client):
    resp = client.get('/login')
    match = re.search(rb'Signed in as <strong>([^<]+)</strong>', resp.data)
    return match.group(1).decode() if match else None


# --- Flag off ---------------------------------------------------------------

@pytest.mark.parametrize('method,path', [
    ('get', '/signup'), ('post', '/signup'),
    ('get', '/login'), ('post', '/login'),
    ('get', '/logout'), ('post', '/logout'),
])
def test_routes_404_when_flag_off(client, method, path):
    assert getattr(client, method)(path).status_code == 404


def test_flag_off_404_even_after_many_login_posts():
    client = make_app(RATELIMIT_ENABLED=True).test_client()
    statuses = {client.post('/login', data={}).status_code for _ in range(10)}
    assert statuses == {404}


def test_site_pages_unchanged_by_flag(client, accounts_client):
    for path in ['/', '/about']:
        off = client.get(path)
        on = accounts_client.get(path)
        assert off.status_code == on.status_code == 200
        assert off.data == on.data
        assert b'/login' not in off.data and b'/signup' not in off.data


# --- Sign up ----------------------------------------------------------------

def test_signup_page_renders(accounts_client):
    resp = accounts_client.get('/signup')
    assert resp.status_code == 200
    assert b'Create your account' in resp.data


def test_signup_creates_user_and_logs_in(accounts_app, accounts_client):
    resp = signup(accounts_client)
    assert resp.status_code == 302
    assert signed_in_email(accounts_client) == 'new@example.com'

    with accounts_app.app_context():
        user = db.session.scalar(db.select(User))
        assert user.email == 'new@example.com'
        assert user.password_hash and PASSWORD not in user.password_hash
        assert user.password_hash.startswith('scrypt:')
        assert user.created_at is not None
        assert user.email_verified_at is None


def test_signup_rejects_duplicate_email_case_insensitively(accounts_app, accounts_client, make_user):
    make_user(email='user@example.com')
    resp = signup(accounts_client, email='USER@example.com')
    assert resp.status_code == 200
    assert b'already exists' in resp.data
    with accounts_app.app_context():
        assert db.session.query(User).count() == 1


@pytest.mark.parametrize('kwargs,message', [
    ({'password': 'short'}, b'Password must be 8 to 128 characters.'),
    ({'confirm': 'something else'}, b'Passwords must match'),
    ({'email': 'not-an-email'}, b'Invalid email'),
])
def test_signup_validation_errors(accounts_app, accounts_client, kwargs, message):
    resp = signup(accounts_client, **kwargs)
    assert resp.status_code == 200
    assert message in resp.data
    with accounts_app.app_context():
        assert db.session.query(User).count() == 0


# --- Log in / log out -------------------------------------------------------

def test_login_page_renders(accounts_client):
    resp = accounts_client.get('/login')
    assert resp.status_code == 200
    assert b'Log in' in resp.data


def test_login_success_with_any_email_case(accounts_client, make_user):
    make_user()
    resp = login(accounts_client, email='  User@Example.COM')
    assert resp.status_code == 302
    assert signed_in_email(accounts_client) == 'user@example.com'


def test_login_wrong_password(accounts_client, make_user):
    make_user()
    resp = login(accounts_client, password='wrong password')
    assert resp.status_code == 200
    assert b'Invalid email or password.' in resp.data
    assert signed_in_email(accounts_client) is None


def test_login_unknown_email_gives_same_error(accounts_client):
    resp = login(accounts_client, email='nobody@example.com')
    assert b'Invalid email or password.' in resp.data


def test_login_rejects_user_without_password(accounts_client, make_user):
    make_user(email='google-only@example.com', password=None)
    resp = login(accounts_client, email='google-only@example.com', password='anything at all')
    assert b'Invalid email or password.' in resp.data
    assert signed_in_email(accounts_client) is None


def test_logout(accounts_client, make_user):
    make_user()
    login(accounts_client)
    resp = accounts_client.post('/logout')
    assert resp.status_code == 302
    assert resp.headers['Location'] == '/'
    assert signed_in_email(accounts_client) is None


def test_logout_get_not_allowed(accounts_client):
    assert accounts_client.get('/logout').status_code == 405


@pytest.mark.parametrize('next_url,expected', [
    ('/about', '/about'),
    ('https://evil.example.com/', '/login'),
    ('//evil.example.com/', '/login'),
    ('/\\evil.example.com', '/login'),
])
def test_login_next_redirect_is_local_only(accounts_client, make_user, next_url, expected):
    make_user()
    resp = login(accounts_client, query_string={'next': next_url})
    assert resp.headers['Location'] == expected


# --- CSRF and rate limiting -------------------------------------------------

def test_forms_require_csrf_token(make_user):
    app = make_app(ACCOUNTS_ENABLED=True, WTF_CSRF_ENABLED=True)
    client = app.test_client()

    resp = signup(client)
    assert resp.status_code == 200
    with app.app_context():
        assert db.session.query(User).count() == 0

    page = client.get('/signup').data.decode()
    token = re.search(r'name="csrf_token" type="hidden" value="([^"]+)"', page).group(1)
    resp = client.post('/signup', data={
        'csrf_token': token, 'email': 'a@example.com', 'password': PASSWORD, 'confirm': PASSWORD,
    })
    assert resp.status_code == 302

    assert client.post('/logout').status_code == 400


def test_notify_still_works_with_csrf_enabled():
    client = make_app(ACCOUNTS_ENABLED=True, WTF_CSRF_ENABLED=True).test_client()
    resp = client.post('/notify', json={'email': 'someone@example.com'})
    assert resp.status_code == 200


def test_login_is_rate_limited():
    client = make_app(ACCOUNTS_ENABLED=True, RATELIMIT_ENABLED=True).test_client()
    statuses = [login(client, email='x@example.com', password='nope').status_code for _ in range(6)]
    assert statuses[:5] == [200] * 5
    assert statuses[5] == 429


def test_rate_limit_does_not_apply_to_get(accounts_app):
    client = make_app(ACCOUNTS_ENABLED=True, RATELIMIT_ENABLED=True).test_client()
    assert all(client.get('/login').status_code == 200 for _ in range(10))
