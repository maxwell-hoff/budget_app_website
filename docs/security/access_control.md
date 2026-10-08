# Access Control

Part of the [Information Security Policy](information_security_policy.md). Owner: Max Hoff.
Reviewed quarterly (see the review log at the end).

## Who has access

Only the owner, Max Hoff, has access to production: the systems below, their secrets, and
the database. There are no employees, contractors, or support staff, and no shared logins.
Nobody else has an account on any of these systems.

## Systems that can reach production or user data

Every one of these accounts uses a unique password from a password manager and has
multi-factor authentication turned on.

| System | What it gives access to | MFA |
| --- | --- | --- |
| Render | The web service, its environment variables (every production secret), the Postgres database and its backups, logs, the Render Shell | Required |
| Plaid Dashboard | Production API keys, Items, usage | Required |
| Stripe | Customers, subscriptions, payments, API keys, webhooks | Required |
| GitHub | Source code; merging to `main` deploys the website; the desktop release workflow and its Apple signing secrets | Required |
| Apple Developer account | The certificate that signs and notarizes the macOS app | Required |
| Google Cloud | The OAuth client for Sign in with Google | Required |
| Resend | Sending email from `workbenchbudgeting.com`, sent-email history | Required |
| DNS registrar | The `workbenchbudgeting.com` domain and its DNS records | Required |
| `max@gardenstudiosoftware.com` mailbox | Password resets for all of the above | Required |
| Password manager | All of the above passwords and recovery codes | Required |
| Owner's laptop | Development, and signed-in sessions to the above | FileVault disk encryption, login password, screen lock |

Recovery codes for each account are kept in the password manager.

## How production is protected

- **Secrets** (`SECRET_KEY`, `PLAID_SECRET`, `PLAID_TOKEN_KEY`, Stripe and Google
  secrets, the Resend key, the database URL) exist only in Render's environment variables
  and the password manager. They're never committed to git or put in the laptop's `.env`
  (which holds only test-mode and sandbox keys).
- **The database** is reachable only from the web service over Render's private network:
  its inbound IP allow list is empty, so it accepts no connections from the internet. The
  owner reaches it, when needed, through the Render Shell.
- **Plaid access tokens** are encrypted by the application (`PLAID_TOKEN_KEY`, Fernet), so a
  copy of the database alone doesn't expose them.
- **No admin pages.** The website has no administrator interface; there's no way to sign
  in as or view another user. Every request is limited to the signed-in user's own data
  (tests check that one user can't sync, re-link, or remove another user's banks).
- **Users' own access:** passwords are hashed with scrypt; password sign-ins need an emailed
  one-time code; sign-in routes are rate limited; the desktop app gets a revocable 180-day
  session token, stored as a hash on the server and in the OS keychain on the computer.

## Granting and removing access

If anyone else ever needs access (an employee, contractor, or someone covering for the
owner):

1. Give them their own account on only the systems their work needs, with the lowest role
   that works (for example a Render "Developer" role rather than "Admin", read-only in
   Stripe). Never share the owner's logins.
2. Require MFA on their accounts before granting access.
3. Record who has access to what, and why, in the review log below.
4. When their work ends, the same day: remove their accounts on every system, and rotate any
   secret they could have seen (see [incident_response.md](incident_response.md) for how
   to rotate each one).

## Quarterly access review

Every three months (January, April, July, October), check and record:

- [ ] Each system above lists only the expected members (Render workspace members, Plaid
  team members, Stripe team, GitHub collaborators and deploy keys, Google Cloud IAM, Resend
  team).
- [ ] MFA is still on for every account above.
- [ ] No unexpected API keys exist (Stripe, Resend, Plaid, Google Cloud), and unused ones are
  deleted.
- [ ] The Render database's inbound IP allow list is still empty.
- [ ] GitHub: no unknown SSH keys, personal access tokens, or OAuth apps on the account.

## Review log

| Date | Reviewer | Result |
| --- | --- | --- |
| | | |
