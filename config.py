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


def public_base_url(on_render):
    url = os.environ.get('PUBLIC_BASE_URL', '').strip().rstrip('/')
    if url:
        return url
    # Links in emails must not be built from the request's Host header, which an attacker
    # can set to their own domain to capture reset tokens.
    return 'https://workbenchbudgeting.com' if on_render else None


def load_config(instance_path):
    on_render = env_flag('RENDER')
    resend_api_key = os.environ.get('RESEND_API_KEY', '').strip()
    return {
        'RESEND_API_KEY': resend_api_key,
        'EMAIL_BACKEND': os.environ.get('EMAIL_BACKEND') or ('resend' if resend_api_key else 'console'),
        'EMAIL_FROM': os.environ.get('EMAIL_FROM') or 'Workbench Budgeting <noreply@workbenchbudgeting.com>',
        'PUBLIC_BASE_URL': public_base_url(on_render),
        'GOOGLE_CLIENT_ID': os.environ.get('GOOGLE_CLIENT_ID', '').strip(),
        'GOOGLE_CLIENT_SECRET': os.environ.get('GOOGLE_CLIENT_SECRET', '').strip(),
        'STRIPE_SECRET_KEY': os.environ.get('STRIPE_SECRET_KEY', '').strip(),
        'STRIPE_PRICE_ID': os.environ.get('STRIPE_PRICE_ID', '').strip(),
        'STRIPE_WEBHOOK_SECRET': os.environ.get('STRIPE_WEBHOOK_SECRET', '').strip(),
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
