"""POST /plaid/webhook: Plaid's Item events, verified by the signed JWT Plaid sends with each one.

https://plaid.com/docs/api/webhooks/webhook-verification/
"""
import base64
import binascii
import hashlib
import hmac
import json
import logging
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from flask import Blueprint, abort, current_app, jsonify, request
from plaid.model.webhook_verification_key_get_request import WebhookVerificationKeyGetRequest

from extensions import db, limiter
from models import PlaidItem
from plaid_api import ITEM_GONE_ERRORS, PLAID_ERRORS, PLAID_TIMEOUT, RELINK_STATUSES, _plaid, plaid_error_code

logger = logging.getLogger(__name__)

# Plaid: reject webhooks whose JWT was issued more than 5 minutes ago.
MAX_AGE_SECONDS = 300
CLOCK_SKEW_SECONDS = 60
# Plaid rotates signing keys rarely and marks old ones expired; refetch now and then.
KEY_CACHE_SECONDS = 3600

# Only registered when ACCOUNTS_ENABLED is on and Plaid is configured (see create_app).
bp = Blueprint('plaid_webhook', __name__)


class InvalidWebhook(Exception):
    pass


# --- Plaid API call (replaced in tests) ---

def fetch_verification_key(key_id):
    response = _plaid().webhook_verification_key_get(
        WebhookVerificationKeyGetRequest(key_id=key_id), _request_timeout=PLAID_TIMEOUT,
    )
    return response.key.to_dict()


# --- Verification ---

def _b64decode(value):
    try:
        return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))
    except (binascii.Error, ValueError) as exc:
        raise InvalidWebhook('bad base64') from exc


def _verification_key(key_id):
    cache = current_app.extensions.setdefault('plaid_webhook_keys', {})
    cached = cache.get(key_id)
    if cached and time.time() - cached[1] < KEY_CACHE_SECONDS:
        return cached[0]
    try:
        key = fetch_verification_key(key_id)
    except PLAID_ERRORS as exc:
        raise InvalidWebhook(f'key {key_id!r} not available: {plaid_error_code(exc)}') from exc
    cache[key_id] = (key, time.time())
    return key


def verify(body, token, now=None):
    """Check the Plaid-Verification JWT against the raw request body; returns its claims."""
    parts = (token or '').split('.')
    if len(parts) != 3:
        raise InvalidWebhook('not a JWT')
    header_b64, claims_b64, signature_b64 = parts
    try:
        header = json.loads(_b64decode(header_b64))
        claims = json.loads(_b64decode(claims_b64))
    except ValueError as exc:
        raise InvalidWebhook('bad JWT JSON') from exc
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise InvalidWebhook('bad JWT JSON')
    # Pinning the algorithm stops "alg": "none" and algorithm-confusion tricks.
    if header.get('alg') != 'ES256' or not isinstance(header.get('kid'), str) or not header['kid']:
        raise InvalidWebhook('unexpected JWT header')

    key = _verification_key(header['kid'])
    if key.get('expired_at') is not None:
        raise InvalidWebhook('signing key expired')
    if key.get('kty') != 'EC' or key.get('crv') != 'P-256':
        raise InvalidWebhook('unexpected key type')
    public_key = ec.EllipticCurvePublicNumbers(
        int.from_bytes(_b64decode(key['x']), 'big'), int.from_bytes(_b64decode(key['y']), 'big'), ec.SECP256R1(),
    ).public_key()

    # JWS ES256 signatures are r || s, 32 bytes each; cryptography wants DER.
    signature = _b64decode(signature_b64)
    if len(signature) != 64:
        raise InvalidWebhook('bad signature length')
    der = encode_dss_signature(int.from_bytes(signature[:32], 'big'), int.from_bytes(signature[32:], 'big'))
    try:
        public_key.verify(der, f'{header_b64}.{claims_b64}'.encode(), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise InvalidWebhook('bad signature') from exc

    now = time.time() if now is None else now
    issued_at = claims.get('iat')
    if type(issued_at) is not int or issued_at < now - MAX_AGE_SECONDS or issued_at > now + CLOCK_SKEW_SECONDS:
        raise InvalidWebhook('too old')
    body_hash = claims.get('request_body_sha256')
    if not isinstance(body_hash, str) or not hmac.compare_digest(body_hash, hashlib.sha256(body).hexdigest()):
        raise InvalidWebhook('body does not match')
    return claims


# --- Handling ---

def _new_status(item, webhook_type, webhook_code, payload):
    """The item's status after this event, or None to leave it."""
    if webhook_type != 'ITEM':
        return None
    if webhook_code == 'ERROR':
        error_code = (payload.get('error') or {}).get('error_code')
        # Plaid: an ITEM ERROR is resolved by sending the user through update mode.
        return 'error' if error_code in ITEM_GONE_ERRORS else 'relink_required'
    if webhook_code in ('PENDING_EXPIRATION', 'PENDING_DISCONNECT'):
        return 'relink_recommended' if item.status == 'ok' else None
    if webhook_code == 'USER_PERMISSION_REVOKED':
        return 'error'
    if webhook_code == 'LOGIN_REPAIRED':
        return 'ok' if item.status in RELINK_STATUSES else None
    return None


@bp.route('/plaid/webhook', methods=['POST'])
@limiter.limit('300 per minute')
def webhook():
    body = request.get_data()
    try:
        verify(body, request.headers.get('Plaid-Verification'))
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError('not an object')
    except (InvalidWebhook, ValueError) as exc:
        logger.warning('Rejected Plaid webhook: %s', exc)
        abort(400)

    webhook_type, webhook_code = payload.get('webhook_type'), payload.get('webhook_code')
    environment = payload.get('environment')
    if environment and environment != current_app.config['PLAID_ENVIRONMENT']:
        logger.warning('Ignoring Plaid %s webhook for the %s environment', webhook_type, environment)
        return jsonify(received=True)

    item = db.session.scalar(db.select(PlaidItem).filter_by(item_id=payload.get('item_id'))) if payload.get('item_id') else None
    if item is None:
        logger.info('Plaid %s %s webhook for an unknown item', webhook_type, webhook_code)
        return jsonify(received=True)

    status = _new_status(item, webhook_type, webhook_code, payload)
    logger.info('Plaid %s %s webhook for item %s: status %s -> %s',
                webhook_type, webhook_code, item.item_id, item.status, status or item.status)
    if status and status != item.status:
        item.status = status
        db.session.commit()
    return jsonify(received=True)
