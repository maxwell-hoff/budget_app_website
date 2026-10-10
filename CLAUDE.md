# CLAUDE.md — Budget App Website

## Project Overview

Marketing and download website for the Workbench Budgeting desktop app, hosted on Render.
It is also becoming the hosted server for paid features (accounts, Stripe billing, and
all Plaid API calls) used by the desktop app in `../budget_app`.

## Architecture

```
serve.py → Flask app factory (create_app) + module-level `app`: pages, legal pages (/privacy, /terms, /refunds; LEGAL_CONTACT_EMAIL, LEGAL_UPDATED), /download/<platform>, /notify, /healthz; security headers on every response (CSP with a per-response nonce: every inline <script> needs nonce="{{ csp_nonce() }}"; HSTS only when cookies are secure)
analytics.py → first-party visit counting: an after_request hook adds a site_visits row per marketing page view (index, about, legal pages) and /download/<platform>, flags bots by user agent / missing Accept-Language, stores no IP or cookie (daily-rotating HMAC visitor), prunes rows after RETENTION_DAYS; CLI `flask site-stats [--days N]`
security_log.py → log_event(event, user_id, **fields): one `security event=… user=… ip=…` line per security event (never passwords, tokens, codes, or emails; email_hash for unknown emails); rate_limit_hit is Flask-Limiter's on_breach
config.py → env-based config (DATABASE_URL, SECRET_KEY, ACCOUNTS_ENABLED, cookie security)
extensions.py → SQLAlchemy `db`, Flask-Migrate `migrate`, Flask-Login `login_manager`, Flask-Limiter `limiter`
models.py → SQLAlchemy models (User, OAuthIdentity, Subscription, StripeEvent, LoginCode, AuthCode, AppSession, PlaidItem, SiteVisit)
auth.py → account blueprint: sign up/in/out, /login/code (emailed sign-in code after every password login and sign-up), password reset, email verification (registered only when ACCOUNTS_ENABLED is on)
login_codes.py → the pending login (in the session) and its emailed 6-digit code: start, pending, check, send_code; codes stored only as HMACs (login_codes table)
account.py → /account page blueprint (details, change password, POST /account/delete which removes banks at Plaid and the Stripe customer first; registered only when ACCOUNTS_ENABLED is on)
google_auth.py → Sign in with Google (Authlib OIDC): /auth/google, callback, disconnect
billing.py → Stripe: POST /billing/checkout, /billing/portal, /stripe/webhook (registered only when ACCOUNTS_ENABLED and all STRIPE_* vars are set); has_paid_access(user) is the only place that decides paid access (the whole desktop app, bank syncing included) and access_until(user) says until when; Checkout adds a TRIAL_DAYS free trial (default 7) for users who never had one (subscriptions.trial_used_at); the webhook emails a reminder on customer.subscription.trial_will_end and removes a user's banks at Plaid once subscription_ended(user)
app_auth.py → desktop sign-in: GET/POST /app-login (confirm page, one-time PKCE codes; refused with a "verify your email" page until the email is verified), app session create/lookup (registered only when ACCOUNTS_ENABLED is on)
api.py → /v1 JSON API for the desktop app: POST /v1/auth/token, GET /v1/me (paid_access, access_until, trial_available), POST /v1/auth/logout; require_app_session and api_error (registered only when ACCOUNTS_ENABLED is on)
plaid_api.py → /v1/plaid: link-token, exchange, items, sync (pass-through of Plaid's /transactions/get JSON, not stored), relink-token / relink-complete (update mode), delete; require_paid_access (402 subscription_required); Fernet-encrypted access tokens (PLAID_TOKEN_KEY); CLI `flask plaid-remove-lapsed`; registered only when ACCOUNTS_ENABLED and PLAID_CLIENT_ID/PLAID_SECRET/PLAID_TOKEN_KEY are set
plaid_webhook.py → POST /plaid/webhook: verifies Plaid's JWT, updates item status (registered with plaid_api)
mailer.py → send_email (Resend in production, console in dev, memory in tests)
tokens.py → signed, expiring tokens for password reset and email verification
frontend/templates/auth/, account/ → account page templates (auth.css styles them)
migrations/ → Alembic migrations (flask db ...)
tests/ → pytest suite (conftest.py has app/client fixtures, password_login / signup_with_code that enter the emailed code, and log_in_as to put a session in directly); test_plaid_sandbox.py makes live Plaid sandbox calls, skipped unless PLAID_SANDBOX_CLIENT_ID/PLAID_SANDBOX_SECRET are set
scripts/desktop_flow_check.py → live check of the desktop sign-in and /v1 API against a running local server (plays the desktop app on port 5002)
scripts/stripe_trial_check.py → live check of the free trial in Stripe test mode with test clocks (needs a running local server and `stripe listen`)
scripts/check_sync_fixture_with_desktop.py → runs the desktop's unchanged Plaid ingest on a /v1/plaid/sync response (run with the desktop's Python)
scripts/plaid_webhook_check.py → live check of /plaid/webhook with real sandbox webhooks (needs a public tunnel, e.g. cloudflared, to port 5001)
scripts/go_live_check.py → read-only preflight that the Stripe/Plaid/Google/Resend/database settings match the code (run in the Render Shell; --mode test for test mode and the sandbox)
docs/paid_plaid/fixtures/ → shared fixtures for the API contract (plaid_sync_response.json)
frontend/templates/ → index.html (landing page; the launch copy (pricing, free week, Sample profile) and the logged-out "Log in" nav link show only when ACCOUNTS_ENABLED is on, otherwise the pre-launch copy), about.html, legal/ (privacy, terms, refunds), _legal_links.html (footer links, included in every page's footer)
frontend/static/ → CSS, videos, installer downloads (Git LFS)
render.yaml → Render service definition (gunicorn serve:app, 120 s worker timeout)
requirements.in, requirements-dev.in → direct dependencies; requirements.txt / requirements-dev.txt are pinned by pip-compile (see below)
.github/workflows/ci.yml → pytest and pip-audit on pull requests and pushes to main; .github/dependabot.yml → weekly pip and GitHub Actions updates
docs/paid_plaid/ → Cross-repo plan for the paid app (whole-app subscription); GO_LIVE.md is the step 18 dashboard checklist
docs/security/ → written security policies (information security, access control, vulnerability management, incident response, data retention, vendors) and the Plaid questionnaire answers; keep them true when the code changes (e.g. what the server stores, MFA, logging)
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

## Dependencies

Add or change dependencies in `requirements.in` (app) or `requirements-dev.in` (tests and
tools), never in the `.txt` files. Then recompile with Python 3.12 (Render's
`PYTHON_VERSION`; another version can drop packages that only 3.12 needs), e.g. in a
3.12 venv with `pip install pip-tools`:

```bash
pip-compile --strip-extras --no-emit-index-url -o requirements.txt requirements.in
pip-compile --strip-extras --no-emit-index-url -o requirements-dev.txt requirements-dev.in
pip install -r requirements-dev.txt && pytest && pip-audit -r requirements.txt
```

To upgrade, add `--upgrade` (everything) or `--upgrade-package <name>` to both
`pip-compile` commands. Without Python 3.12 installed: `uv venv -p 3.12` gets one.

## Paid app project (cross-repo)

A $8.99/month subscription unlocks the whole desktop app (bank syncing through Plaid
included; the Sample profile stays free). It is built in small steps across this repo
and `../budget_app`, with docs in `docs/paid_plaid/` (the folder keeps its original
name). Before working on it, read:

- `docs/paid_plaid/PLAN.md` — steps, status, decisions log, handoff notes (source of truth)
- `docs/paid_plaid/API_CONTRACT.md` — endpoints between the desktop app and this server
- `docs/paid_plaid/AGENT_PROMPT.md` — the prompt used to run one step

Rules: do one step per session, keep new account/billing features behind
`ACCOUNTS_ENABLED`, update `API_CONTRACT.md` with any endpoint change, and update
`PLAN.md` (status, PR link, handoff notes) before finishing.

## Conventions

- Branch naming: `feature/mhoff/<short_topic>_<yyyymmdd>`.
- Secrets come from environment variables only; never commit them.
