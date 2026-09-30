# CLAUDE.md — Budget App Website

## Project Overview

Marketing and download website for the Workbench Budgeting desktop app, hosted on Render.
It is also becoming the hosted server for paid features (accounts, Stripe billing, and
all Plaid API calls) used by the desktop app in `../budget_app`.

## Architecture

```
serve.py → Flask app: pages, /download/<platform>, /notify
frontend/templates/ → index.html (landing page), about.html
frontend/static/ → CSS, videos, installer downloads (Git LFS)
render.yaml → Render service definition (gunicorn serve:app)
docs/paid_plaid/ → Cross-repo plan for the paid Plaid feature
```

## Running the App

```bash
pip install -r requirements.txt
python serve.py # Flask on http://127.0.0.1:5001
```

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
