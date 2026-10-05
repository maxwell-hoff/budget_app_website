import math
import time
from datetime import timedelta, timezone
from functools import wraps

from flask import Blueprint, g, jsonify, request
from werkzeug.exceptions import HTTPException

from app_auth import clean_device_name, create_app_session, find_app_session, redeem_auth_code
from auth import external_url
from billing import access_until, has_paid_access, trial_available
from extensions import db, limiter
from models import as_utc, utcnow

# Writing last_used_at on every request would add a database write per API call.
LAST_USED_RESOLUTION = timedelta(minutes=5)

_ERROR_CODES = {400: 'bad_request', 401: 'unauthorized', 404: 'not_found', 429: 'rate_limited'}

# The desktop app's API (see docs/paid_plaid/API_CONTRACT.md). Only registered when
# ACCOUNTS_ENABLED is on (see create_app), so every route 404s otherwise.
bp = Blueprint('api', __name__, url_prefix='/v1')


def api_error(status, code, message, headers=None):
    response = jsonify(error={'code': code, 'message': message})
    response.status_code = status
    response.headers.update(headers or {})
    return response


@bp.errorhandler(HTTPException)
def json_http_error(exc):
    """Every HTTP error in the desktop API uses the contract's JSON error format."""
    headers = {}
    if exc.code == 429:
        limit = limiter.current_limit
        if limit:
            headers['Retry-After'] = str(max(1, math.ceil(limit.reset_at - time.time())))
    return api_error(exc.code, _ERROR_CODES.get(exc.code, 'error'), exc.description, headers)


def unauthorized(message='Sign in again from the desktop app.'):
    return api_error(401, 'unauthorized', message, {'WWW-Authenticate': 'Bearer'})


def iso_utc(value):
    return as_utc(value).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') if value else None


def _bearer_token():
    scheme, _, token = request.headers.get('Authorization', '').partition(' ')
    return token.strip() if scheme.lower() == 'bearer' else None


def require_app_session(view):
    """Requires `Authorization: Bearer <session_token>`; sets g.app_session and g.user."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        session = find_app_session(_bearer_token())
        if session is None:
            return unauthorized()
        now = utcnow()
        if session.last_used_at is None or now - as_utc(session.last_used_at) >= LAST_USED_RESOLUTION:
            session.last_used_at = now
            db.session.commit()
        g.app_session = session
        g.user = session.user
        return view(*args, **kwargs)
    return wrapper


@bp.route('/auth/token', methods=['POST'])
@limiter.limit('20 per minute')
def token():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return api_error(400, 'bad_request', 'Send a JSON object with code and code_verifier.')
    code, verifier = data.get('code'), data.get('code_verifier')
    if not isinstance(code, str) or not isinstance(verifier, str):
        return api_error(400, 'bad_request', 'code and code_verifier are required.')
    device_name = data.get('device_name')
    if device_name is not None and not isinstance(device_name, str):
        return api_error(400, 'bad_request', 'device_name must be a string.')

    auth_code = redeem_auth_code(code, verifier)
    if auth_code is None:
        return api_error(400, 'bad_request', 'The sign-in code is invalid, expired, or already used. Sign in again.')

    user = auth_code.user
    session_token, session = create_app_session(user, clean_device_name(device_name) or auth_code.device_name)
    return jsonify(
        session_token=session_token,
        expires_at=iso_utc(session.expires_at),
        user={'id': user.id, 'email': user.email},
    )


@bp.route('/auth/logout', methods=['POST'])
@require_app_session
def logout():
    g.app_session.revoked_at = utcnow()
    db.session.commit()
    return '', 204


@bp.route('/me')
@require_app_session
def me():
    user = g.user
    sub = user.subscription
    subscription = None
    if sub is not None and sub.status:
        subscription = {
            'status': sub.status,
            'current_period_end': iso_utc(sub.current_period_end),
            'cancel_at_period_end': sub.cancel_at_period_end,
        }
    return jsonify(
        user={'id': user.id, 'email': user.email, 'email_verified': user.email_verified_at is not None},
        paid_access=has_paid_access(user),
        access_until=iso_utc(access_until(user)),
        trial_available=trial_available(user),
        subscription=subscription,
        account_url=external_url('account.account'),
    )
