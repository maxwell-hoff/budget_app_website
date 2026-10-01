from urllib.parse import parse_qs, urlsplit

import pytest

import google_auth
from extensions import db
from models import OAuthIdentity, User, utcnow
from tests.conftest import make_app, signed_in_email

PASSWORD = 'correct horse battery'

GOOGLE_METADATA = {
    'issuer': 'https://accounts.google.com',
    'authorization_endpoint': 'https://accounts.google.com/o/oauth2/v2/auth',
    'token_endpoint': 'https://oauth2.googleapis.com/token',
    'jwks_uri': 'https://www.googleapis.com/oauth2/v3/certs',
}


class FakeGoogle:
    """Stands in for Google's servers: the metadata document, the token endpoint, and
    ID-token signature checks. Authlib's own state and nonce handling still runs."""

    def __init__(self):
        self.claims = None
        self.received_nonce = None


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogle()
    app = make_app(ACCOUNTS_ENABLED=True, GOOGLE_CLIENT_ID='test-client-id', GOOGLE_CLIENT_SECRET='test-secret')
    with app.app_context():
        client = google_auth.google_client()

    def parse_id_token(token, nonce, **kwargs):
        fake.received_nonce = nonce
        return dict(fake.claims)

    monkeypatch.setattr(client, 'load_server_metadata', lambda: GOOGLE_METADATA)
    monkeypatch.setattr(client, 'fetch_access_token', lambda **kwargs: {'access_token': 'at', 'id_token': 'idt'})
    monkeypatch.setattr(client, 'parse_id_token', parse_id_token)
    fake.app = app
    return fake


def claims(sub='google-sub-1', email='user@example.com', verified=True):
    return {'sub': sub, 'email': email, 'email_verified': verified}


def start(client, next_url=None):
    resp = client.get('/auth/google', query_string={'next': next_url} if next_url else None)
    assert resp.status_code == 302
    return {k: v[0] for k, v in parse_qs(urlsplit(resp.headers['Location']).query).items()}


def google_login(google, client, user_claims, next_url=None):
    params = start(client, next_url)
    google.claims = user_claims
    return client.get('/auth/google/callback', query_string={'code': 'auth-code', 'state': params['state']})


def add_user(app, email='user@example.com', password=PASSWORD, verified=True):
    with app.app_context():
        user = User(email=email, email_verified_at=utcnow() if verified else None)
        if password:
            user.set_password(password)
        db.session.add(user)
        db.session.commit()
        return user.id


def load(app, user_id):
    with app.app_context():
        user = db.session.get(User, user_id)
        identities = [(i.provider, i.provider_subject, i.email) for i in user.identities]
        return user.password_hash, user.email_verified_at, identities


def password_login(client, email='user@example.com', password=PASSWORD):
    return client.post('/login', data={'email': email, 'password': password})


# --- Availability -------------------------------------------------------------

@pytest.mark.parametrize('method,path', [
    ('get', '/auth/google'), ('get', '/auth/google/callback'), ('post', '/account/google/unlink'),
])
def test_routes_404_when_flag_off(client, method, path):
    assert getattr(client, method)(path).status_code == 404


def test_routes_404_when_google_not_configured(accounts_client):
    assert accounts_client.get('/auth/google').status_code == 404
    assert accounts_client.get('/auth/google/callback').status_code == 404


@pytest.mark.parametrize('path', ['/login', '/signup'])
def test_button_hidden_when_google_not_configured(accounts_client, path):
    assert b'Continue with Google' not in accounts_client.get(path).data


@pytest.mark.parametrize('path', ['/login', '/signup'])
def test_button_shown_when_configured(google, path):
    assert b'Continue with Google' in google.app.test_client().get(path).data


def test_start_redirects_to_google_with_state_and_nonce(google):
    params = start(google.app.test_client())
    assert params['client_id'] == 'test-client-id'
    assert params['redirect_uri'] == 'http://localhost/auth/google/callback'
    assert params['response_type'] == 'code'
    assert params['scope'] == 'openid email profile'
    assert params['state'] and params['nonce']


def test_redirect_uri_uses_public_base_url(monkeypatch):
    app = make_app(
        ACCOUNTS_ENABLED=True, GOOGLE_CLIENT_ID='cid', GOOGLE_CLIENT_SECRET='secret',
        PUBLIC_BASE_URL='https://workbenchbudgeting.com',
    )
    with app.app_context():
        monkeypatch.setattr(google_auth.google_client(), 'load_server_metadata', lambda: GOOGLE_METADATA)
    params = start(app.test_client())
    assert params['redirect_uri'] == 'https://workbenchbudgeting.com/auth/google/callback'


# --- Account matching ---------------------------------------------------------

def test_new_user_signs_up_with_google(google):
    client = google.app.test_client()
    resp = google_login(google, client, claims(email='New.Person@Gmail.com'))
    assert resp.status_code == 302
    assert resp.headers['Location'] == '/account'
    assert signed_in_email(client) == 'new.person@gmail.com'

    with google.app.app_context():
        user = db.session.scalar(db.select(User).filter_by(email='new.person@gmail.com'))
        assert user.password_hash is None
        assert user.email_verified_at is not None
        assert [(i.provider, i.provider_subject) for i in user.identities] == [('google', 'google-sub-1')]


def test_nonce_is_checked_against_the_one_sent(google):
    client = google.app.test_client()
    params = start(client)
    google.claims = claims()
    client.get('/auth/google/callback', query_string={'code': 'c', 'state': params['state']})
    assert google.received_nonce == params['nonce']


def test_existing_password_user_signs_in_with_google_same_account(google):
    user_id = add_user(google.app)
    client = google.app.test_client()
    google_login(google, client, claims())
    assert signed_in_email(client) == 'user@example.com'

    password_hash, _, identities = load(google.app, user_id)
    assert password_hash is not None
    assert identities == [('google', 'google-sub-1', 'user@example.com')]
    with google.app.app_context():
        assert db.session.query(User).count() == 1

    assert password_login(google.app.test_client()).status_code == 302  # password still works


def test_unverified_google_email_does_not_link(google):
    user_id = add_user(google.app)
    client = google.app.test_client()
    resp = google_login(google, client, claims(verified=False))
    assert resp.status_code == 400
    assert b"so we can&#39;t use it to sign in" in resp.data
    assert signed_in_email(client) is None
    assert load(google.app, user_id)[2] == []


def test_unverified_google_email_does_not_create_account(google):
    resp = google_login(google, google.app.test_client(), claims(email='someone@gmail.com', verified=False))
    assert resp.status_code == 400
    with google.app.app_context():
        assert db.session.query(User).count() == 0


def test_linking_unverified_local_account_drops_its_password(google):
    user_id = add_user(google.app, verified=False)
    squatter = google.app.test_client()
    password_login(squatter)
    assert signed_in_email(squatter) == 'user@example.com'

    owner = google.app.test_client()
    google_login(google, owner, claims())

    password_hash, verified_at, identities = load(google.app, user_id)
    assert password_hash is None
    assert verified_at is not None
    assert len(identities) == 1
    assert signed_in_email(owner) == 'user@example.com'
    assert signed_in_email(squatter) is None
    assert b'Invalid email or password.' in password_login(google.app.test_client()).data


def test_returning_user_matched_by_subject_even_if_email_changed(google):
    client = google.app.test_client()
    google_login(google, client, claims(email='old@gmail.com'))
    client.post('/logout')

    google_login(google, client, claims(email='new@gmail.com'))
    assert signed_in_email(client) == 'old@gmail.com'
    with google.app.app_context():
        assert db.session.query(User).count() == 1
        assert db.session.scalar(db.select(OAuthIdentity)).email == 'new@gmail.com'


def test_next_url_is_honored_when_local(google):
    resp = google_login(google, google.app.test_client(), claims(), next_url='/about')
    assert resp.headers['Location'] == '/about'


def test_external_next_url_is_ignored(google):
    resp = google_login(google, google.app.test_client(), claims(), next_url='https://evil.example.com/')
    assert resp.headers['Location'] == '/account'


# --- Rejected callbacks -------------------------------------------------------

def test_bad_state_rejected(google):
    client = google.app.test_client()
    start(client)
    google.claims = claims()
    resp = client.get('/auth/google/callback', query_string={'code': 'c', 'state': 'forged-state'})
    assert resp.status_code == 400
    assert signed_in_email(client) is None
    with google.app.app_context():
        assert db.session.query(User).count() == 0


def test_missing_state_rejected(google):
    client = google.app.test_client()
    start(client)
    google.claims = claims()
    assert client.get('/auth/google/callback', query_string={'code': 'c'}).status_code == 400


def test_callback_without_starting_rejected(google):
    google.claims = claims()
    resp = google.app.test_client().get('/auth/google/callback', query_string={'code': 'c', 'state': 'x'})
    assert resp.status_code == 400


def test_state_cannot_be_replayed(google):
    client = google.app.test_client()
    params = start(client)
    google.claims = claims()
    client.get('/auth/google/callback', query_string={'code': 'c', 'state': params['state']})
    client.post('/logout')
    resp = client.get('/auth/google/callback', query_string={'code': 'c', 'state': params['state']})
    assert resp.status_code == 400


def test_user_cancelled_at_google(google):
    client = google.app.test_client()
    params = start(client)
    resp = client.get('/auth/google/callback', query_string={'error': 'access_denied', 'state': params['state']})
    assert resp.status_code == 400
    assert signed_in_email(client) is None


def test_missing_email_claim_rejected(google):
    resp = google_login(google, google.app.test_client(), {'sub': 'x', 'email_verified': True})
    assert resp.status_code == 400


# --- Connecting and disconnecting from /account ---------------------------------

def test_logged_in_user_connects_google_with_a_different_email(google):
    user_id = add_user(google.app)
    client = google.app.test_client()
    password_login(client)
    resp = google_login(google, client, claims(email='personal@gmail.com'), next_url='/account')
    assert resp.headers['Location'] == '/account'
    assert load(google.app, user_id)[2] == [('google', 'google-sub-1', 'personal@gmail.com')]
    assert b'Google is now connected' in client.get('/account').data


def test_cannot_connect_google_account_linked_to_someone_else(google):
    google_login(google, google.app.test_client(), claims(email='other@gmail.com'))
    user_id = add_user(google.app)
    client = google.app.test_client()
    password_login(client)

    resp = google_login(google, client, claims(email='other@gmail.com'))
    assert resp.status_code == 400
    assert b'different Workbench account' in resp.data
    assert signed_in_email(client) == 'user@example.com'
    assert load(google.app, user_id)[2] == []


def test_account_page_shows_connect_button(google):
    add_user(google.app)
    client = google.app.test_client()
    password_login(client)
    page = client.get('/account').data
    assert b'Sign-in methods' in page
    assert b'Connect Google' in page


def test_account_page_hides_google_row_when_not_configured(accounts_client, make_user):
    make_user()
    password_login(accounts_client)
    page = accounts_client.get('/account').data
    assert b'Sign-in methods' in page
    assert b'Connect Google' not in page


def test_disconnect_google_when_password_set(google):
    user_id = add_user(google.app)
    client = google.app.test_client()
    google_login(google, client, claims())
    assert b'Disconnect' in client.get('/account').data

    resp = client.post('/account/google/unlink', follow_redirects=True)
    assert b'Google has been disconnected' in resp.data
    assert load(google.app, user_id)[2] == []


def test_cannot_disconnect_last_sign_in_method(google):
    client = google.app.test_client()
    google_login(google, client, claims(email='only-google@gmail.com'))
    page = client.get('/account').data
    assert b'Disconnect' not in page
    assert b'Set a password below' in page

    resp = client.post('/account/google/unlink', follow_redirects=True)
    assert b'Set a password before disconnecting Google' in resp.data
    with google.app.app_context():
        assert db.session.query(OAuthIdentity).count() == 1


def test_google_only_user_can_set_password_then_disconnect(google):
    client = google.app.test_client()
    google_login(google, client, claims(email='only-google@gmail.com'))
    client.post('/account', data={'password': 'a new password', 'confirm': 'a new password'})
    client.post('/account/google/unlink')
    with google.app.app_context():
        assert db.session.query(OAuthIdentity).count() == 0
    assert password_login(google.app.test_client(), 'only-google@gmail.com', 'a new password').status_code == 302


def test_unlink_requires_login(google):
    resp = google.app.test_client().post('/account/google/unlink')
    assert resp.status_code == 302
    assert '/login' in resp.headers['Location']


def test_unlink_requires_csrf(monkeypatch):
    app = make_app(ACCOUNTS_ENABLED=True, WTF_CSRF_ENABLED=True, GOOGLE_CLIENT_ID='cid', GOOGLE_CLIENT_SECRET='s')
    user_id = add_user(app)
    with app.app_context():
        db.session.add(OAuthIdentity(user_id=user_id, provider='google', provider_subject='s1', email='x@gmail.com'))
        db.session.commit()
        session_id = db.session.get(User, user_id).get_id()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = session_id
        sess['_fresh'] = True

    assert client.post('/account/google/unlink').status_code == 400
    with app.app_context():
        assert db.session.query(OAuthIdentity).count() == 1
