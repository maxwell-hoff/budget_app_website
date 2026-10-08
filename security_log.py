"""Security event logs: one line per event, `security event=<name> key=value ...`.

Never pass passwords, tokens, codes, or full email addresses. For an email that doesn't
belong to an account, log `email_hash(email)` instead.
"""
import hashlib
import hmac
import logging
import sys

from flask import current_app, has_request_context, request

logger = logging.getLogger('security')

_WARNING_EVENTS = frozenset({'login_failed', 'app_token_rejected', 'rate_limited'})


def init_logging():
    """Python's default only prints warnings, so give security events their own handler."""
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        logger.addHandler(handler)


def email_hash(email):
    """A short keyed hash: repeated attempts on one address can be correlated in the
    logs without the address itself being written there."""
    key = current_app.config['SECRET_KEY'].encode()
    return hmac.new(key, (email or '').strip().lower().encode(), hashlib.sha256).hexdigest()[:12]


def _value(value):
    # Values are ours (IDs, event and endpoint names, IPs), but keep each event on one line.
    return str(value).replace('\n', ' ').replace('\r', ' ').replace(' ', '_')


def log_event(event, user_id=None, **fields):
    parts = [f'event={event}']
    if user_id is not None:
        parts.append(f'user={user_id}')
    if has_request_context():
        parts.append(f'ip={_value(request.remote_addr)}')
    parts.extend(f'{key}={_value(value)}' for key, value in fields.items() if value is not None)
    level = logging.WARNING if event in _WARNING_EVENTS else logging.INFO
    logger.log(level, 'security %s', ' '.join(parts))


def rate_limit_hit(limit):
    """Flask-Limiter's on_breach callback; returning None keeps the normal 429 response."""
    log_event('rate_limited', endpoint=request.endpoint, path=request.path, limit=limit.limit)
    return None
