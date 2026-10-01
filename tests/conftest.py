import pytest

from extensions import db
from models import User
from serve import create_app


def make_app(**overrides):
    config = {
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite://',
        'ACCOUNTS_ENABLED': False,
        'WTF_CSRF_ENABLED': False,
        'RATELIMIT_ENABLED': False,
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
