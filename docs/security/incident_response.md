# Incident Response

Part of the [Information Security Policy](information_security_policy.md). Owner: Max Hoff,
who handles every incident. Security reports go to max@gardenstudiosoftware.com.

An incident is anything that may have exposed, changed, or destroyed user data or the
service's secrets: a leaked key, someone else signing in to one of the
[production accounts](access_control.md), a vulnerability being exploited, a lost or stolen
laptop, a provider reporting a breach, or a user reporting access they didn't make.

## 1. Notice

- **Security logs** in Render (Logs, search `security event=`): bursts of `login_failed`,
  `login_code_failed`, `login_code_locked_out`, `app_token_rejected`, or `rate_limited`;
  `plaid_item_removed`, `password_changed`, or `account_deleted` that users didn't ask for.
- **Provider alerts:** sign-in alerts and security emails from Render, Plaid, Stripe, GitHub
  (including secret-scanning and Dependabot alerts), Google, and Resend.
- **Reports** from users or researchers by email.
- **Health:** Render's health check on `/healthz` and deploy notifications.

Write down when it was noticed and what was seen. Keep a running timeline from here on (in a
private note, not in git).

## 2. Contain

Do the steps that fit what happened. Each one is safe to do on its own.

| What may be exposed | Do this |
| --- | --- |
| A production account (Render, Plaid, Stripe, GitHub, Google, Resend, DNS, the mailbox) | Change its password, sign out all its sessions, check and re-enroll MFA, remove unknown members, keys, and apps. Then rotate every secret that account could see. |
| `PLAID_SECRET` | Plaid Dashboard → Developers → Keys → rotate the production secret; set the new one on Render. |
| `PLAID_TOKEN_KEY` | Generate a new key and set `PLAID_TOKEN_KEY=NEW,OLD` on Render (new tokens use the new key; old ones still decrypt). If the database leaked **with** the key, the access tokens themselves are exposed: rotate each one with Plaid's `/item/access_token/invalidate` (needs a one-off script) or remove the Items so users re-link. |
| `SECRET_KEY` | Generate a new one (`python -c "import secrets; print(secrets.token_hex(32))"`) and set it on Render. This signs **everyone** out of the website **and** the desktop app, voids reset and verification links and pending sign-in codes, and changes the email hashes in the logs. |
| Stripe keys | Stripe → Developers → API keys → roll the secret key; Webhooks → roll the signing secret. Update `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET` on Render. |
| Google client secret | Google Cloud → the OAuth client → add a new secret, set `GOOGLE_CLIENT_SECRET` on Render, delete the old one. |
| `RESEND_API_KEY` | Resend → API Keys → create a new key, set it on Render, delete the old one. |
| The database URL or password | Render → the database → rotate the password (or create a new user), update `DATABASE_URL`, and check the inbound IP allow list is empty. |
| One user's account | Reset their password (signs out their web and desktop sessions) and tell them. |
| Desktop sessions only | Revoke them all (below). Users sign in again; their local data isn't affected. |
| The laptop (lost or stolen) | It's encrypted with FileVault. From another device: sign out its sessions on every provider, rotate the secrets it held (the `.env` has only test keys, but browser sessions reach production), and use Find My to lock or erase it. |

After any change to Render's environment, deploy, then check `https://workbenchbudgeting.com/healthz`
answers `{"status": "ok"}` and run `python scripts/go_live_check.py` in the Render Shell.

To revoke every desktop app session (Render Shell, `flask shell`):

```python
from datetime import datetime, timezone
from extensions import db
from models import AppSession
db.session.execute(db.update(AppSession).where(AppSession.revoked_at.is_(None))
                   .values(revoked_at=datetime.now(timezone.utc)))
db.session.commit()
```

If the service itself is being attacked and can't be contained quickly, set
`ACCOUNTS_ENABLED=false` on Render: every account, billing, API, and Plaid route stops
answering (404), while the public pages stay up. Stripe retries its webhooks for up to three
days, so billing catches up once accounts are back on.

## 3. Find out what happened

Use Render's logs (kept 7 days on Render's Hobby workspace plan and 14 on Pro, so copy the
relevant lines early), the
providers' own logs (Stripe events, Plaid activity, GitHub audit log, Google Cloud audit
logs), and the database. Work out which users and which data were affected, and how.
Fix the cause before turning anything back on.

## 4. Notify

Decide as soon as it's known that user data was, or may have been, exposed:

- **Affected users:** by email, without unreasonable delay: what happened, what data, what
  we've done, and what they should do (for example, change their password or remove and
  re-link a bank). The privacy policy promises to tell users promptly.
- **Plaid:** if Plaid data (access tokens, Item information, or transactions) may be
  involved, tell Plaid within the time its agreement with Garden Studio requires (look up
  the security-incident clause in the signed production agreement and write the period
  here once known: ____).
- **Other providers** whose data or keys are involved (Stripe, Google, Resend, Render), as
  their terms require.
- **Regulators:** Garden Studio is in Illinois. Illinois's Personal Information Protection
  Act requires notifying affected Illinois residents in the most expedient time possible
  and without unreasonable delay, and the Illinois Attorney General as well if more than
  500 residents are affected. Users in other states, the EU, or the UK may bring their own
  deadlines (the GDPR's is 72 hours to the regulator). Get legal advice on which laws apply
  as soon as personal data is involved.

## 5. Review

Within two weeks of closing an incident, write a short review (private): timeline, cause,
what worked, what didn't, and the changes made. Update these documents and add a test if
the cause was in the code.

## Incident log

| Date | Summary | Users affected | Review done |
| --- | --- | --- | --- |
| | | | |
