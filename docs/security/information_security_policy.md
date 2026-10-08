# Information Security Policy

**Organization:** Garden Studio (maker of Workbench Budgeting)
**Owner:** Max Hoff, founder and sole operator (max@gardenstudiosoftware.com)
**Adopted:** 2026-10-08 · **Next review:** 2027-10-08 (yearly, and after any incident or
major change)

This policy and the documents it links describe what Garden Studio actually does to protect
its systems and its users' data. Garden Studio is one person, so the owner is responsible
for every part of it.

## Scope

- **The service:** the website and server at `workbenchbudgeting.com` (this repository),
  hosted on Render, with its Postgres database, and the accounts that run it: Render,
  Plaid, Stripe, Google Cloud, Resend, GitHub, the DNS registrar, and the
  `max@gardenstudiosoftware.com` mailbox.
- **The desktop app** (`budget_app` repository): how it's built and released, and the data
  it sends to and receives from the server. Data the app keeps on a user's own computer is
  under that user's control (see [data retention](data_retention_and_deletion.md)).
- **The owner's laptop**, the only computer used to develop and administer the service.

## Principles

1. **Keep as little data as possible.** Users' budgets and transactions live on their own
   computers. The server passes bank transactions from Plaid straight to the desktop app and
   doesn't store them. It keeps only what accounts, billing, and bank connections need.
2. **Encrypt it.** HTTPS (TLS 1.2 or newer) for every connection to the service and from it
   to Plaid, Stripe, Google, and Resend. Plaid access tokens are encrypted by the
   application with a key kept apart from the database, and the database and its backups are
   encrypted at rest by Render (AES-256). Passwords are stored only as scrypt hashes.
3. **Multi-factor authentication everywhere.** Users who sign in with a password must also
   enter a one-time code emailed to them; Google sign-ins rely on Google's own security. Every
   account that can reach production has MFA turned on.
4. **Least access.** Only the owner can reach production. Secrets live only in Render's
   environment variables and a password manager, never in git.
5. **Keep software patched.** Dependencies are pinned and scanned for known vulnerabilities,
   and the laptop installs operating-system updates.
6. **Watch and respond.** Security events are logged without secrets, and there's a written
   plan for incidents.

## Supporting documents

| Document | Covers |
| --- | --- |
| [access_control.md](access_control.md) | Who can reach production and how, MFA on each system, granting and removing access, quarterly review |
| [vulnerability_management.md](vulnerability_management.md) | Dependency scanning, patch targets, production scans, the laptop |
| [incident_response.md](incident_response.md) | Noticing a problem, containing it, rotating keys, notifying people, reviewing afterwards |
| [data_retention_and_deletion.md](data_retention_and_deletion.md) | What the server stores, for how long, and how it's deleted |
| [vendors.md](vendors.md) | Each service provider, what it holds, and why |
| [plaid_questionnaire.md](plaid_questionnaire.md) | Answers to Plaid's security questionnaire, pointing back here |

## Secure development

- All code changes go through a pull request on GitHub. CI runs the test suite (including
  tests for the security headers, sign-in codes, access checks, and that secrets never
  appear in logs) and `pip-audit` on every pull request and on `main`.
- New account and billing features ship behind a feature flag (`ACCOUNTS_ENABLED`) until
  they're ready.
- The site sends a strict Content Security Policy, HSTS, and anti-framing headers on every
  response; forms are protected against CSRF; sign-in routes are rate limited.
- Secrets come only from environment variables (`.env` locally, which git ignores; Render
  in production).

## Logging

Security events (sign-ups, logins and failed logins, sign-in codes, password changes and
resets, email verification, Google sign-in, desktop sign-ins and sign-outs, bank
connections linked and removed, account deletion, rate-limit hits) are written as one
`security event=…` line with the user ID and IP address. Passwords, tokens, codes, and email
addresses are never logged. Logs are kept by Render for its retention period (see
[data retention](data_retention_and_deletion.md)).

## Exceptions and known gaps

Anything this policy doesn't yet meet is listed, with its plan, in the document it belongs
to (for example, the desktop app's dependency scanning in
[vulnerability_management.md](vulnerability_management.md)). Accepted without a fix:

- Transactions on a user's computer are stored in an unencrypted local database, protected
  by that computer's own disk encryption and login. The server stores none.
- There's no outside audit or certification (such as SOC 2).

## Review log

| Date | Reviewer | Changes |
| --- | --- | --- |
| 2026-10-08 | Max Hoff | Adopted |
