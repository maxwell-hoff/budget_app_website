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
models.py → SQLAlchemy models (User, OAuthIdentity)
auth.py → account blueprint: sign up/in/out, password reset, email verification (registered only when ACCOUNTS_ENABLED is on)
account.py → /account page blueprint (details, change password; registered only when ACCOUNTS_ENABLED is on)
google_auth.py → Sign in with Google (Authlib OIDC): /auth/google, callback, disconnect
mailer.py → send_email (Resend in production, console in dev, memory in tests)
tokens.py → signed, expiring tokens for password reset and email verification
frontend/templates/auth/, account/ → account page templates (auth.css styles them)
migrations/ → Alembic migrations (flask db ...)
tests/ → pytest suite (conftest.py has app/client fixtures)
frontend/templates/ → index.html (landing page), about.html
frontend/static/ → CSS, videos, installer downloads (Git LFS)
render.yaml → Render service definition (gunicorn serve:app)
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
