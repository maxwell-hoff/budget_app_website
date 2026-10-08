# Data Retention and Deletion

Part of the [Information Security Policy](information_security_policy.md). Owner: Max Hoff.
Matches the [privacy policy](https://workbenchbudgeting.com/privacy)
(`frontend/templates/legal/privacy.html`); change both together. Reviewed yearly with the
policy, and whenever the server starts storing something new.

## What the server stores, and for how long

Database tables (`models.py`), on Render Postgres:

| Data | Table | Kept until |
| --- | --- | --- |
| Email, password hash (scrypt), when the email was verified | `users` | The account is deleted |
| Google account ID and email (Sign in with Google) | `oauth_identities` | Google is disconnected, or the account is deleted |
| Stripe customer and subscription IDs, status, period dates, whether the trial was used | `subscriptions` | The account is deleted |
| Plaid access token (encrypted), Item ID, bank name and ID, status, last sync time | `plaid_items` | The bank is removed, the subscription ends, or the account is deleted. In each case it's removed at Plaid first (`/item/remove`). |
| Desktop sign-in codes (SHA-256 hashes) | `auth_codes` | Used within 5 minutes; deleted a day after they expire |
| Emailed sign-in codes (HMACs) | `login_codes` | Deleted when used or locked out; otherwise a day after they expire |
| Desktop sessions (token hash, device name, sign-in and last-used times) | `app_sessions` | The account is deleted (expired and revoked sessions are kept until then) |
| Stripe event IDs and types (no personal data), to ignore repeats | `stripe_events` | Indefinitely |

Not stored on the server:

- **Bank accounts and transactions.** `/v1/plaid/sync` asks Plaid for them and passes them
  straight to the desktop app in the response; they're never written to the database or
  the logs.
- **Card details.** Entered only on Stripe's pages.
- **Bank usernames and passwords.** Entered only in Plaid Link.
- **The "notify me" form.** Emails are printed to the server log, not saved in the
  database (so they're kept only as long as the logs).

Elsewhere:

| Data | Where | Kept |
| --- | --- | --- |
| Request logs and security event logs (user ID, IP address; never passwords, tokens, codes, or email addresses) | Render logs | Render's retention for the workspace plan: 7 days on Hobby, 14 on Pro, 30 on Scale |
| Database backups | Render point-in-time recovery | 3 days on Hobby, 7 on Pro. Deleted data is gone from backups once the window passes. |
| Sent emails (verification, reset, sign-in code, trial reminder) | Resend | Resend's own retention for the plan. Codes and links in them expire within 10 minutes to 48 hours. |
| Payment records | Stripe | As tax and financial law requires, even after the customer is deleted |
| Bank connection data | Plaid | Under Plaid's End User Privacy Policy, after we remove the Item |

## Deleting data

- **Remove a bank** (desktop app): `DELETE /v1/plaid/items/<id>` removes the Item at Plaid,
  then deletes the row. Works even after a subscription lapses, so nobody is stuck with a
  connection.
- **Subscription ends** (`canceled` or `incomplete_expired` with no paid time left): the
  Stripe webhook removes the user's banks at Plaid and deletes the rows;
  `flask plaid-remove-lapsed` retries any that failed and catches the cases no Stripe event
  announces.
- **Delete account** (`/account` → Delete account, `POST /account/delete`): removes every
  bank at Plaid, deletes the Stripe customer (which cancels any subscription), then deletes
  the user. The database removes their sign-in methods, desktop sessions, subscription row,
  bank rows, and codes with them. If Plaid or Stripe can't be reached, nothing is deleted and
  the user is asked to try again, so no bank is left billing without an owner.
- **Disconnect Google** (`/account`): deletes the Google identity row.

Accounts aren't deleted for inactivity. An account with no subscription holds only an
email address, a password hash, and its sign-in records.

## Requests from users

Users can ask, by email to max@gardenstudiosoftware.com, for a copy of their information,
a correction, or deletion. The privacy policy gives these rights to everyone, wherever they
live.

- Accept a request only from the account's own email address (or after the user signs in
  and confirms from it).
- Answer within 30 days.
- **Copy:** export their rows from the tables above (Render Shell) and send them; don't
  include hashes or encrypted tokens.
- **Deletion:** use the account's Delete account flow (or the same steps by hand if they
  can't sign in), and confirm by email when it's done.

## Data on the user's computer

The desktop app keeps budgets, accounts, and transactions in a local database on the user's
computer, and its session token in the OS keychain. That data is the user's: we never
receive a copy and can't delete it. Signing out of the app removes the keychain entries;
uninstalling doesn't delete the data folder, which the user can delete themselves (the
privacy policy says so).
