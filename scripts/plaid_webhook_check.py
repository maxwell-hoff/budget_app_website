"""Live check of POST /plaid/webhook with real, signed webhooks from Plaid's sandbox.

Plaid has to reach this computer, so start a tunnel first (any HTTPS tunnel works):

    cloudflared tunnel --url http://127.0.0.1:5001

and pass the https://….trycloudflare.com URL it prints:

    python scripts/plaid_webhook_check.py --public-url https://….trycloudflare.com

It runs the website on 127.0.0.1:5001 itself (quit any other server on that port), with a
throwaway SQLite database and PUBLIC_BASE_URL set to the tunnel. It links a sandbox bank
whose webhook points at the tunnel, then makes Plaid send webhooks (/sandbox/item/reset_login,
/sandbox/item/fire_webhook) and waits for the item's status to change. Every webhook is
verified with Plaid's real JWT and signing key. It also checks that a forged webhook sent
through the tunnel is rejected, and removes the sandbox bank at the end.

Needs PLAID_SANDBOX_CLIENT_ID and PLAID_SANDBOX_SECRET in .env.
"""
import argparse
import logging
import os
import sys
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path

import requests
from werkzeug.serving import make_server

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402,F401  (loads .env)
from cryptography.fernet import Fernet  # noqa: E402
from plaid.model.products import Products  # noqa: E402
from plaid.model.sandbox_item_fire_webhook_request import SandboxItemFireWebhookRequest  # noqa: E402
from plaid.model.sandbox_item_reset_login_request import SandboxItemResetLoginRequest  # noqa: E402
from plaid.model.sandbox_public_token_create_request import SandboxPublicTokenCreateRequest  # noqa: E402
from plaid.model.sandbox_public_token_create_request_options import (  # noqa: E402
    SandboxPublicTokenCreateRequestOptions,
)
from plaid.model.webhook_type import WebhookType  # noqa: E402

import plaid_api  # noqa: E402
from app_auth import create_app_session  # noqa: E402
from extensions import db  # noqa: E402
from models import PlaidItem, Subscription, User, utcnow  # noqa: E402
from serve import create_app  # noqa: E402

PORT = 5001


class WebhookLog(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        line = record.getMessage()
        self.lines.append(line)
        print(f'   server: {line}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--public-url', required=True, help='the tunnel URL that reaches 127.0.0.1:5001')
    parser.add_argument('--timeout', type=int, default=90, help='seconds to wait for each webhook')
    args = parser.parse_args()
    public_url = args.public_url.rstrip('/')
    client_id = os.environ.get('PLAID_SANDBOX_CLIENT_ID', '').strip()
    secret = os.environ.get('PLAID_SANDBOX_SECRET', '').strip()
    if not (client_id and secret):
        sys.exit('Set PLAID_SANDBOX_CLIENT_ID and PLAID_SANDBOX_SECRET in .env.')

    tmp = tempfile.mkdtemp()
    app = create_app({
        'SQLALCHEMY_DATABASE_URI': f'sqlite:///{tmp}/webhook_check.db',
        'ACCOUNTS_ENABLED': True,
        'PLAID_CLIENT_ID': client_id, 'PLAID_SECRET': secret, 'PLAID_ENVIRONMENT': 'sandbox',
        'PLAID_TOKEN_KEY': Fernet.generate_key().decode(),
        'PUBLIC_BASE_URL': public_url,
        'EMAIL_BACKEND': 'memory',
    })
    log = WebhookLog()
    webhook_logger = logging.getLogger('plaid_webhook')
    webhook_logger.addHandler(log)
    webhook_logger.setLevel(logging.INFO)

    server = make_server('127.0.0.1', PORT, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    failures = []

    def check(label, ok):
        print(f"{'PASS' if ok else 'FAIL'}: {label}", flush=True)
        if not ok:
            failures.append(label)

    with app.app_context():
        db.create_all()
        user = User(email='webhook-check@example.com', email_verified_at=utcnow())
        user.set_password('correct horse battery')
        db.session.add(user)
        db.session.add(Subscription(user=user, stripe_customer_id='cus_check', status='active',
                                    current_period_start=utcnow(), current_period_end=utcnow() + timedelta(days=30)))
        db.session.commit()
        token, _ = create_app_session(user, 'plaid_webhook_check.py')
        headers = {'Authorization': f'Bearer {token}'}
        webhook = plaid_api.webhook_url()
        plaid = plaid_api._plaid()
    print(f'Webhook URL: {webhook}', flush=True)

    try:
        health = requests.get(f'{public_url}/healthz', timeout=30)
        check('the tunnel reaches the server', health.status_code == 200)
        if failures:
            return 1

        forged = requests.post(webhook, data=b'{"webhook_type": "ITEM", "webhook_code": "ERROR"}',
                               headers={'Plaid-Verification': 'e30.e30.e30', 'Content-Type': 'application/json'},
                               timeout=30)
        check('a forged webhook is rejected (400)', forged.status_code == 400)

        public_token = plaid.sandbox_public_token_create(SandboxPublicTokenCreateRequest(
            institution_id='ins_109508', initial_products=[Products('transactions')],
            options=SandboxPublicTokenCreateRequestOptions(webhook=webhook),
        )).public_token
        client = app.test_client()
        resp = client.post('/v1/plaid/exchange', headers=headers, json={
            'public_token': public_token, 'institution': {'id': 'ins_109508', 'name': 'First Platypus Bank'},
        })
        check('linked a sandbox bank', resp.status_code == 200)
        item_id = resp.get_json()['item']['item_id']
        with app.app_context():
            access_token = plaid_api.decrypt_token(
                db.session.scalar(db.select(PlaidItem).filter_by(item_id=item_id)).access_token_encrypted)

        def wait_for(expected, label):
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                with app.app_context():
                    current = db.session.scalar(db.select(PlaidItem.status).filter_by(item_id=item_id))
                if current == expected:
                    break
                time.sleep(1)
            check(f'{label}: status is {expected!r}', current == expected)

        def fire(code):
            print(f'Firing ITEM {code}…', flush=True)
            try:
                plaid.sandbox_item_fire_webhook(SandboxItemFireWebhookRequest(
                    access_token=access_token, webhook_type=WebhookType('ITEM'), webhook_code=code,
                ))
                return True
            except plaid_api.PLAID_ERRORS as exc:
                print(f'   Plaid refused to fire {code}: {plaid_api.plaid_error_code(exc)} (skipped)', flush=True)
                return False

        # The sandbox only fires webhooks for a working Item, so the login reset comes last.
        if fire('PENDING_DISCONNECT'):
            wait_for('relink_recommended', 'ITEM PENDING_DISCONNECT')
        if fire('LOGIN_REPAIRED'):
            wait_for('ok', 'ITEM LOGIN_REPAIRED')
        if fire('USER_PERMISSION_REVOKED'):
            wait_for('error', 'ITEM USER_PERMISSION_REVOKED')
        print('Resetting the sandbox login (Plaid sends ITEM ERROR ITEM_LOGIN_REQUIRED)…', flush=True)
        plaid.sandbox_item_reset_login(SandboxItemResetLoginRequest(access_token=access_token))
        wait_for('relink_required', 'ITEM ERROR')

        accepted = [line for line in log.lines if line.startswith('Plaid ITEM') or line.startswith('Plaid TRANSACTIONS')]
        check(f'{len(accepted)} signed webhook(s) verified, none rejected besides the forged one',
              bool(accepted) and sum('Rejected' in line for line in log.lines) == 1)

        resp = client.delete(f'/v1/plaid/items/{item_id}', headers=headers)
        check('removed the sandbox bank', resp.status_code == 204)
    finally:
        server.shutdown()
    print('OK' if not failures else f'{len(failures)} check(s) failed', flush=True)
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
