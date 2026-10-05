# CLAUDE.md — Budget App Website

## Project Overview

Marketing and download website for the Workbench Budgeting desktop app, hosted on Render.
It is also becoming the hosted server for paid features (accounts, Stripe billing, and
all Plaid API calls) used by the desktop app in `../budget_app`.

## Architecture

```
serve.py → Flask app factory (create_app) + module-level `app`: pages, /download/<platform>, /notify, /healthz
config.py → env-based config (DATABASE_URL, SECRET_KEY, ACCOUNTS_ENABLED, cookie security)
extensions.py → SQLAlchemy `db`, Flask-Migrate `migrate`, Flask-Login `login_manager`, Flask-Limiter `limiter`
models.py → SQLAlchemy models (User, OAuthIdentity, Subscription, StripeEvent, AuthCode, AppSession)
auth.py → account blueprint: sign up/in/out, password reset, email verification (registered only when ACCOUNTS_ENABLED is on)
account.py → /account page blueprint (details, change password, POST /account/delete which removes banks at Plaid and the Stripe customer first; registered only when ACCOUNTS_ENABLED is on)
google_auth.py → Sign in with Google (Authlib OIDC): /auth/google, callback, disconnect
billing.py → Stripe: POST /billing/checkout, /billing/portal, /stripe/webhook (registered only when ACCOUNTS_ENABLED and all STRIPE_* vars are set); has_plaid_access(user) is the only place that decides paid access; the webhook removes a user's banks at Plaid once subscription_ended(user)
app_auth.py → desktop sign-in: GET/POST /app-login (confirm page, one-time PKCE codes), app session create/lookup (registered only when ACCOUNTS_ENABLED is on)
api.py → /v1 JSON API for the desktop app: POST /v1/auth/token, GET /v1/me, POST /v1/auth/logout; require_app_session and api_error (registered only when ACCOUNTS_ENABLED is on)
plaid_api.py → /v1/plaid: link-token, exchange, items, sync (pass-through of Plaid's /transactions/get JSON, not stored), relink-token / relink-complete (update mode), delete; Fernet-encrypted access tokens (PLAID_TOKEN_KEY); CLI `flask plaid-remove-lapsed`; registered only when ACCOUNTS_ENABLED and PLAID_CLIENT_ID/PLAID_SECRET/PLAID_TOKEN_KEY are set
plaid_webhook.py → POST /plaid/webhook: verifies Plaid's JWT, updates item status (registered with plaid_api)
mailer.py → send_email (Resend in production, console in dev, memory in tests)
tokens.py → signed, expiring tokens for password reset and email verification
frontend/templates/auth/, account/ → account page templates (auth.css styles them)
migrations/ → Alembic migrations (flask db ...)
tests/ → pytest suite (conftest.py has app/client fixtures); test_plaid_sandbox.py makes live Plaid sandbox calls, skipped unless PLAID_SANDBOX_CLIENT_ID/PLAID_SANDBOX_SECRET are set
scripts/desktop_flow_check.py → live check of the desktop sign-in and /v1 API against a running local server (plays the desktop app on port 5002)
scripts/check_sync_fixture_with_desktop.py → runs the desktop's unchanged Plaid ingest on a /v1/plaid/sync response (run with the desktop's Python)
scripts/plaid_webhook_check.py → live check of /plaid/webhook with real sandbox webhooks (needs a public tunnel, e.g. cloudflared, to port 5001)
docs/paid_plaid/fixtures/ → shared fixtures for the API contract (plaid_sync_response.json)
frontend/templates/ → index.html (landing page), about.html
frontend/static/ → CSS, videos, installer downloads (Git LFS)
render.yaml → Render service definition (gunicorn serve:app, 120 s worker timeout)
docs/paid_plaid/ → Cross-repo plan for the paid Plaid feature
```

## Running the App

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env # optional; without DATABASE_URL, dev uses instance/app.db (SQLite)
flask db upgrade # FLASK_APP comes from .flaskenv
python serve.py # Flask on http://127.0.0.1:5001
pytest
```

After adding or changing models, create a migration with `flask db migrate -m "<what>"`,
review the generated file in `migrations/versions/`, and commit it.

## Paid Plaid project (cross-repo)

The $8.99/month Plaid feature is built in small steps across this repo and
`../budget_app`. Before working on it, read:

- `docs/paid_plaid/PLAN.md` — steps, status, decisions log, handoff notes (source of truth)
- `docs/paid_plaid/API_CONTRACT.md` — endpoints between the desktop app and this server
- `docs/paid_plaid/AGENT_PROMPT.md` — the prompt used to run one step

Rules: do one step per session, keep new account/billing features behind
`ACCOUNTS_ENABLED`, update `API_CONTRACT.md` with any endpoint change, and update
`PLAN.md` (status, PR link, handoff notes) before finishing.

## Conventions

- Branch naming: `feature/mhoff/<short_topic>_<yyyymmdd>`.
- Secrets come from environment variables only; never commit them.
