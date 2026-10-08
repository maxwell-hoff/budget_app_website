import base64
import hashlib
import hmac
import re
import secrets
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

from flask import Blueprint, abort, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm

from auth import LogoutForm, ResendVerificationForm, message_page
from extensions import db, limiter
from models import AppSession, AuthCode, as_utc, utcnow
from security_log import log_event

AUTH_CODE_TTL = timedelta(minutes=5)
APP_SESSION_TTL = timedelta(days=180)
LOOPBACK_HOSTS = ('127.0.0.1', 'localhost')
CALLBACK_PATH = '/auth/callback'
MAX_STATE_LENGTH = 256
MAX_DEVICE_NAME_LENGTH = 100
# BASE64URL(SHA256(verifier)) without padding is always 43 characters.
_CODE_CHALLENGE = re.compile(r'[A-Za-z0-9_-]{43}')
# RFC 7636: 43-128 characters from the unreserved set.
_CODE_VERIFIER = re.compile(r'[A-Za-z0-9._~-]{43,128}')

# Only registered when ACCOUNTS_ENABLED is on (see create_app), so every route 404s otherwise.
bp = Blueprint('app_auth', __name__)


class AppLoginForm(FlaskForm):
    pass


def hash_secret(value):
    return hashlib.sha256(value.encode()).hexdigest()


def pkce_challenge(verifier):
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b'=').decode()


def is_loopback_redirect_uri(uri):
    """Only http://127.0.0.1:<port>/auth/callback or http://localhost:<port>/auth/callback."""
    try:
        parts = urlsplit(uri)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == 'http'
        and parts.hostname in LOOPBACK_HOSTS
        and port  # None when missing; 0 isn't a real port
        # Rules out user info (user@host), uppercase hosts, and other spellings.
        and parts.netloc == f'{parts.hostname}:{port}'
        and parts.path == CALLBACK_PATH
        and not parts.query
        and not parts.fragment
    )


def clean_device_name(value):
    name = ' '.join((value or '').split())
    return name[:MAX_DEVICE_NAME_LENGTH] or None


def _login_request():
    """The desktop app's /app-login parameters, or None if any are missing or invalid."""
    args = request.args
    redirect_uri = args.get('redirect_uri', '')
    state = args.get('state', '')
    challenge = args.get('code_challenge', '')
    if not is_loopback_redirect_uri(redirect_uri):
        return None
    if not state or len(state) > MAX_STATE_LENGTH:
        return None
    if args.get('code_challenge_method') != 'S256' or not _CODE_CHALLENGE.fullmatch(challenge):
        return None
    return {
        'redirect_uri': redirect_uri,
        'state': state,
        'code_challenge': challenge,
        'device_name': clean_device_name(args.get('device_name')),
    }


def _app_callback_url(params, **values):
    return f"{params['redirect_uri']}?{urlencode({**values, 'state': params['state']})}"


def issue_auth_code(user, params):
    now = utcnow()
    # Codes live five minutes; clear out old ones as new ones are made.
    db.session.execute(db.delete(AuthCode).where(AuthCode.expires_at < now - timedelta(days=1)))
    code = secrets.token_urlsafe(32)
    db.session.add(AuthCode(
        user=user,
        code_hash=hash_secret(code),
        code_challenge=params['code_challenge'],
        redirect_uri=params['redirect_uri'],
        device_name=params['device_name'],
        created_at=now,
        expires_at=now + AUTH_CODE_TTL,
    ))
    db.session.commit()
    return code


def redeem_auth_code(code, verifier):
    """The code's user if the code is unused, unexpired, and matches the PKCE verifier;
    otherwise None. A code is used up by its first redemption attempt, right or wrong."""
    if not code or not verifier or not _CODE_VERIFIER.fullmatch(verifier):
        return None
    row = db.session.scalar(db.select(AuthCode).filter_by(code_hash=hash_secret(code)))
    now = utcnow()
    if row is None or row.used_at is not None or now >= as_utc(row.expires_at):
        return None
    claimed = db.session.execute(
        db.update(AuthCode).where(AuthCode.id == row.id, AuthCode.used_at.is_(None)).values(used_at=now)
    ).rowcount
    db.session.commit()
    if not claimed or not hmac.compare_digest(pkce_challenge(verifier), row.code_challenge):
        return None
    return row


def create_app_session(user, device_name):
    """Returns (token, session). Only the token's hash is stored."""
    now = utcnow()
    token = secrets.token_urlsafe(32)
    session = AppSession(
        user=user,
        token_hash=hash_secret(token),
        device_name=device_name,
        password_fingerprint=user.password_fingerprint(),
        created_at=now,
        last_used_at=now,
        expires_at=now + APP_SESSION_TTL,
    )
    db.session.add(session)
    db.session.commit()
    return token, session


def find_app_session(token):
    """The live session for a bearer token, or None if unknown, revoked, expired, or its
    user's password has changed since it was created."""
    if not token:
        return None
    session = db.session.scalar(db.select(AppSession).filter_by(token_hash=hash_secret(token)))
    if session is None or session.revoked_at is not None or utcnow() >= as_utc(session.expires_at):
        return None
    if not hmac.compare_digest(session.password_fingerprint, session.user.password_fingerprint()):
        return None
    return session


def _invalid_request():
    return message_page(
        'Sign-in link invalid', "This sign-in link isn't valid",
        'Start signing in again from the Workbench Budgeting desktop app.', status=400,
    )


@bp.route('/app-login', methods=['GET', 'POST'])
@login_required
@limiter.limit('30 per hour', methods=['POST'])
def app_login():
    params = _login_request()
    if params is None:
        # Never redirect to an unchecked URI, so errors are shown here instead.
        return _invalid_request()

    switch_account_url = url_for('auth.logout', next=request.full_path)
    cancel_url = _app_callback_url(params, error='access_denied')
    user = current_user._get_current_object()
    # Plaid Link is only reachable from a desktop session, so this also keeps unverified
    # addresses from linking banks.
    if not user.email_verified_at:
        if request.method == 'POST':
            log_event('app_code_refused', user.id, reason='email_unverified')
        return render_template(
            'auth/app_login_unverified.html',
            resend_form=ResendVerificationForm(),
            resend_url=url_for('auth.resend_verification', next=request.full_path),
            logout_form=LogoutForm(),
            switch_account_url=switch_account_url,
            cancel_url=cancel_url,
        ), 403

    form = AppLoginForm()
    if request.method == 'POST':
        if not form.validate_on_submit():
            abort(400)
        code = issue_auth_code(user, params)
        log_event('app_code_issued', user.id)
        return redirect(_app_callback_url(params, code=code))

    # A confirmation step (RFC 8252 section 8.6): another program on the computer could
    # start this flow, so a code is never issued without the user clicking Continue.
    return render_template(
        'auth/app_login.html',
        form=form,
        device_name=params['device_name'],
        logout_form=LogoutForm(),
        switch_account_url=switch_account_url,
        cancel_url=cancel_url,
    )
