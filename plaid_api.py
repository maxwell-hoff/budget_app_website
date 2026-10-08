import json
import logging
from datetime import timedelta
from functools import wraps

import certifi
import click
import plaid
import urllib3
from cryptography.fernet import Fernet, MultiFernet
from flask import Blueprint, current_app, g, jsonify, request
from flask.cli import with_appcontext
from plaid.api import plaid_api
from plaid.model.country_code import CountryCode
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.item_remove_request import ItemRemoveRequest
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.link_token_transactions import LinkTokenTransactions
from plaid.model.products import Products
from plaid.model.transactions_get_request import TransactionsGetRequest
from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions
from werkzeug.exceptions import HTTPException

from api import api_error, iso_utc, json_http_error, require_app_session
from billing import has_paid_access, subscription_ended
from extensions import db, limiter
from models import PlaidItem, User, utcnow
from security_log import log_event

logger = logging.getLogger(__name__)

PLAID_ENVIRONMENTS = {'sandbox': plaid.Environment.Sandbox, 'production': plaid.Environment.Production}
# Plaid bills per connected Item per month, so cap how many one subscription can hold.
MAX_ITEMS_PER_USER = 10
# Plaid only fetches 90 days of history unless asked; the desktop's first pull is 24 months.
TRANSACTIONS_DAYS_REQUESTED = 730
# /item/remove errors that mean the Item is already gone at Plaid.
ITEM_GONE_ERRORS = frozenset({'ITEM_NOT_FOUND', 'INVALID_ACCESS_TOKEN'})
PUBLIC_TOKEN_ERRORS = frozenset({'INVALID_PUBLIC_TOKEN'})
# The user has to sign in to the bank again (update-mode Link).
RELINK_ERRORS = frozenset({'ITEM_LOGIN_REQUIRED'})
# ok: syncing works. relink_recommended: it still works, but the bank's consent ends soon
# (a successful sync doesn't clear this; relink-complete does). relink_required: sync
# fails until the user goes through update mode. error: the Item is gone; remove and re-add.
RELINK_STATUSES = frozenset({'relink_required', 'relink_recommended'})
# Plaid is still pulling the first batch of history, typically for a few seconds after linking.
NOT_READY_ERRORS = frozenset({'PRODUCT_NOT_READY'})
NOT_READY_RETRY_AFTER = 10
# /transactions/get's maximum page size.
TRANSACTIONS_PAGE_SIZE = 500
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
        app.cli.add_command(remove_lapsed_command)


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


def webhook_url():
    # Only from PUBLIC_BASE_URL: a URL built from the request's Host header could point
    # Plaid's webhooks at someone else's server.
    base = current_app.config.get('PUBLIC_BASE_URL')
    return f'{base}/plaid/webhook' if base else None


def _link_token_request(user, **fields):
    webhook = webhook_url()
    if webhook:
        fields['webhook'] = webhook
    return LinkTokenCreateRequest(
        user=LinkTokenCreateRequestUser(client_user_id=str(user.id)),
        client_name='Workbench Budgeting',
        country_codes=[CountryCode('US')],
        language='en',
        **fields,
    )


def create_link_token(user):
    response = _plaid().link_token_create(_link_token_request(
        user,
        products=[Products('transactions')],
        transactions=LinkTokenTransactions(days_requested=TRANSACTIONS_DAYS_REQUESTED),
    ), _request_timeout=PLAID_TIMEOUT)
    return response.link_token, response.expiration


def create_update_link_token(user, access_token):
    """Update mode: Link signs the user back in to an existing Item (no products, no exchange)."""
    response = _plaid().link_token_create(
        _link_token_request(user, access_token=access_token), _request_timeout=PLAID_TIMEOUT,
    )
    return response.link_token, response.expiration


def exchange_public_token(public_token):
    response = _plaid().item_public_token_exchange(
        ItemPublicTokenExchangeRequest(public_token=public_token), _request_timeout=PLAID_TIMEOUT,
    )
    return response.access_token, response.item_id


def remove_item(access_token):
    _plaid().item_remove(ItemRemoveRequest(access_token=access_token), _request_timeout=PLAID_TIMEOUT)


def get_transactions_page(access_token, start_date, end_date, offset):
    return _plaid().transactions_get(TransactionsGetRequest(
        access_token=access_token,
        start_date=start_date,
        end_date=end_date,
        options=TransactionsGetRequestOptions(count=TRANSACTIONS_PAGE_SIZE, offset=offset),
    ), _request_timeout=PLAID_TIMEOUT)


def fetch_transactions(access_token, days_back):
    """Accounts and every transaction from the last `days_back` days, as Plaid's own JSON
    (field names and values exactly as /transactions/get returns them)."""
    end_date = utcnow().date()
    start_date = end_date - timedelta(days=days_back)
    accounts, transactions = None, []
    while True:
        page = get_transactions_page(access_token, start_date, end_date, len(transactions))
        if accounts is None:
            accounts = page.accounts
        transactions.extend(page.transactions)
        if not page.transactions or len(transactions) >= page.total_transactions:
            break
    to_json = plaid.ApiClient.sanitize_for_serialization
    return to_json(accounts), to_json(transactions)


# --- Helpers ---

def _error_body(exc):
    try:
        return json.loads(getattr(exc, 'body', None) or '{}')
    except (TypeError, ValueError):
        return {}


def plaid_error_code(exc):
    return _error_body(exc).get('error_code')


PLAID_ERROR_MESSAGE = 'Our bank connection provider had a problem. Please try again in a few minutes.'


def log_plaid_failure(exc, action, user_id=None):
    body = _error_body(exc)
    logger.warning(
        'Plaid %s failed for user %s: %s (request %s)',
        action, user_id or g.user.id, body.get('error_code') or type(exc).__name__, body.get('request_id'),
    )


def plaid_failure(exc, action):
    log_plaid_failure(exc, action)
    return api_error(502, 'plaid_error', PLAID_ERROR_MESSAGE)


def require_paid_access(view):
    """An app session (else 401) and an active subscription (else 402)."""
    @require_app_session
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not has_paid_access(g.user):
            return api_error(402, 'subscription_required', 'This needs an active Workbench Budgeting subscription.')
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


def _own_item(item_id):
    return db.session.scalar(db.select(PlaidItem).filter_by(user_id=g.user.id, item_id=item_id))


def _mark_if_gone(item, exc):
    """A Plaid call said the Item no longer exists there; it can only be removed and re-added."""
    if plaid_error_code(exc) in ITEM_GONE_ERRORS:
        item.status = 'error'
        db.session.commit()


# --- Removing Items (also used by billing and account deletion) ---

def remove_items_at_plaid(items, reason):
    """Remove each item at Plaid (so Plaid stops billing for it), then here. Returns the
    items that couldn't be removed; they're kept so a later attempt can retry. The caller
    commits."""
    failed = []
    for item in items:
        try:
            remove_item(decrypt_token(item.access_token_encrypted))
        except PLAID_ERRORS as exc:
            if plaid_error_code(exc) not in ITEM_GONE_ERRORS:
                log_plaid_failure(exc, 'item_remove', item.user_id)
                failed.append(item)
                continue
        log_event('plaid_item_removed', item.user_id, item=item.item_id, reason=reason)
        db.session.delete(item)
    return failed


def remove_items_if_subscription_ended(user):
    """Called when a subscription changes; Plaid bills per Item, so lapsed users' Items go."""
    if not user.plaid_items or not subscription_ended(user):
        return []
    items = list(user.plaid_items)
    logger.info('Subscription ended for user %s; removing %d Plaid item(s)', user.id, len(items))
    return remove_items_at_plaid(items, 'subscription_ended')


@click.command('plaid-remove-lapsed')
@with_appcontext
def remove_lapsed_command():
    """Remove Plaid items of users whose subscription has ended (retries failed removals)."""
    removed = failed = 0
    for user in db.session.scalars(db.select(User).where(User.plaid_items.any())).all():
        if not subscription_ended(user):
            continue
        count = len(user.plaid_items)
        left = len(remove_items_at_plaid(list(user.plaid_items), 'subscription_ended'))
        db.session.commit()
        removed += count - left
        failed += left
    click.echo(f'Removed {removed} item(s); {failed} failed and will be retried next time.')


def _sync_item(item, days_back):
    """(result, None) on success, or (None, (status, code, message, headers)) on failure."""
    try:
        accounts, transactions = fetch_transactions(decrypt_token(item.access_token_encrypted), days_back)
    except PLAID_ERRORS as exc:
        code = plaid_error_code(exc)
        if code in RELINK_ERRORS:
            item.status = 'relink_required'
            db.session.commit()
            return None, (409, 'plaid_relink_required', 'Sign in to this bank again to keep syncing it.', None)
        if code in NOT_READY_ERRORS:
            return None, (
                503, 'plaid_not_ready', 'This bank is still loading its history. Try again in a few seconds.',
                {'Retry-After': str(NOT_READY_RETRY_AFTER)},
            )
        _mark_if_gone(item, exc)
        log_plaid_failure(exc, 'transactions_get')
        return None, (502, 'plaid_error', PLAID_ERROR_MESSAGE, None)
    # A pending-expiration warning stays until the user re-links (relink-complete).
    if item.status != 'relink_recommended':
        item.status = 'ok'
    item.last_synced_at = utcnow()
    # Commit per item so a long multi-bank sync doesn't hold a transaction open across Plaid calls.
    db.session.commit()
    return {'item': item_json(item), 'accounts': accounts, 'transactions': transactions, 'error': None}, None


# --- Endpoints ---

@bp.route('/link-token', methods=['POST'])
@require_paid_access
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
@require_paid_access
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
    log_event('plaid_item_linked', g.user.id, item=item.item_id)
    return jsonify(item=item_json(item))


@bp.route('/items')
@require_paid_access
def items():
    rows = db.session.scalars(
        db.select(PlaidItem).filter_by(user_id=g.user.id).order_by(PlaidItem.created_at, PlaidItem.id)
    )
    return jsonify(items=[item_json(item) for item in rows])


@bp.route('/sync', methods=['POST'])
@require_paid_access
@limiter.limit('120 per hour')
def sync():
    data = request.get_json(silent=True) if request.get_data() else {}
    if not isinstance(data, dict):
        return api_error(400, 'bad_request', 'The request body must be a JSON object.')
    days_back = data.get('days_back', TRANSACTIONS_DAYS_REQUESTED)
    if type(days_back) is not int or not 1 <= days_back <= TRANSACTIONS_DAYS_REQUESTED:
        return api_error(400, 'bad_request', f'days_back must be a whole number from 1 to {TRANSACTIONS_DAYS_REQUESTED}.')
    item_id = data.get('item_id')
    if item_id is not None and (not isinstance(item_id, str) or not item_id.strip()):
        return api_error(400, 'bad_request', 'item_id must be a non-empty string.')

    if item_id is not None:
        item = db.session.scalar(db.select(PlaidItem).filter_by(user_id=g.user.id, item_id=item_id.strip()))
        if item is None:
            return api_error(404, 'not_found', 'No bank connection with that ID.')
        result, error = _sync_item(item, days_back)
        if error:
            return api_error(*error)
        return jsonify(items=[result])

    # Every item: one bank's failure is reported in its entry instead of failing the rest.
    results = []
    rows = db.session.scalars(
        db.select(PlaidItem).filter_by(user_id=g.user.id).order_by(PlaidItem.created_at, PlaidItem.id)
    ).all()
    for item in rows:
        result, error = _sync_item(item, days_back)
        if error:
            _status, code, message, _headers = error
            result = {'item': item_json(item), 'accounts': [], 'transactions': [],
                      'error': {'code': code, 'message': message}}
        results.append(result)
    return jsonify(items=results)


@bp.route('/items/<item_id>/relink-token', methods=['POST'])
@require_paid_access
@limiter.limit('30 per hour')
def relink_token(item_id):
    item = _own_item(item_id)
    if item is None:
        return api_error(404, 'not_found', 'No bank connection with that ID.')
    try:
        token, expiration = create_update_link_token(g.user, decrypt_token(item.access_token_encrypted))
    except PLAID_ERRORS as exc:
        _mark_if_gone(item, exc)
        return plaid_failure(exc, 'link_token_create (update mode)')
    return jsonify(link_token=token, expiration=iso_utc(expiration))


@bp.route('/items/<item_id>/relink-complete', methods=['POST'])
@require_paid_access
@limiter.limit('30 per hour')
def relink_complete(item_id):
    item = _own_item(item_id)
    if item is None:
        return api_error(404, 'not_found', 'No bank connection with that ID.')
    # Update mode keeps the same access token, and Plaid sends no webhook when it succeeds
    # in our app, so the client says so. If the bank still needs a login, the next sync
    # sets relink_required again.
    if item.status in RELINK_STATUSES:
        item.status = 'ok'
        db.session.commit()
    return jsonify(item=item_json(item))


@bp.route('/items/<item_id>', methods=['DELETE'])
# Only a session: removing a bank stops Plaid billing, so a lapsed subscriber can still do it.
@require_app_session
def delete_item(item_id):
    item = _own_item(item_id)
    if item is None:
        return api_error(404, 'not_found', 'No bank connection with that ID.')
    try:
        remove_item(decrypt_token(item.access_token_encrypted))
    except PLAID_ERRORS as exc:
        if plaid_error_code(exc) not in ITEM_GONE_ERRORS:
            return plaid_failure(exc, 'item_remove')
    db.session.delete(item)
    db.session.commit()
    log_event('plaid_item_removed', g.user.id, item=item_id, reason='user_request')
    return '', 204
