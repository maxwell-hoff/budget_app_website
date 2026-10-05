"""Feed a /v1/plaid/sync response through the desktop app's real Plaid ingest code.

Runs `backend.plaid_data_fetcher.fetch_and_save_plaid_data` from the desktop repo, unchanged,
with only its Plaid client swapped for one that returns the response's accounts and
transactions, into throwaway SQLite databases. It does this twice:

  1. as plaid-python model objects, which is what the desktop's direct Plaid path gets today;
  2. as the plain JSON wrapped in SimpleNamespace, the light adapter step 15 can use instead.

Both must save the same rows (including hash_id, the desktop's dedup key), so switching a bank
from the direct path to the server never duplicates its transactions.

Run it with the desktop app's Python (it needs pandas, PyYAML, and plaid-python):

    python scripts/check_sync_fixture_with_desktop.py [--desktop ../budget_app] [response.json]

The response defaults to docs/paid_plaid/fixtures/plaid_sync_response.json.
"""
import argparse
import copy
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent.parent
TABLES = (
    'plaid_checking_account_transactions',
    'plaid_credit_card_transactions',
    'plaid_investment_account_transactions',
)


def as_namespace(value):
    if isinstance(value, dict):
        return SimpleNamespace(**{key: as_namespace(v) for key, v in value.items()})
    if isinstance(value, list):
        return [as_namespace(v) for v in value]
    return value


def as_plaid_models(data, model):
    import plaid
    from plaid.model_utils import validate_and_convert_types
    return validate_and_convert_types(
        copy.deepcopy(data), ([model],), ['received_data'], True, True, configuration=plaid.Configuration(),
    )


def ingest(accounts, transactions, db_path):
    from backend import plaid_data_fetcher
    from backend.plaid_data_retreiver import PlaidDataRetriever

    class SyncedDataRetriever(PlaidDataRetriever):
        def __init__(self, *args, **kwargs):
            pass  # no Plaid client: the data is already here

        def get_accounts(self):
            return accounts

        def get_transactions(self, *args, **kwargs):
            return {'transactions': transactions, 'accounts': accounts,
                    'total_transactions': len(transactions), 'request_id': None}

    original = plaid_data_fetcher.PlaidDataRetriever
    plaid_data_fetcher.PlaidDataRetriever = SyncedDataRetriever
    try:
        result = plaid_data_fetcher.fetch_and_save_plaid_data('unused', db_path=db_path, silent=True)
    finally:
        plaid_data_fetcher.PlaidDataRetriever = original
    if not result['success']:
        raise SystemExit(f"Desktop ingest failed: {result['message']}")
    return result['total_saved']


def saved_rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = {}
    for table in TABLES:
        found = conn.execute(f'SELECT * FROM {table} ORDER BY hash_id').fetchall()
        rows[table] = [{k: r[k] for k in r.keys() if k != 'id'} for r in found]
    conn.close()
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('response', nargs='?', default=HERE / 'docs/paid_plaid/fixtures/plaid_sync_response.json')
    parser.add_argument('--desktop', default=HERE.parent / 'budget_app', help='path to the budget_app repo')
    args = parser.parse_args()

    sys.path.insert(0, str(Path(args.desktop).resolve()))
    # fetch_and_save_plaid_data checks these exist before doing anything; nothing calls Plaid.
    os.environ.setdefault('PLAID_CLIENT_ID', 'unused')
    os.environ.setdefault('PLAID_SECRET', 'unused')
    from plaid.model.account_base import AccountBase
    from plaid.model.transaction import Transaction

    response = json.loads(Path(args.response).read_text())
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for n, entry in enumerate(response['items']):
            label = entry['item'].get('institution_name') or entry['item']['item_id']
            if entry['error']:
                print(f"{label}: skipped ({entry['error']['code']})")
                continue
            models_db, json_db = os.path.join(tmp, f'models{n}.db'), os.path.join(tmp, f'json{n}.db')
            from_models = ingest(
                as_plaid_models(entry['accounts'], AccountBase),
                as_plaid_models(entry['transactions'], Transaction), models_db,
            )
            from_json = ingest(as_namespace(entry['accounts']), as_namespace(entry['transactions']), json_db)
            models_rows, json_rows = saved_rows(models_db), saved_rows(json_db)
            counts = ', '.join(f'{table}={len(rows)}' for table, rows in models_rows.items())
            print(f"{label}: {len(entry['transactions'])} transactions in, {from_models} saved ({counts})")
            if from_models == 0:
                print('  FAIL: nothing was saved')
                failures += 1
            if (from_models, models_rows) != (from_json, json_rows):
                print('  FAIL: the SimpleNamespace path saved different rows than the plaid-python path')
                failures += 1
    if failures:
        raise SystemExit(1)
    print('OK: the desktop ingest accepts this response, and both paths save identical rows.')


if __name__ == '__main__':
    main()
