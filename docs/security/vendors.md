# Vendors

Part of the [Information Security Policy](information_security_policy.md). Owner: Max Hoff.
These are the service providers that run parts of Workbench Budgeting. The privacy policy
lists the same ones for users. Review yearly, and before adding a new one: it should need
as little user data as possible, offer MFA on its admin accounts, and encrypt data in
transit and at rest. Every account below has MFA on (see [access_control.md](access_control.md)).

| Vendor | What it does for us | User data it holds | Secrets we hold for it |
| --- | --- | --- | --- |
| **Render** | Hosts the website and API (web service) and the Postgres database; terminates HTTPS (through Cloudflare, Render's edge network); keeps logs and backups | Everything in the [database](data_retention_and_deletion.md), encrypted at rest (AES-256, backups included); request and security logs | All production environment variables |
| **Plaid** | Connects users' banks and returns their accounts and transactions | Users' bank connections and the data Plaid retrieves, under Plaid's End User Privacy Policy; our server holds only an encrypted access token per bank | `PLAID_CLIENT_ID`, `PLAID_SECRET`; `PLAID_TOKEN_KEY` is ours (never sent to Plaid) |
| **Stripe** | Subscriptions and payments (Checkout, Customer Portal), with Managed Payments as merchant of record for sales tax | Card details, billing name and address, email, payment history | `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` |
| **Google** | Sign in with Google (OpenID Connect: `openid`, `email`, `profile` only); the site's fonts come from Google Fonts | Google already has the user's account; we receive only their Google ID and email. Font requests reveal visitors' IP addresses to Google. | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` |
| **Resend** | Sends account emails (verification, password reset, sign-in codes, trial reminders) from `workbenchbudgeting.com` | Recipients' email addresses and the emails sent | `RESEND_API_KEY` |
| **GitHub** | Hosts the code for both repositories, runs CI, Dependabot, and the desktop release builds | No user data | None in the repositories or the website's CI. The desktop release workflow has Apple's signing certificate and notarization credentials as GitHub Actions secrets. |

Not vendors of user data, but part of operating the service: Apple (the Developer account
whose certificate signs and notarizes the macOS app), the DNS registrar for
`workbenchbudgeting.com` (controls where the domain points and email authentication
records), the `max@gardenstudiosoftware.com` mailbox provider (password resets for every
account above), and the password manager (every password and recovery code).

## What each vendor is trusted for

- **Render** patches the servers, operating system, and Postgres; we have no server of our
  own. A Render breach could expose the database, so Plaid access tokens are also encrypted
  by the application with a key Render stores separately as an environment variable.
- **Plaid** and **Stripe** take the most sensitive data (bank logins, cards) on their own
  pages, so it never passes through our server.
- **Resend** sees sign-in codes and reset links; these expire quickly (10 minutes and 1 hour)
  and are single-use.
