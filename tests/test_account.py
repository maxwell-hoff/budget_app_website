import pytest

from extensions import db
from models import User, utcnow
from tests.conftest import make_app, signed_in_email

PASSWORD = 'correct horse battery'
NEW_PASSWORD = 'a brand new passphrase'


def login(client, email='user@example.com', password=PASSWORD):
    return client.post('/login', data={'email': email, 'password': password})


def change_password(client, current=PASSWORD, new=NEW_PASSWORD, confirm=None, **kwargs):
    data = {'password': new, 'confirm': new if confirm is None else confirm}
    if current is not None:
        data['current_password'] = current
    return client.post('/account', data=data, **kwargs)


# --- Access -----------------------------------------------------------------

@pytest.mark.parametrize('method', ['get', 'post'])
def test_account_404_when_flag_off(client, method):
    assert getattr(client, method)('/account').status_code == 404


def test_account_redirects_to_login_when_logged_out(accounts_client):
    resp = accounts_client.get('/account')
    assert resp.status_code == 302
    assert resp.headers['Location'] == '/login?next=%2Faccount'


def test_login_then_lands_on_account(accounts_client, make_user):
    make_user()
    resp = login(accounts_client)
    assert resp.headers['Location'] == '/account'


def test_login_page_redirects_when_already_logged_in(accounts_client, make_user):
    make_user()
    login(accounts_client)
    resp = accounts_client.get('/login')
    assert resp.status_code == 302
    assert resp.headers['Location'] == '/account'


def test_signup_lands_on_account(accounts_client):
    resp = accounts_client.post('/signup', data={
        'email': 'new@example.com', 'password': PASSWORD, 'confirm': PASSWORD,
    }, follow_redirects=True)
    assert b'My account' in resp.data
    assert b'We sent a link to new@example.com' in resp.data


# --- Page contents ----------------------------------------------------------

def test_account_page_shows_details(accounts_client, make_user):
    make_user()
    login(accounts_client)
    resp = accounts_client.get('/account')
    assert resp.status_code == 200
    assert signed_in_email(accounts_client) == 'user@example.com'
    assert b'Not verified' in resp.data
    assert b'Resend verification link' in resp.data
    assert b'Not subscribed' in resp.data
    assert b'Change password' in resp.data
    assert b'Current password' in resp.data
    assert b'Log out' in resp.data


def test_account_page_shows_verified(accounts_app, accounts_client, make_user):
    user_id = make_user()
    with accounts_app.app_context():
        db.session.get(User, user_id).email_verified_at = utcnow()
        db.session.commit()
    login(accounts_client)
    resp = accounts_client.get('/account')
    assert b'Verified' in resp.data
    assert b'Resend verification link' not in resp.data


# --- Change password --------------------------------------------------------

def test_change_password(accounts_app, accounts_client, make_user):
    make_user()
    login(accounts_client)
    resp = change_password(accounts_client, follow_redirects=True)
    assert b'Your password has been updated.' in resp.data
    assert signed_in_email(accounts_client) == 'user@example.com'

    fresh = accounts_app.test_client()
    assert b'Invalid email or password.' in login(fresh).data
    assert login(fresh, password=NEW_PASSWORD).status_code == 302


def test_change_password_ends_other_sessions(accounts_app, make_user):
    make_user()
    this_device = accounts_app.test_client()
    other_device = accounts_app.test_client()
    login(this_device)
    login(other_device)

    change_password(this_device)

    assert signed_in_email(this_device) == 'user@example.com'
    assert signed_in_email(other_device) is None


def test_change_password_wrong_current(accounts_app, accounts_client, make_user):
    make_user()
    login(accounts_client)
    resp = change_password(accounts_client, current='not my password')
    assert resp.status_code == 200
    assert b'Current password is incorrect.' in resp.data
    assert login(accounts_app.test_client()).status_code == 302  # old password still works


def test_change_password_requires_current_for_password_users(accounts_client, make_user):
    make_user()
    login(accounts_client)
    resp = change_password(accounts_client, current=None)
    assert b'Current password is incorrect.' in resp.data


@pytest.mark.parametrize('kwargs,message', [
    ({'new': 'short'}, b'Password must be 8 to 128 characters.'),
    ({'confirm': 'something else'}, b'Passwords must match.'),
])
def test_change_password_validation(accounts_client, make_user, kwargs, message):
    make_user()
    login(accounts_client)
    resp = change_password(accounts_client, **kwargs)
    assert resp.status_code == 200
    assert message in resp.data


def log_in_as(app, client, user_id):
    """Put a user's session straight into the client, for users who can't log in with a form."""
    with app.app_context():
        session_id = db.session.get(User, user_id).get_id()
    with client.session_transaction() as sess:
        sess['_user_id'] = session_id
        sess['_fresh'] = True


def test_passwordless_user_can_set_password(accounts_app, accounts_client, make_user):
    user_id = make_user(email='google-only@example.com', password=None)
    log_in_as(accounts_app, accounts_client, user_id)

    page = accounts_client.get('/account')
    assert b'Set a password' in page.data
    assert b'Current password' not in page.data

    resp = change_password(accounts_client, current=None, follow_redirects=True)
    assert b'Your password has been set.' in resp.data
    assert login(accounts_app.test_client(), email='google-only@example.com', password=NEW_PASSWORD).status_code == 302


def test_change_password_requires_csrf():
    app = make_app(ACCOUNTS_ENABLED=True, WTF_CSRF_ENABLED=True)
    with app.app_context():
        user = User(email='user@example.com')
        user.set_password(PASSWORD)
        db.session.add(user)
        db.session.commit()
        user_id = user.id
    client = app.test_client()
    log_in_as(app, client, user_id)

    resp = change_password(client)
    assert resp.status_code == 200
    with app.app_context():
        assert db.session.get(User, user_id).check_password(PASSWORD)


# --- Nav link ---------------------------------------------------------------

@pytest.mark.parametrize('path', ['/', '/about'])
def test_nav_shows_account_link_when_logged_in(accounts_client, make_user, path):
    make_user()
    login(accounts_client)
    assert b'<a href="/account">Account</a>' in accounts_client.get(path).data


@pytest.mark.parametrize('path', ['/', '/about'])
def test_nav_hides_account_link_when_logged_out(accounts_client, path):
    assert b'/account' not in accounts_client.get(path).data


def test_account_page_marks_nav_link_current(accounts_client, make_user):
    make_user()
    login(accounts_client)
    assert b'aria-current="page">Account</a>' in accounts_client.get('/account').data
