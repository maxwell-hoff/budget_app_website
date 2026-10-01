import pytest
from flask_migrate import upgrade
from sqlalchemy import inspect

from config import database_url, env_flag
from extensions import db
from serve import create_app


def test_healthz_ok(client):
    resp = client.get('/healthz')
    assert resp.status_code == 200
    assert resp.get_json() == {'status': 'ok'}


def test_healthz_reports_database_failure(client, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError('db down')

    monkeypatch.setattr(db.session, 'execute', broken)
    resp = client.get('/healthz')
    assert resp.status_code == 503
    assert resp.get_json()['status'] == 'error'


def test_accounts_disabled_by_default(monkeypatch):
    monkeypatch.delenv('ACCOUNTS_ENABLED', raising=False)
    assert create_app().config['ACCOUNTS_ENABLED'] is False


@pytest.mark.parametrize('value', ['1', 'true', 'TRUE', 'yes', 'on'])
def test_accounts_enabled_truthy_values(monkeypatch, value):
    monkeypatch.setenv('ACCOUNTS_ENABLED', value)
    assert create_app().config['ACCOUNTS_ENABLED'] is True


@pytest.mark.parametrize('value', ['0', 'false', 'no', 'off', ''])
def test_accounts_enabled_falsy_values(monkeypatch, value):
    monkeypatch.setenv('ACCOUNTS_ENABLED', value)
    assert env_flag('ACCOUNTS_ENABLED', default=True) is False


def test_secret_key_required_on_render(monkeypatch):
    monkeypatch.setenv('RENDER', 'true')
    monkeypatch.delenv('SECRET_KEY', raising=False)
    with pytest.raises(RuntimeError):
        create_app()


def test_render_uses_secure_cookies(monkeypatch):
    monkeypatch.setenv('RENDER', 'true')
    monkeypatch.setenv('SECRET_KEY', 'x' * 32)
    monkeypatch.delenv('SESSION_COOKIE_SECURE', raising=False)
    assert create_app().config['SESSION_COOKIE_SECURE'] is True


def test_dev_works_without_secret_key(monkeypatch):
    monkeypatch.delenv('RENDER', raising=False)
    monkeypatch.delenv('SECRET_KEY', raising=False)
    assert create_app().config['SECRET_KEY']


def test_database_url_defaults_to_sqlite_in_instance(monkeypatch, tmp_path):
    monkeypatch.delenv('DATABASE_URL', raising=False)
    assert database_url(tmp_path) == f"sqlite:///{tmp_path / 'app.db'}"


@pytest.mark.parametrize('raw', [
    'postgres://u:p@host:5432/db',
    'postgresql://u:p@host:5432/db',
])
def test_database_url_uses_psycopg_driver(monkeypatch, tmp_path, raw):
    monkeypatch.setenv('DATABASE_URL', raw)
    assert database_url(tmp_path) == 'postgresql+psycopg://u:p@host:5432/db'


def test_database_url_leaves_explicit_driver_alone(monkeypatch, tmp_path):
    monkeypatch.setenv('DATABASE_URL', 'sqlite:////tmp/other.db')
    assert database_url(tmp_path) == 'sqlite:////tmp/other.db'


def test_migrations_upgrade_runs(tmp_path):
    app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'm.db'}"})
    with app.app_context():
        upgrade()
        tables = inspect(db.engine).get_table_names()
        assert 'alembic_version' in tables
        assert 'users' in tables
