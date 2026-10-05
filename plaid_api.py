import json
import logging
from functools import wraps

import certifi
import plaid
import urllib3
from cryptography.fernet import Fernet, MultiFernet
from flask import Blueprint, current_app, g, jsonify, request
from plaid.api import plaid_api
from plaid.model.country_code import CountryCode
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.item_remove_request import ItemRemoveRequest
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.link_token_transactions import LinkTokenTransactions
from plaid.model.products import Products
from werkzeug.exceptions import HTTPException

from api import api_error, iso_utc, json_http_error, require_app_session
from billing import has_plaid_access
from extensions import db, limiter
from models import PlaidItem

logger = logging.getLogger(__name__)

PLAID_ENVIRONMENTS = {'sandbox': plaid.Environment.Sandbox, 'production': plaid.Environment.Production}
# Plaid bills per connected Item per month, so cap how many one subscription can hold.
MAX_ITEMS_PER_USER = 10
# Plaid only fetches 90 days of history unless asked; the desktop's first pull is 24 months.
TRANSACTIONS_DAYS_REQUESTED = 730
# /item/remove errors that mean the Item is already gone at Plaid.
ITEM_GONE_ERRORS = frozenset({'ITEM_NOT_FOUND', 'INVALID_ACCESS_TOKEN'})
PUBLIC_TOKEN_ERRORS = frozenset({'INVALID_PUBLIC_TOKEN'})
# Seconds; without a timeout a stalled Plaid call would hold a gunicorn worker indefinitely.
PLAID_TIMEOUT = 30
# Plaid's error responses, and network failures reaching Plaid at all.
PLAID_ERRORS = (plaid.ApiException, urllib3.exceptions.HTTPError)

# Only registered when ACCOUNTS_ENABLED is on and Plaid is configured (see create_app).
bp = Blueprint('plaid_api', __name__, url_prefix='/v1/plaid')
bp.register_error_handler(HTTPException, json_http_error)


def init_plaid(app):
    app.config['PLAID_ENABLED'] = all(
        app.config.get(name) for name in ('PLAID_CLIENT_ID', 'PLAID_SECRET', 'PLAID_TOKEN_KEY')
    )
    if app.config['PLAID_ENABLED'] and app.config['ACCOUNTS_ENABLED']:
        if app.config['PLAID_ENVIRONMENT'] not in PLAID_ENVIRONMENTS:
            raise RuntimeError(f"PLAID_ENVIRONMENT must be one of {', '.join(PLAID_ENVIRONMENTS)}.")
        # Fails at startup, not on the first bank link, if the key is malformed.
        app.extensions['plaid_fernet'] = token_cipher(app.config['PLAID_TOKEN_KEY'])


# --- Access token encryption ---

def token_cipher(keys):
    """PLAID_TOKEN_KEY is one Fernet key, or several comma-separated for rotation: the
    first encrypts, any of them decrypts."""
    return MultiFernet([Fernet(key.strip()) for key in keys.split(',') if key.strip()])


def encrypt_token(token):
    return current_app.extensions['plaid_fernet'].encrypt(token.encode()).decode()


def decrypt_token(ciphertext):
    return current_app.extensions['plaid_fernet'].decrypt(ciphertext.encode()).decode()


# --- Plaid API calls (replaced in tests) ---

def _plaid():
    config = current_app.config
    configuration = plaid.Configuration(
        host=PLAID_ENVIRONMENTS[config['PLAID_ENVIRONMENT']],
        api_key={'clientId': config['PLAID_CLIENT_ID'], 'secret': config['PLAID_SECRET']},
        # python.org's macOS builds have no system CA store; certifi works the same everywhere.
        ssl_ca_cert=certifi.where(),
    )
    return plaid_api.PlaidApi(plaid.ApiClient(configuration))


def create_link_token(user):
    response = _plaid().link_token_create(LinkTokenCreateRequest(
        user=LinkTokenCreateRequestUser(client_user_id=str(user.id)),
        client_name='Workbench Budgeting',
        products=[Products('transactions')],
        transactions=LinkTokenTransactions(days_requested=TRANSACTIONS_DAYS_REQUESTED),
        country_codes=[CountryCode('US')],
        language='en',
    ), _request_timeout=PLAID_TIMEOUT)
    return response.link_token, response.expiration


def exchange_public_token(public_token):
    response = _plaid().item_public_token_exchange(
        ItemPublicTokenExchangeRequest(public_token=public_token), _request_timeout=PLAID_TIMEOUT,
    )
    return response.access_token, response.item_id


def remove_item(access_token):
    _plaid().item_remove(ItemRemoveRequest(access_token=access_token), _request_timeout=PLAID_TIMEOUT)


# --- Helpers ---

def _error_body(exc):
    try:
        return json.loads(getattr(exc, 'body', None) or '{}')
    except (TypeError, ValueError):
        return {}


def plaid_error_code(exc):
    return _error_body(exc).get('error_code')


def plaid_failure(exc, action):
    body = _error_body(exc)
    logger.warning(
        'Plaid %s failed for user %s: %s (request %s)',
        action, g.user.id, body.get('error_code') or type(exc).__name__, body.get('request_id'),
    )
    return api_error(502, 'plaid_error', "Our bank connection provider had a problem. Please try again in a few minutes.")


def require_plaid_access(view):
    """An app session (else 401) and an active subscription (else 402)."""
    @require_app_session
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not has_plaid_access(g.user):
            return api_error(402, 'plaid_access_required', 'Bank syncing needs an active subscription.')
        return view(*args, **kwargs)
    return wrapper


def item_json(item):
    return {
        'item_id': item.item_id,
        'institution_id': item.institution_id,
        'institution_name': item.institution_name,
        'status': item.status,
        'created_at': iso_utc(item.created_at),
        'last_synced_at': iso_utc(item.last_synced_at),
    }


def _item_limit_error():
    if len(g.user.plaid_items) >= MAX_ITEMS_PER_USER:
        return api_error(
            400, 'item_limit_reached',
            f'You can connect up to {MAX_ITEMS_PER_USER} banks. Remove one to add another.',
        )
    return None


def _optional_string(value, max_length):
    if not isinstance(value, str):
        return None
    return value.strip()[:max_length] or None


# --- Endpoints ---

@bp.route('/link-token', methods=['POST'])
@require_plaid_access
@limiter.limit('30 per hour')
def link_token():
    error = _item_limit_error()
    if error:
        return error
    try:
        token, expiration = create_link_token(g.user)
    except PLAID_ERRORS as exc:
        return plaid_failure(exc, 'link_token_create')
    return jsonify(link_token=token, expiration=iso_utc(expiration))


@bp.route('/exchange', methods=['POST'])
@require_plaid_access
@limiter.limit('20 per hour')
def exchange():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get('public_token'), str) or not data['public_token'].strip():
        return api_error(400, 'bad_request', 'public_token is required.')
    institution = data.get('institution')
    if institution is not None and not isinstance(institution, dict):
        return api_error(400, 'bad_request', 'institution must be an object.')
    institution = institution or {}
    error = _item_limit_error()
    if error:
        return error

    try:
        access_token, item_id = exchange_public_token(data['public_token'].strip())
    except PLAID_ERRORS as exc:
        if plaid_error_code(exc) in PUBLIC_TOKEN_ERRORS:
            return api_error(400, 'bad_request', 'That bank link expired or was already used. Connect the bank again.')
        return plaid_failure(exc, 'item_public_token_exchange')

    item = db.session.scalar(db.select(PlaidItem).filter_by(item_id=item_id))
    if item is None:
        item = PlaidItem(user=g.user, item_id=item_id)
        db.session.add(item)
    elif item.user_id != g.user.id:
        logger.error('Plaid item %s already belongs to another user', item_id)
        return api_error(400, 'bad_request', 'That bank connection belongs to a different account.')
    item.access_token_encrypted = encrypt_token(access_token)
    # Display only: Link's metadata comes from the client.
    item.institution_id = _optional_string(institution.get('id'), 64)
    item.institution_name = _optional_string(institution.get('name'), 255)
    item.status = 'ok'
    db.session.commit()
    return jsonify(item=item_json(item))


@bp.route('/items')
@require_plaid_access
def items():
    rows = db.session.scalars(
        db.select(PlaidItem).filter_by(user_id=g.user.id).order_by(PlaidItem.created_at, PlaidItem.id)
    )
    return jsonify(items=[item_json(item) for item in rows])


@bp.route('/items/<item_id>', methods=['DELETE'])
# Only a session: removing a bank stops Plaid billing, so a lapsed subscriber can still do it.
@require_app_session
def delete_item(item_id):
    item = db.session.scalar(db.select(PlaidItem).filter_by(user_id=g.user.id, item_id=item_id))
    if item is None:
        return api_error(404, 'not_found', 'No bank connection with that ID.')
    try:
        remove_item(decrypt_token(item.access_token_encrypted))
    except PLAID_ERRORS as exc:
        if plaid_error_code(exc) not in ITEM_GONE_ERRORS:
            return plaid_failure(exc, 'item_remove')
    db.session.delete(item)
    db.session.commit()
    return '', 204
