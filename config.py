import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# Real environment variables win over .env, so production (no .env file) is unaffected.
load_dotenv(BASE_DIR / '.env')

_TRUTHY = {'1', 'true', 'yes', 'on'}


def env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in _TRUTHY


def database_url(instance_path):
    url = os.environ.get('DATABASE_URL', '').strip()
    if not url:
        return f"sqlite:///{Path(instance_path) / 'app.db'}"
    # Render and Heroku hand out postgres:// URLs, which SQLAlchemy 2 rejects.
    if url.startswith('postgres://'):
        url = 'postgresql://' + url[len('postgres://'):]
    if url.startswith('postgresql://'):
        url = 'postgresql+psycopg://' + url[len('postgresql://'):]
    return url


def secret_key():
    key = os.environ.get('SECRET_KEY')
    if key:
        return key
    # Render sets RENDER=true. A random key there would log users out on every restart
    # and break sessions across gunicorn workers.
    if env_flag('RENDER'):
        raise RuntimeError('SECRET_KEY must be set in production.')
    return secrets.token_hex(32)


def load_config(instance_path):
    on_render = env_flag('RENDER')
    return {
        'SECRET_KEY': secret_key(),
        'SQLALCHEMY_DATABASE_URI': database_url(instance_path),
        'SQLALCHEMY_ENGINE_OPTIONS': {'pool_pre_ping': True},
        'ACCOUNTS_ENABLED': env_flag('ACCOUNTS_ENABLED'),
        'SESSION_COOKIE_SECURE': env_flag('SESSION_COOKIE_SECURE', default=on_render),
        'SESSION_COOKIE_SAMESITE': 'Lax',
        'REMEMBER_COOKIE_SECURE': env_flag('SESSION_COOKIE_SECURE', default=on_render),
        # Per-process counters: with several gunicorn workers the effective limit is
        # multiplied by the worker count. Good enough until a shared store is needed.
        'RATELIMIT_STORAGE_URI': 'memory://',
    }
