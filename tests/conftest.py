import pytest

from serve import create_app


@pytest.fixture
def app():
    return create_app({
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite://',
    })


@pytest.fixture
def client(app):
    return app.test_client()
