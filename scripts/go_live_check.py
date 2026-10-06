"""Read-only preflight for going live (step 18): checks that the production settings in
Stripe, Plaid, Google, Resend, the database, and the public site match what the code expects.

Run it where the production env vars are, e.g. in the Render Shell (service → Shell):

    python scripts/go_live_check.py

Or locally against test mode (Stripe test keys from .env, Plaid's sandbox through
PLAID_SANDBOX_CLIENT_ID / PLAID_SANDBOX_SECRET):

    python scripts/go_live_check.py --mode test

It only reads: no Stripe objects are created, no Plaid calls that bill, no email is sent,
and nothing is written to the database. It works with ACCOUNTS_ENABLED on or off.
Exit status 1 if any check fails; warnings don't fail.
"""
import argparse
import os
import sys
from pathlib import Path

import requests
import stripe

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import billing  # noqa: E402
import plaid_api  # noqa: E402
from config import BASE_DIR  # noqa: E402

SITE = 'https://workbenchbudgeting.com'
PRODUCT_NAME = 'Workbench Budgeting'
PRICE_AMOUNT = 899
PRICE_CURRENCY = 'usd'
TAX_CODE = 'txcd_10103000'
TRIAL_DAYS = 7
WEBHOOK_EVENTS = frozenset(billing._HANDLERS)
PUBLIC_PAGES = ('/healthz', '/privacy', '/terms', '/refunds')
RESEND_DOMAINS_URL = 'https://api.resend.com/domains'


class Report:
    def __init__(self):
        self.failures = []
        self.warnings = []

    def section(self, title):
        print(f'\n{title}')

    def ok(self, label, detail=''):
        self._line('ok', label, detail)

    def warn(self, label, detail=''):
        self.warnings.append(label)
        self._line('warn', label, detail)

    def fail(self, label, detail=''):
        self.failures.append(label)
        self._line('FAIL', label, detail)

    def note(self, label, detail=''):
        self._line('info', label, detail)

    def check(self, label, ok, detail='', warn_only=False):
        if ok:
            self.ok(label, detail)
        elif warn_only:
            self.warn(label, detail)
        else:
            self.fail(label, detail)
        return ok

    def _line(self, tag, label, detail):
        print(f"  [{tag}] {label}{f' ({detail})' if detail else ''}", flush=True)


def _as_dict(obj):
    return obj.to_dict() if hasattr(obj, 'to_dict') else obj


def _id(value):
    return value.get('id') if isinstance(value, dict) else value


def _host(url):
    # Stripe keeps the business website as typed, often without the scheme.
    host = (url or '').lower().split('://', 1)[-1].split('/', 1)[0]
    return host.removeprefix('www.')


# --- Config ---

def check_config(report, config, mode, environ):
    report.section('Config')
    live = mode == 'live'
    report.note('ACCOUNTS_ENABLED', 'on' if config['ACCOUNTS_ENABLED'] else 'off; keep it off except while testing, until step 19')
    report.check('SECRET_KEY is set', bool(environ.get('SECRET_KEY')), 'without it every restart signs everyone out',
                 warn_only=not live)
    base = config.get('PUBLIC_BASE_URL')
    if live:
        report.check('PUBLIC_BASE_URL', base == SITE, base or 'unset', warn_only=bool(base))
        report.check('Secure session cookies', bool(config.get('SESSION_COOKIE_SECURE')))
    else:
        report.note('PUBLIC_BASE_URL', base or 'unset (links use the request host)')
    trial = config.get('TRIAL_DAYS')
    if not isinstance(trial, int) or isinstance(trial, bool) or not 0 <= trial <= billing.MAX_TRIAL_DAYS:
        report.fail('TRIAL_DAYS', f'{trial!r} is not a whole number from 0 to {billing.MAX_TRIAL_DAYS}')
    else:
        report.check('TRIAL_DAYS', trial == TRIAL_DAYS, f'{trial}; the plan is {TRIAL_DAYS}', warn_only=True)


# --- Database ---

def check_database(report, app, mode):
    from alembic.config import Config as AlembicConfig
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from sqlalchemy import text

    from extensions import db

    report.section('Database')
    with app.app_context():
        try:
            dialect = db.engine.dialect.name
            with db.engine.connect() as conn:
                conn.execute(text('SELECT 1'))
                current = set(MigrationContext.configure(conn).get_current_heads())
        except Exception as exc:
            report.fail('Database reachable', type(exc).__name__)
            return
        report.ok('Database reachable', dialect)
        if mode == 'live':
            report.check('Database is Postgres', dialect == 'postgresql', dialect)
        alembic_config = AlembicConfig()
        alembic_config.set_main_option('script_location', str(BASE_DIR / 'migrations'))
        heads = set(ScriptDirectory.from_config(alembic_config).get_heads())
        report.check('Migrations up to date', current == heads,
                     f"at {', '.join(sorted(current)) or 'none'}, head {', '.join(sorted(heads))}")


# --- Stripe ---

def check_stripe(report, config, mode, client=None, base_url=SITE):
    report.section('Stripe')
    live = mode == 'live'
    key = config.get('STRIPE_SECRET_KEY', '')
    price_id = config.get('STRIPE_PRICE_ID', '')
    secret = config.get('STRIPE_WEBHOOK_SECRET', '')
    if not (key and price_id and secret):
        report.fail('STRIPE_SECRET_KEY, STRIPE_PRICE_ID, STRIPE_WEBHOOK_SECRET all set',
                    'billing stays hidden without all three')
        if not key:
            return
    prefix = 'live' if live else 'test'
    report.check(f'Secret key is a {prefix}-mode key', key.startswith((f'sk_{prefix}_', f'rk_{prefix}_')), key[:8] + '…')
    if secret:
        report.check('Webhook secret looks like a signing secret', secret.startswith('whsec_'))
    client = client or stripe.StripeClient(key)

    def attempt(label, call):
        try:
            return call()
        except stripe.StripeError as exc:
            report.fail(label, f'{type(exc).__name__}: {exc.user_message or exc}')
            return None

    account = attempt('Stripe account', lambda: _as_dict(client.v1.accounts.retrieve_current()))
    if account is not None:
        report.check('Account can take payments', account.get('charges_enabled'), warn_only=not live)
        report.check('Account can receive payouts', account.get('payouts_enabled'), warn_only=True)
        site = (account.get('business_profile') or {}).get('url')
        report.check('Business website', _host(site) == _host(base_url), site or 'unset', warn_only=True)

    if price_id:
        price = attempt('Price', lambda: _as_dict(client.v1.prices.retrieve(price_id, params={'expand': ['product']})))
        if price is not None:
            _check_price(report, price, live)

    endpoints = attempt('Webhook endpoints', lambda: _as_dict(client.v1.webhook_endpoints.list(params={'limit': 100})))
    if endpoints is not None:
        _check_webhook(report, endpoints.get('data') or [], f'{base_url}/stripe/webhook', live)

    portals = attempt('Customer portal', lambda: _as_dict(
        client.v1.billing_portal.configurations.list(params={'is_default': True, 'limit': 1})))
    if portals is not None:
        _check_portal(report, (portals.get('data') or [None])[0], base_url)


def _check_price(report, price, live):
    report.check('Price is active', price.get('active'))
    recurring = price.get('recurring') or {}
    amount = price.get('unit_amount')
    currency = price.get('currency')
    monthly = recurring.get('interval') == 'month' and recurring.get('interval_count', 1) == 1
    report.check('Price is $8.99 USD a month', amount == PRICE_AMOUNT and currency == PRICE_CURRENCY and monthly,
                 f"{amount} {currency} every {recurring.get('interval_count')} {recurring.get('interval')}")
    report.check('Price is in the same mode as the key', price.get('livemode') == live)
    product = price.get('product') if isinstance(price.get('product'), dict) else {}
    report.check('Product is active', product.get('active'))
    report.check('Product name', product.get('name') == PRODUCT_NAME, product.get('name') or 'unknown', warn_only=True)
    report.check('Product tax code (Managed Payments)', _id(product.get('tax_code')) == TAX_CODE,
                 _id(product.get('tax_code')) or 'unset', warn_only=True)


def _check_webhook(report, endpoints, url, live):
    matches = [e for e in endpoints if (e.get('url') or '').rstrip('/') == url]
    if not matches:
        report.check(f'Webhook endpoint for {url}', False, 'none found', warn_only=not live)
        return
    if len(matches) > 1:
        report.warn(f'One webhook endpoint for {url}', f'{len(matches)} found; each has its own signing secret, '
                    'so all but one fail verification')
    endpoint = matches[0]
    report.check('Webhook endpoint enabled', endpoint.get('status') == 'enabled', endpoint.get('status'))
    events = set(endpoint.get('enabled_events') or [])
    missing = set() if '*' in events else WEBHOOK_EVENTS - events
    report.check('Webhook endpoint sends every handled event', not missing,
                 f"missing {', '.join(sorted(missing))}" if missing else f'{len(WEBHOOK_EVENTS)} events')
    report.note('Webhook API version', endpoint.get('api_version') or "the account's default")


def _check_portal(report, portal, base_url):
    if portal is None:
        report.fail('Customer portal settings saved', 'open Settings → Billing → Customer portal and click Save')
        return
    features = portal.get('features') or {}
    cancel = features.get('subscription_cancel') or {}
    report.check('Portal lets customers cancel at the end of the period',
                 cancel.get('enabled') and cancel.get('mode') == 'at_period_end',
                 f"enabled={cancel.get('enabled')}, mode={cancel.get('mode')}")
    report.check('Portal lets customers update their card',
                 (features.get('payment_method_update') or {}).get('enabled'))
    profile = portal.get('business_profile') or {}
    for field, path in (('privacy_policy_url', '/privacy'), ('terms_of_service_url', '/terms')):
        value = profile.get(field)
        report.check(f'Portal {field.replace("_", " ")}', (value or '').rstrip('/') == base_url + path,
                     value or 'unset', warn_only=True)


# --- Plaid ---

def plaid_probe(config):
    """Cheapest authenticated Plaid call (not billed): proves the keys work in this environment."""
    from plaid.model.country_code import CountryCode
    from plaid.model.institutions_get_request import InstitutionsGetRequest

    with _plaid_app_context(config):
        plaid_api._plaid().institutions_get(
            InstitutionsGetRequest(count=1, offset=0, country_codes=[CountryCode('US')]),
            _request_timeout=plaid_api.PLAID_TIMEOUT,
        )


def _plaid_app_context(config):
    from flask import Flask

    app = Flask(__name__)
    app.config.update(config)
    return app.app_context()


def check_plaid(report, config, mode, app=None, probe=plaid_probe):
    report.section('Plaid')
    expected = 'production' if mode == 'live' else 'sandbox'
    environment = config.get('PLAID_ENVIRONMENT')
    report.check('PLAID_ENVIRONMENT', environment == expected, f'{environment}; expected {expected}')
    if not (config.get('PLAID_CLIENT_ID') and config.get('PLAID_SECRET')):
        report.fail('PLAID_CLIENT_ID and PLAID_SECRET set', 'bank syncing stays hidden without them')
    elif environment in plaid_api.PLAID_ENVIRONMENTS:
        try:
            probe(config)
            report.ok(f'Plaid accepts the keys in {environment}')
        except plaid_api.PLAID_ERRORS as exc:
            report.fail(f'Plaid accepts the keys in {environment}', _plaid_error(exc))

    key = config.get('PLAID_TOKEN_KEY', '')
    if not key:
        report.fail('PLAID_TOKEN_KEY set', 'bank syncing stays hidden without it')
        return
    try:
        cipher = plaid_api.token_cipher(key)
    except Exception:
        report.fail('PLAID_TOKEN_KEY is a valid Fernet key (or NEW,OLD)')
        return
    report.ok('PLAID_TOKEN_KEY is a valid Fernet key', f"{len([k for k in key.split(',') if k.strip()])} key(s)")
    if app is not None:
        _check_stored_tokens(report, app, cipher)


def _check_stored_tokens(report, app, cipher):
    from models import PlaidItem

    with app.app_context():
        try:
            items = PlaidItem.query.all()
        except Exception as exc:
            report.warn('Stored bank tokens', f"couldn't read plaid_items: {type(exc).__name__}")
            return
        unreadable = 0
        for item in items:
            try:
                cipher.decrypt(item.access_token_encrypted.encode())
            except Exception:
                unreadable += 1
        report.check('Stored bank tokens decrypt with PLAID_TOKEN_KEY', not unreadable,
                     f'{len(items)} stored, {unreadable} unreadable' if items else 'none stored yet')


def _plaid_error(exc):
    import json

    body = getattr(exc, 'body', None)
    try:
        data = json.loads(body) if body else {}
    except (TypeError, ValueError):
        data = {}
    return data.get('error_code') or type(exc).__name__


# --- Google ---

def check_google(report, config, base_url=SITE):
    report.section('Google sign-in')
    client_id = config.get('GOOGLE_CLIENT_ID', '')
    secret = config.get('GOOGLE_CLIENT_SECRET', '')
    if not (client_id and secret):
        report.fail('GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET set', 'the Google button stays hidden without them')
        return
    report.check('Client ID format', client_id.endswith('.apps.googleusercontent.com'), client_id[:12] + '…')
    report.check('Client secret format', secret.startswith('GOCSPX-'), warn_only=True)
    report.note('Redirect URI the client must allow', f'{base_url}/auth/google/callback')
    report.note('Publishing status and branding', "not visible through an API; check the Google Auth Platform pages")


# --- Email ---

def resend_domains(api_key):
    return requests.get(RESEND_DOMAINS_URL, headers={'Authorization': f'Bearer {api_key}'}, timeout=10)


def check_email(report, config, mode, fetch=resend_domains):
    report.section('Email (Resend)')
    live = mode == 'live'
    key = config.get('RESEND_API_KEY', '')
    backend = config.get('EMAIL_BACKEND')
    report.check('Emails are sent through Resend', backend == 'resend', backend, warn_only=not live)
    if not key:
        return
    report.check('API key format', key.startswith('re_'), warn_only=True)
    sender = config.get('EMAIL_FROM') or ''
    domain = sender.rsplit('@', 1)[-1].strip(' >').lower()
    report.note('Sender', sender)
    try:
        response = fetch(key)
    except requests.RequestException as exc:
        report.warn('Sender domain verified', f"couldn't reach Resend: {type(exc).__name__}")
        return
    if response.status_code != 200:
        report.warn('Sender domain verified', f"can't check with this key (HTTP {response.status_code}; "
                    'sending-only keys can\'t list domains); check the Resend dashboard')
        return
    domains = {(d.get('name') or '').lower(): d.get('status') for d in response.json().get('data') or []}
    report.check(f'Sender domain {domain} verified', domains.get(domain) == 'verified', domains.get(domain) or 'not added')


# --- Public site ---

def check_public_site(report, base_url, accounts_enabled, get=requests.get):
    report.section(f'Public site ({base_url})')
    for path in PUBLIC_PAGES:
        try:
            status = get(base_url + path, timeout=15, allow_redirects=False).status_code
        except requests.RequestException as exc:
            report.fail(f'GET {path}', type(exc).__name__)
            continue
        report.check(f'GET {path}', status == 200, str(status))
    try:
        status = get(base_url + '/login', timeout=15, allow_redirects=False).status_code
        report.note('GET /login', f"{status}: {'accounts on' if status == 200 else 'accounts off' if status == 404 else 'unexpected'}"
                    f" on the running site; this shell has ACCOUNTS_ENABLED {'on' if accounts_enabled else 'off'}")
    except requests.RequestException:
        pass


# --- Main ---

def load_app(mode):
    from serve import create_app

    overrides = {}
    if mode == 'test':
        # Shell profiles may export real Plaid keys; the sandbox check must never use them.
        overrides = {
            'PLAID_CLIENT_ID': os.environ.get('PLAID_SANDBOX_CLIENT_ID', '').strip(),
            'PLAID_SECRET': os.environ.get('PLAID_SANDBOX_SECRET', '').strip(),
            'PLAID_ENVIRONMENT': 'sandbox',
        }
    return create_app(overrides)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--mode', choices=('live', 'test'), default='live',
                        help='live: Stripe live mode and Plaid production (default); test: test mode and sandbox')
    parser.add_argument('--base-url', help=f'the public site to check (default PUBLIC_BASE_URL, else {SITE})')
    args = parser.parse_args(argv)

    report = Report()
    try:
        app = load_app(args.mode)
    except Exception as exc:
        print(f"The app doesn't start with these settings: {exc}")
        return 1
    config = app.config
    base_url = (args.base_url or config.get('PUBLIC_BASE_URL') or SITE).rstrip('/')
    print(f'Go-live check, {args.mode} mode, site {base_url}')

    check_config(report, config, args.mode, os.environ)
    check_database(report, app, args.mode)
    check_stripe(report, config, args.mode, base_url=base_url)
    check_plaid(report, config, args.mode, app=app)
    check_google(report, config, base_url=base_url)
    check_email(report, config, args.mode)
    check_public_site(report, base_url, config['ACCOUNTS_ENABLED'])

    print(f'\n{len(report.failures)} failed, {len(report.warnings)} warnings')
    return 1 if report.failures else 0


if __name__ == '__main__':
    sys.exit(main())
