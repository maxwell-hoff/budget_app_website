"""Live check of the desktop app's server API, run against a local website server.

Plays the desktop app's part: listens on http://127.0.0.1:5002/auth/callback (quit the
desktop app first), opens your browser to /app-login, and when you click Continue it
trades the code for a session token and calls GET /v1/me. With --plaid it then links
Plaid's sandbox bank (First Platypus Bank) through POST /v1/plaid/link-token and
/v1/plaid/exchange, syncs it with POST /v1/plaid/sync (retrying while it's 503
plaid_not_ready), lists items, and deletes the item again (add --keep to leave it).
--save-sync PATH writes the sync response to a file, for
scripts/check_sync_fixture_with_desktop.py.

The server must run with ACCOUNTS_ENABLED=true and, for --plaid, sandbox Plaid keys.
--plaid needs the signed-in account to have an active (test-mode) subscription, and
PLAID_SANDBOX_CLIENT_ID / PLAID_SANDBOX_SECRET in .env to fake the Link step.

Run from the repo root:
    python scripts/desktop_flow_check.py [--plaid] [--keep] [--save-sync PATH] [--server http://127.0.0.1:5001]
"""
import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402,F401  (loads .env)

CALLBACK_PORT = 5002
REDIRECT_URI = f'http://127.0.0.1:{CALLBACK_PORT}/auth/callback'


def wait_for_callback():
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parts = urlsplit(self.path)
            if parts.path != '/auth/callback':
                self.send_response(404)
                self.end_headers()
                return
            result.update({k: v[0] for k, v in parse_qs(parts.query).items()})
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(b'Signed in. You can close this tab and go back to the terminal.')

        def log_message(self, *args):
            pass

    server = HTTPServer(('127.0.0.1', CALLBACK_PORT), Handler)
    while not result:
        server.handle_request()
    server.server_close()
    return result


def show(label, resp):
    print(f'{label}: HTTP {resp.status_code}')
    if resp.content:
        body = resp.json()
        if 'session_token' in body:
            body = {**body, 'session_token': body['session_token'][:6] + '…'}
        print('  ', body)
    return resp


def sandbox_public_token():
    client_id, secret = os.environ.get('PLAID_SANDBOX_CLIENT_ID'), os.environ.get('PLAID_SANDBOX_SECRET')
    if not (client_id and secret):
        sys.exit('Set PLAID_SANDBOX_CLIENT_ID and PLAID_SANDBOX_SECRET in .env for --plaid.')
    resp = requests.post('https://sandbox.plaid.com/sandbox/public_token/create', json={
        'client_id': client_id, 'secret': secret,
        'institution_id': 'ins_109508', 'initial_products': ['transactions'],
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()['public_token']


def sync(server, headers, item_id, save_to=None):
    deadline = time.monotonic() + 60
    while True:
        resp = requests.post(f'{server}/v1/plaid/sync', headers=headers, json={'item_id': item_id}, timeout=150)
        if resp.status_code != 503 or time.monotonic() > deadline:
            break
        wait = int(resp.headers.get('Retry-After', '5'))
        print(f"POST /v1/plaid/sync: HTTP 503 {resp.json()['error']['code']}, retrying in {wait} s", flush=True)
        time.sleep(wait)
    if resp.status_code != 200:
        return show('POST /v1/plaid/sync', resp)
    print('POST /v1/plaid/sync: HTTP 200')
    for entry in resp.json()['items']:
        print(f"   {entry['item']['institution_name']}: {len(entry['accounts'])} accounts, "
              f"{len(entry['transactions'])} transactions, last_synced_at={entry['item']['last_synced_at']}, "
              f"error={entry['error']}")
    if save_to:
        Path(save_to).write_text(json.dumps(resp.json(), indent=2) + '\n')
        print(f'   saved to {save_to}')
    return resp


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--server', default='http://127.0.0.1:5001')
    parser.add_argument('--plaid', action='store_true', help='also link, list, and delete a sandbox bank')
    parser.add_argument('--keep', action='store_true', help='with --plaid, leave the linked item in place')
    parser.add_argument('--save-sync', metavar='PATH', help='with --plaid, write the sync response to PATH')
    parser.add_argument('--no-browser', action='store_true', help='print the sign-in URL instead of opening it')
    args = parser.parse_args()
    server = args.server.rstrip('/')

    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    state = secrets.token_urlsafe(16)
    login_url = f'{server}/app-login?' + urlencode({
        'redirect_uri': REDIRECT_URI, 'code_challenge': challenge, 'code_challenge_method': 'S256',
        'state': state, 'device_name': 'desktop_flow_check.py',
    })
    print(f'Sign in at {login_url}\nLog in if asked, then click Continue.', flush=True)
    if not args.no_browser:
        webbrowser.open(login_url)

    callback = wait_for_callback()
    if callback.get('state') != state:
        sys.exit(f'State mismatch: {callback}')
    if 'error' in callback:
        sys.exit(f"Sign-in canceled: {callback['error']}")

    resp = show('POST /v1/auth/token', requests.post(f'{server}/v1/auth/token', json={
        'code': callback['code'], 'code_verifier': verifier,
    }, timeout=30))
    if resp.status_code != 200:
        sys.exit(1)
    headers = {'Authorization': f"Bearer {resp.json()['session_token']}"}
    me = show('GET /v1/me', requests.get(f'{server}/v1/me', headers=headers, timeout=30)).json()

    if args.plaid:
        if not me.get('plaid_access'):
            print('Note: this account has no Plaid access, so expect 402s. Subscribe on /account first.')
        show('POST /v1/plaid/link-token', requests.post(f'{server}/v1/plaid/link-token', headers=headers, timeout=60))
        resp = show('POST /v1/plaid/exchange', requests.post(f'{server}/v1/plaid/exchange', headers=headers, json={
            'public_token': sandbox_public_token(),
            'institution': {'id': 'ins_109508', 'name': 'First Platypus Bank'},
        }, timeout=60))
        item_id = resp.json()['item']['item_id'] if resp.status_code == 200 else None
        if item_id:
            sync(server, headers, item_id, args.save_sync)
        show('GET /v1/plaid/items', requests.get(f'{server}/v1/plaid/items', headers=headers, timeout=30))
        if item_id and not args.keep:
            show(f'DELETE /v1/plaid/items/{item_id}',
                 requests.delete(f'{server}/v1/plaid/items/{item_id}', headers=headers, timeout=60))

    show('POST /v1/auth/logout', requests.post(f'{server}/v1/auth/logout', headers=headers, timeout=30))
    show('GET /v1/me after logout', requests.get(f'{server}/v1/me', headers=headers, timeout=30))


if __name__ == '__main__':
    main()
