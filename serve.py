import os
import secrets
from datetime import datetime
from pathlib import Path

from flask import Flask, current_app, g, render_template, request, jsonify, send_from_directory, abort
from sqlalchemy import text
from werkzeug.middleware.proxy_fix import ProxyFix

import account
import analytics
import api
import app_auth
import auth
import billing
import google_auth
import models  # noqa: F401  (registers tables with SQLAlchemy for migrations)
import plaid_api
import plaid_webhook
import security_log
from config import BASE_DIR, load_config
from extensions import db, limiter, login_manager, migrate

DOWNLOADS_DIR = Path(__file__).resolve().parent / 'frontend' / 'static' / 'downloads'

PLATFORM_FOLDERS = {
    'mac-arm': 'mac-arm',
    'mac-x64': 'mac-x64',
    'windows': 'windows',
}

LEGAL_CONTACT_EMAIL = 'max@gardenstudiosoftware.com'
# Change whenever the wording of a legal page changes.
LEGAL_UPDATED = 'October 9, 2026'


def index():
    return render_template('index.html')


def about():
    return render_template('about.html')


def render_legal(page):
    return render_template(
        f'legal/{page}.html', legal_contact_email=LEGAL_CONTACT_EMAIL, legal_updated=LEGAL_UPDATED,
    )


def privacy():
    return render_legal('privacy')


def terms():
    return render_legal('terms')


def refunds():
    return render_legal('refunds')


def download(platform):
    folder_name = PLATFORM_FOLDERS.get(platform)
    if not folder_name:
        abort(404)

    folder = DOWNLOADS_DIR / folder_name
    if not folder.is_dir():
        abort(404)

    files = [f for f in folder.iterdir() if f.is_file() and not f.name.startswith('.')]
    if not files:
        abort(404)

    target = files[0]
    return send_from_directory(folder, target.name, as_attachment=True)


def notify():
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip()
    if not email or '@' not in email:
        return jsonify({'ok': False, 'error': 'invalid email'}), 400

    # Not stored anywhere — just surfaced in the dev logs to copy manually.
    timestamp = datetime.now().isoformat(timespec='seconds')
    print(f"\n=== NOTIFY SIGNUP === {timestamp} === {email} ===\n", flush=True)

    return jsonify({'ok': True})


def healthz():
    try:
        db.session.execute(text('SELECT 1'))
    except Exception:
        return jsonify({'status': 'error', 'database': 'unreachable'}), 503
    return jsonify({'status': 'ok'})


def csp_nonce():
    """Per-response nonce for the templates' inline <script> blocks."""
    if 'csp_nonce' not in g:
        g.csp_nonce = secrets.token_urlsafe(16)
    return g.csp_nonce


def content_security_policy(nonce):
    script_src = f"'self' 'nonce-{nonce}'" if nonce else "'self'"
    # Styles allow 'unsafe-inline' because the landing page uses many style="" attributes,
    # which nonces can't cover. form-action is left out: Chrome applies it to redirects,
    # and forms here redirect to Stripe and to the desktop app's loopback address.
    return '; '.join([
        "default-src 'self'",
        f'script-src {script_src}',
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com",
        "img-src 'self' data:",
        "media-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "frame-ancestors 'none'",
    ])


def security_headers(response):
    headers = response.headers
    headers.setdefault('X-Content-Type-Options', 'nosniff')
    headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    # Nobody may frame these pages, so /app-login's Continue button can't be clickjacked.
    headers.setdefault('X-Frame-Options', 'DENY')
    headers.setdefault('Content-Security-Policy', content_security_policy(g.get('csp_nonce')))
    # Only over HTTPS (Render): browsers would otherwise refuse plain-HTTP local dev.
    if current_app.config.get('SESSION_COOKIE_SECURE'):
        headers.setdefault('Strict-Transport-Security', 'max-age=31536000')
    return response


def create_app(test_config=None):
    app = Flask(__name__, template_folder='frontend/templates', static_folder='frontend/static')
    # Render terminates TLS in one proxy hop; this gives rate limiting the real client IP.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
    os.makedirs(app.instance_path, exist_ok=True)

    app.config.update(load_config(app.instance_path))
    if test_config:
        app.config.update(test_config)

    security_log.init_logging()
    app.after_request(security_headers)
    app.jinja_env.globals['csp_nonce'] = csp_nonce

    db.init_app(app)
    migrate.init_app(app, db, directory=str(BASE_DIR / 'migrations'))
    login_manager.init_app(app)
    limiter.init_app(app)
    google_auth.init_google(app)
    billing.init_billing(app)
    plaid_api.init_plaid(app)
    analytics.init_analytics(app)

    login_manager.login_view = 'auth.login' if app.config['ACCOUNTS_ENABLED'] else None
    if app.config['ACCOUNTS_ENABLED']:
        app.register_blueprint(auth.bp)
        app.register_blueprint(account.bp)
        app.register_blueprint(google_auth.bp)
        app.register_blueprint(app_auth.bp)
        app.register_blueprint(api.bp)
        if app.config['STRIPE_ENABLED']:
            app.register_blueprint(billing.bp)
        if app.config['PLAID_ENABLED']:
            app.register_blueprint(plaid_api.bp)
            app.register_blueprint(plaid_webhook.bp)

    app.add_url_rule('/', view_func=index)
    app.add_url_rule('/about', view_func=about)
    app.add_url_rule('/privacy', view_func=privacy)
    app.add_url_rule('/terms', view_func=terms)
    app.add_url_rule('/refunds', view_func=refunds)
    app.add_url_rule('/download/<platform>', view_func=download)
    app.add_url_rule('/notify', view_func=notify, methods=['POST'])
    app.add_url_rule('/healthz', view_func=healthz)

    return app


app = create_app()


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(prog="serve.py", add_help=True)
    parser.add_argument("--debug", action="store_true", help="Print cumulative step timings for slow UI actions")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--no-flask-debug", action="store_true", help="Disable Flask debug mode")
    args = parser.parse_args()
    app.run(debug=(not args.no_flask_debug), host=args.host, port=int(args.port))
