import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import plaid
import pytest
from cryptography.fernet import Fernet

from conftest import make_app
from extensions import db
from models import PlaidItem, User

SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'go_live_check.py'
spec = importlib.util.spec_from_file_location('go_live_check', SCRIPT)
glc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(glc)

SITE = 'https://workbenchbudgeting.com'
ALL_EVENTS = sorted(glc.WEBHOOK_EVENTS)


def good_stripe_objects():
    return {
        'account': {'charges_enabled': True, 'payouts_enabled': True,
                    'business_profile': {'url': 'workbenchbudgeting.com'}},
        'price': {
            'active': True, 'unit_amount': 899, 'currency': 'usd', 'livemode': True,
            'recurring': {'interval': 'month', 'interval_count': 1},
            'product': {'active': True, 'name': 'Workbench Budgeting', 'tax_code': 'txcd_10103000'},
        },
        'endpoints': {'data': [{'url': f'{SITE}/stripe/webhook', 'status': 'enabled',
                                'enabled_events': ALL_EVENTS, 'api_version': None}]},
        'portals': {'data': [{
            'features': {'subscription_cancel': {'enabled': True, 'mode': 'at_period_end'},
                         'payment_method_update': {'enabled': True}},
            'business_profile': {'privacy_policy_url': f'{SITE}/privacy', 'terms_of_service_url': f'{SITE}/terms'},
        }]},
    }


def fake_stripe(objects):
    def returns(name):
        def call(*args, **kwargs):
            value = objects[name]
            if isinstance(value, Exception):
                raise value
            return value
        return call

    return SimpleNamespace(v1=SimpleNamespace(
        accounts=SimpleNamespace(retrieve_current=returns('account')),
        prices=SimpleNamespace(retrieve=returns('price')),
        webhook_endpoints=SimpleNamespace(list=returns('endpoints')),
        billing_portal=SimpleNamespace(configurations=SimpleNamespace(list=returns('portals'))),
    ))


STRIPE_CONFIG = {'STRIPE_SECRET_KEY': 'sk_live_abc', 'STRIPE_PRICE_ID': 'price_1', 'STRIPE_WEBHOOK_SECRET': 'whsec_1'}


def run_stripe(objects, mode='live', config=STRIPE_CONFIG):
    report = glc.Report()
    glc.check_stripe(report, config, mode, client=fake_stripe(objects), base_url=SITE)
    return report


def test_webhook_events_are_the_ones_billing_handles():
    assert 'customer.subscription.trial_will_end' in glc.WEBHOOK_EVENTS
    assert len(glc.WEBHOOK_EVENTS) == 7


def test_stripe_all_good():
    report = run_stripe(good_stripe_objects())
    assert report.failures == []
    assert report.warnings == []


def test_stripe_test_key_in_live_mode_fails():
    report = run_stripe(good_stripe_objects(), config={**STRIPE_CONFIG, 'STRIPE_SECRET_KEY': 'sk_test_abc'})
    assert 'Secret key is a live-mode key' in report.failures


def test_stripe_missing_settings_fail():
    report = run_stripe(good_stripe_objects(), config={**STRIPE_CONFIG, 'STRIPE_WEBHOOK_SECRET': ''})
    assert any('all set' in label for label in report.failures)


def test_stripe_webhook_missing_an_event_fails(capsys):
    objects = good_stripe_objects()
    objects['endpoints']['data'][0]['enabled_events'] = [
        e for e in ALL_EVENTS if e != 'customer.subscription.trial_will_end']
    report = run_stripe(objects)
    assert report.failures == ['Webhook endpoint sends every handled event']
    assert 'missing customer.subscription.trial_will_end' in capsys.readouterr().out


def test_stripe_webhook_wildcard_counts_as_every_event():
    objects = good_stripe_objects()
    objects['endpoints']['data'][0]['enabled_events'] = ['*']
    assert run_stripe(objects).failures == []


def test_stripe_webhook_endpoint_missing_fails_live_and_warns_in_test():
    objects = good_stripe_objects()
    objects['endpoints'] = {'data': [{'url': 'https://example.com/stripe/webhook'}]}
    assert run_stripe(objects).failures == [f'Webhook endpoint for {SITE}/stripe/webhook']
    objects['price']['livemode'] = False
    test_report = run_stripe(objects, mode='test', config={**STRIPE_CONFIG, 'STRIPE_SECRET_KEY': 'sk_test_abc'})
    assert test_report.failures == []
    assert f'Webhook endpoint for {SITE}/stripe/webhook' in test_report.warnings


def test_stripe_duplicate_webhook_endpoints_warn():
    objects = good_stripe_objects()
    objects['endpoints']['data'].append(dict(objects['endpoints']['data'][0]))
    report = run_stripe(objects)
    assert report.failures == []
    assert any(label.startswith('One webhook endpoint') for label in report.warnings)


def test_stripe_wrong_price_fails():
    objects = good_stripe_objects()
    objects['price'].update(unit_amount=999, livemode=False)
    report = run_stripe(objects)
    assert 'Price is $8.99 USD a month' in report.failures
    assert 'Price is in the same mode as the key' in report.failures


def test_stripe_yearly_price_fails():
    objects = good_stripe_objects()
    objects['price']['recurring'] = {'interval': 'year', 'interval_count': 1}
    assert 'Price is $8.99 USD a month' in run_stripe(objects).failures


def test_stripe_product_name_and_tax_code_only_warn():
    objects = good_stripe_objects()
    objects['price']['product'].update(name='Workbench Bank Sync', tax_code=None)
    report = run_stripe(objects)
    assert report.failures == []
    assert {'Product name', 'Product tax code (Managed Payments)'} <= set(report.warnings)


def test_stripe_portal_not_saved_fails():
    objects = good_stripe_objects()
    objects['portals'] = {'data': []}
    assert run_stripe(objects).failures == ['Customer portal settings saved']


def test_stripe_portal_cancel_immediately_fails():
    objects = good_stripe_objects()
    objects['portals']['data'][0]['features']['subscription_cancel']['mode'] = 'immediately'
    assert run_stripe(objects).failures == ['Portal lets customers cancel at the end of the period']


def test_stripe_api_error_is_reported_not_raised():
    import stripe

    objects = good_stripe_objects()
    objects['price'] = stripe.InvalidRequestError('No such price', 'price')
    report = run_stripe(objects)
    assert report.failures == ['Price']


# --- Plaid ---

def plaid_config(**overrides):
    return {'PLAID_ENVIRONMENT': 'production', 'PLAID_CLIENT_ID': 'id', 'PLAID_SECRET': 'secret',
            'PLAID_TOKEN_KEY': Fernet.generate_key().decode(), **overrides}


def test_plaid_all_good():
    report = glc.Report()
    glc.check_plaid(report, plaid_config(), 'live', probe=lambda config: None)
    assert report.failures == []


def test_plaid_sandbox_in_live_mode_fails():
    report = glc.Report()
    glc.check_plaid(report, plaid_config(PLAID_ENVIRONMENT='sandbox'), 'live', probe=lambda config: None)
    assert report.failures == ['PLAID_ENVIRONMENT']


def test_plaid_rejected_keys_fail_with_plaids_code(capsys):
    def probe(config):
        error = plaid.ApiException(status=400)
        error.body = json.dumps({'error_code': 'INVALID_API_KEYS'})
        raise error

    report = glc.Report()
    glc.check_plaid(report, plaid_config(), 'live', probe=probe)
    assert report.failures == ['Plaid accepts the keys in production']
    assert 'INVALID_API_KEYS' in capsys.readouterr().out


def test_plaid_bad_token_key_fails():
    report = glc.Report()
    glc.check_plaid(report, plaid_config(PLAID_TOKEN_KEY='not-a-key'), 'live', probe=lambda config: None)
    assert report.failures == ['PLAID_TOKEN_KEY is a valid Fernet key (or NEW,OLD)']


def test_plaid_stored_tokens_must_decrypt():
    app = make_app()
    key, other = Fernet.generate_key(), Fernet.generate_key()
    with app.app_context():
        user = User(email='a@example.com')
        db.session.add(user)
        db.session.flush()
        for n, k in enumerate((key, other)):
            db.session.add(PlaidItem(user_id=user.id, item_id=f'item-{n}',
                                     access_token_encrypted=Fernet(k).encrypt(b'access-token').decode()))
        db.session.commit()

    report = glc.Report()
    glc.check_plaid(report, plaid_config(PLAID_TOKEN_KEY=key.decode()), 'live', app=app, probe=lambda config: None)
    assert report.failures == ['Stored bank tokens decrypt with PLAID_TOKEN_KEY']

    report = glc.Report()
    rotated = f'{Fernet.generate_key().decode()},{key.decode()},{other.decode()}'
    glc.check_plaid(report, plaid_config(PLAID_TOKEN_KEY=rotated), 'live', app=app, probe=lambda config: None)
    assert report.failures == []


def test_plaid_probe_uses_the_configured_environment(monkeypatch):
    seen = {}

    class FakeApi:
        def institutions_get(self, request, _request_timeout):
            seen['count'] = request.count

    def fake_plaid():
        from flask import current_app
        seen['environment'] = current_app.config['PLAID_ENVIRONMENT']
        return FakeApi()

    monkeypatch.setattr(glc.plaid_api, '_plaid', fake_plaid)
    glc.plaid_probe(plaid_config())
    assert seen == {'environment': 'production', 'count': 1}


# --- Config, database, Google, email, site ---

def test_config_live_requirements():
    report = glc.Report()
    config = {'ACCOUNTS_ENABLED': False, 'PUBLIC_BASE_URL': SITE, 'SESSION_COOKIE_SECURE': True, 'TRIAL_DAYS': 7}
    glc.check_config(report, config, 'live', {'SECRET_KEY': 'x'})
    assert report.failures == [] and report.warnings == []

    report = glc.Report()
    glc.check_config(report, {**config, 'SESSION_COOKIE_SECURE': False, 'TRIAL_DAYS': 'seven'}, 'live', {})
    assert set(report.failures) == {'SECRET_KEY is set', 'Secure session cookies', 'TRIAL_DAYS'}


def test_config_trial_days_zero_only_warns():
    report = glc.Report()
    config = {'ACCOUNTS_ENABLED': True, 'PUBLIC_BASE_URL': SITE, 'SESSION_COOKIE_SECURE': True, 'TRIAL_DAYS': 0}
    glc.check_config(report, config, 'live', {'SECRET_KEY': 'x'})
    assert report.failures == [] and report.warnings == ['TRIAL_DAYS']


def test_database_without_migrations_fails_in_live_mode():
    report = glc.Report()
    glc.check_database(report, make_app(), 'live')
    assert set(report.failures) == {'Database is Postgres', 'Migrations up to date'}


def test_google():
    report = glc.Report()
    glc.check_google(report, {'GOOGLE_CLIENT_ID': '1-abc.apps.googleusercontent.com',
                              'GOOGLE_CLIENT_SECRET': 'GOCSPX-x'}, base_url=SITE)
    assert report.failures == [] and report.warnings == []

    report = glc.Report()
    glc.check_google(report, {'GOOGLE_CLIENT_ID': '', 'GOOGLE_CLIENT_SECRET': ''})
    assert report.failures == ['GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET set']


def resend_response(status, domains=()):
    return SimpleNamespace(status_code=status, json=lambda: {'data': list(domains)})


EMAIL_CONFIG = {'EMAIL_BACKEND': 'resend', 'RESEND_API_KEY': 're_123',
                'EMAIL_FROM': 'Workbench Budgeting <noreply@workbenchbudgeting.com>'}


@pytest.mark.parametrize('status, expect_failures', [('verified', []), ('pending', ['Sender domain workbenchbudgeting.com verified'])])
def test_email_domain_status(status, expect_failures):
    report = glc.Report()
    fetch = lambda key: resend_response(200, [{'name': 'workbenchbudgeting.com', 'status': status}])  # noqa: E731
    glc.check_email(report, EMAIL_CONFIG, 'live', fetch=fetch)
    assert report.failures == expect_failures


def test_email_sending_only_key_warns():
    report = glc.Report()
    glc.check_email(report, EMAIL_CONFIG, 'live', fetch=lambda key: resend_response(401))
    assert report.failures == [] and report.warnings == ['Sender domain verified']


def test_email_console_backend_fails_live():
    report = glc.Report()
    glc.check_email(report, {'EMAIL_BACKEND': 'console', 'RESEND_API_KEY': ''}, 'live')
    assert report.failures == ['Emails are sent through Resend']


def test_public_site():
    statuses = {'/healthz': 200, '/privacy': 200, '/terms': 500, '/refunds': 200, '/login': 404}

    def get(url, **kwargs):
        return SimpleNamespace(status_code=statuses[url.removeprefix(SITE)])

    report = glc.Report()
    glc.check_public_site(report, SITE, False, get=get)
    assert report.failures == ['GET /terms']
