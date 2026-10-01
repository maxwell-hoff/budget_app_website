import hmac

from flask import current_app
from itsdangerous import BadSignature, URLSafeTimedSerializer

from extensions import db
from models import User

RESET_MAX_AGE = 60 * 60  # 1 hour
VERIFY_MAX_AGE = 60 * 60 * 48  # 48 hours

_RESET_SALT = 'password-reset'
_VERIFY_SALT = 'email-verify'


def _serializer(salt):
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt=salt)


def make_reset_token(user):
    return _serializer(_RESET_SALT).dumps({'uid': user.id, 'pw': user.password_fingerprint()})


def load_reset_token(token):
    """Return the user for a valid, unexpired, unused reset token, else None."""
    try:
        data = _serializer(_RESET_SALT).loads(token, max_age=RESET_MAX_AGE)
    except BadSignature:  # also covers SignatureExpired
        return None
    user = db.session.get(User, data.get('uid'))
    if not user or not hmac.compare_digest(str(data.get('pw', '')), user.password_fingerprint()):
        return None
    return user


def make_verify_token(user):
    return _serializer(_VERIFY_SALT).dumps({'uid': user.id, 'email': user.email})


def load_verify_token(token):
    """Return the user for a valid, unexpired verification token, else None. The token is
    tied to the email it was sent to, so it stops working if the email changes."""
    try:
        data = _serializer(_VERIFY_SALT).loads(token, max_age=VERIFY_MAX_AGE)
    except BadSignature:
        return None
    user = db.session.get(User, data.get('uid'))
    if not user or user.email != data.get('email'):
        return None
    return user
