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


def load_config(instance_path):
    return {
        # A random fallback keeps dev working; production must set SECRET_KEY so sessions
        # survive restarts and are shared across gunicorn workers.
        'SECRET_KEY': os.environ.get('SECRET_KEY') or secrets.token_hex(32),
        'SQLALCHEMY_DATABASE_URI': database_url(instance_path),
        'SQLALCHEMY_ENGINE_OPTIONS': {'pool_pre_ping': True},
        'ACCOUNTS_ENABLED': env_flag('ACCOUNTS_ENABLED'),
    }
