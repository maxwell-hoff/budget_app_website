import logging

from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.flask_client import OAuth
from flask import Blueprint, abort, current_app, flash, redirect, request, session, url_for
from flask_login import current_user, login_required, login_user
from flask_wtf import FlaskForm

from auth import external_url, message_page, safe_next_url
from extensions import db, limiter
from models import OAuthIdentity, User, normalize_email, utcnow

logger = logging.getLogger(__name__)

GOOGLE_METADATA_URL = 'https://accounts.google.com/.well-known/openid-configuration'
PROVIDER = 'google'
_NEXT_KEY = 'google_next'

# Only registered when ACCOUNTS_ENABLED is on (see create_app), so every route 404s otherwise.
bp = Blueprint('google', __name__)


class UnlinkGoogleForm(FlaskForm):
    pass


def init_google(app):
    app.config['GOOGLE_ENABLED'] = bool(app.config.get('GOOGLE_CLIENT_ID') and app.config.get('GOOGLE_CLIENT_SECRET'))
    oauth = OAuth(app)
    if app.config['GOOGLE_ENABLED']:
        oauth.register(
            PROVIDER,
            client_id=app.config['GOOGLE_CLIENT_ID'],
            client_secret=app.config['GOOGLE_CLIENT_SECRET'],
            server_metadata_url=GOOGLE_METADATA_URL,
            client_kwargs={'scope': 'openid email profile'},
        )


def google_client():
    if not current_app.config.get('GOOGLE_ENABLED'):
        abort(404)
    return current_app.extensions['authlib.integrations.flask_client'].create_client(PROVIDER)


def _failure(text):
    return message_page(
        'Google sign-in failed', "Couldn't sign in with Google", text,
        link=url_for('auth.login'), link_text='Back to log in', status=400,
    )


@bp.route('/auth/google')
@limiter.limit('20 per hour')
def login():
    client = google_client()
    session[_NEXT_KEY] = safe_next_url(request.args.get('next'))
    # Authlib stores `state` and `nonce` in the session and checks both on the callback.
    return client.authorize_redirect(external_url('google.callback'))


@bp.route('/auth/google/callback')
@limiter.limit('20 per hour')
def callback():
    client = google_client()
    try:
        token = client.authorize_access_token()
    except OAuthError as exc:
        logger.info('Google sign-in rejected: %s', exc)
        return _failure('The sign-in request expired or was invalid. Please try again.')

    claims = token.get('userinfo') or {}
    subject = claims.get('sub')
    email = normalize_email(claims.get('email'))
    email_verified = claims.get('email_verified') is True
    if not subject or not email:
        return _failure("Google didn't share an email address for this account.")

    identity = db.session.scalar(db.select(OAuthIdentity).filter_by(provider=PROVIDER, provider_subject=subject))

    if identity:
        if current_user.is_authenticated and identity.user_id != current_user.id:
            return _failure('That Google account is already connected to a different Workbench account.')
        user = identity.user
        identity.email = email
    elif current_user.is_authenticated:
        user = current_user._get_current_object()
        if user.identity(PROVIDER):
            return _failure('A different Google account is already connected to your account.')
        _link(user, subject, email)
        flash('Google is now connected to your account.')
    elif not email_verified:
        # Linking or creating on an unverified address would let someone claim an
        # account for an email they don't control.
        return _failure("Your Google account's email address isn't verified, so we can't use it to sign in.")
    else:
        user = db.session.scalar(db.select(User).filter_by(email=email))
        if user:
            if not user.email_verified_at:
                # Someone may have signed up with this address without owning it. Google has
                # proven who owns it, so drop the unverified password (ending its sessions).
                user.password_hash = None
                user.email_verified_at = utcnow()
        else:
            user = User(email=email, email_verified_at=utcnow())
            db.session.add(user)
        _link(user, subject, email)

    db.session.commit()
    login_user(user)
    return redirect(session.pop(_NEXT_KEY, None) or url_for('account.account'))


def _link(user, subject, email):
    db.session.add(OAuthIdentity(user=user, provider=PROVIDER, provider_subject=subject, email=email))


@bp.route('/account/google/unlink', methods=['POST'])
@login_required
def unlink():
    if not UnlinkGoogleForm().validate_on_submit():
        abort(400)
    user = current_user._get_current_object()
    identity = user.identity(PROVIDER)
    if identity:
        if not user.password_hash:
            flash('Set a password before disconnecting Google, so you can still sign in.')
        else:
            db.session.delete(identity)
            db.session.commit()
            flash('Google has been disconnected from your account.')
    return redirect(url_for('account.account'))
