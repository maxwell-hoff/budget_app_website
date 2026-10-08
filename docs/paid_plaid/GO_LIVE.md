# Step 18 — Go live with Stripe, Plaid, and Google

The checklist for step 18 in [PLAN.md](PLAN.md). It's all dashboard work; no code changes.
`ACCOUNTS_ENABLED` stays `false` in production the whole time, except for the short
end-to-end test in part 7.

Do the parts in this order. Plaid's production review takes the longest (days, and
weeks for some OAuth banks), so start it first and do the rest while you wait.

1. Plaid production access (start first)
2. Google sign-in
3. Stripe live mode
4. Email (Resend), if it isn't set up yet
5. Render environment variables
6. Preflight: `python scripts/go_live_check.py` in the Render Shell
7. End-to-end test with a real subscription and a real bank
8. Turn accounts off again
9. Optional: a daily Render Cron Job for `flask plaid-remove-lapsed`

Keep every secret in a password manager and on Render only. Don't put live keys in `.env`.

URLs you'll paste into the dashboards:

| What | URL |
| --- | --- |
| Website | `https://workbenchbudgeting.com` |
| Privacy policy | `https://workbenchbudgeting.com/privacy` |
| Terms of service | `https://workbenchbudgeting.com/terms` |
| Refund policy | `https://workbenchbudgeting.com/refunds` |
| Google redirect URI | `https://workbenchbudgeting.com/auth/google/callback` |
| Stripe webhook endpoint | `https://workbenchbudgeting.com/stripe/webhook` |
| Contact email | `max@gardenstudiosoftware.com` |

---

## 1. Plaid production access

**Pick the team first** (see the 2026-10-05 decision). If Plaid support restored an Admin
on your original team, use it: it already has production keys (the desktop's direct
path uses them). Otherwise use the "Workbench Budgeting" team you created and apply for
production there. Everything below happens in the chosen team.

1. Plaid Dashboard → the team switcher (top left) → pick the team.
2. Open the production checklist (Dashboard home, "Get production access" / "Go to
   production"). It walks through these; fill in each one:
   - **Company profile:** legal name "Garden Studio", website `https://workbenchbudgeting.com`,
     your Illinois business address, and the contact email.
   - **Application profile** (what users see inside Plaid Link): app name "Workbench
     Budgeting", logo, website, privacy policy URL. Plaid shows this name and logo when
     users connect a bank.
   - **Use case:** Transactions, for a personal budgeting app. Products: `transactions`
     only (that's all the server's link token asks for).
   - **Pricing plan:** Pay as you go is enough to start (no minimum). Transactions are
     billed per connected bank (Item) per month, which is why the server caps each user
     at 10 banks and removes them when a subscription ends.
   - **Security questionnaire:** paste the answers from
     [`docs/security/plaid_questionnaire.md`](../security/plaid_questionnaire.md)
     (step 18c), after doing its "Before submitting" list. The cheat sheet below
     summarizes the facts behind them.
3. **OAuth banks.** Large banks (Chase, Wells Fargo, Capital One, Schwab, and others)
   only connect after you register with each of them through Plaid: Dashboard → the
   OAuth institutions / institution access page. It needs the company and application
   profiles to be complete. Some banks approve within days, Chase can take weeks. Banks
   that use a username and password work as soon as production access is granted.
4. Once approved: Developers → Keys → copy the **client ID** and the **Production
   secret** for part 5.

Nothing else needs configuring at Plaid:
- **Webhooks:** the server sets `https://workbenchbudgeting.com/plaid/webhook` on each
  Item when it creates the link token, so there's no dashboard webhook setting.
- **Allowed redirect URIs:** none needed. Plaid Link runs in a desktop browser, where
  OAuth banks open in a pop-up window. The desktop app never sends a `redirect_uri`.

### Security questionnaire cheat sheet

The ready-to-paste answers, one per question, are in
[`docs/security/plaid_questionnaire.md`](../security/plaid_questionnaire.md), backed by
the written policies in [`docs/security/`](../security/information_security_policy.md).
The facts below match what the code does. The items marked **(you)** depend on how you
run your own accounts; the questionnaire file's "Before submitting" list covers them.

- **Where access tokens live:** only on the server, encrypted at rest with Fernet
  (AES-128 in CBC mode with an HMAC-SHA256 tag) using a key held in an environment
  variable (`PLAID_TOKEN_KEY`), separate from the database. They're never sent to the
  desktop app or logged.
- **Transaction data:** passed through to the user's own computer and not stored on the
  server. The server stores account details (email, password hash), subscription
  status, and the bank connections' IDs, names, and status.
- **In transit:** HTTPS everywhere (Render terminates TLS; Plaid, Stripe, Google, and
  Resend are called over HTTPS with certificate checks).
- **At rest:** Render's managed Postgres (encrypted at rest by Render) plus the token
  encryption above.
- **Authentication for end users:** passwords hashed with scrypt, or Sign in with
  Google; rate-limited login; CSRF protection on forms; the desktop app uses
  OAuth-style PKCE sign-in and a revocable 180-day session token stored in the OS
  keychain. The desktop app (and so Plaid Link) can only be signed in with a verified
  email address (step 18a).
- **MFA (step 18b):** required for every password sign-in and at sign-up: after the
  password, a one-time 6-digit code is emailed to the account's address (valid 10
  minutes, single use, 5 wrong tries end the attempt, resends rate limited, stored only
  as a keyed HMAC). There's no way to skip it and no "remember this browser". Google
  sign-ins rely on Google's own sign-in security, including the user's Google 2-Step
  Verification. Since the desktop app signs in through the website, desktop sign-ins
  (and so bank linking) get the same MFA.
- **Web security (step 18a):** HSTS, a Content Security Policy (scripts only from the
  site itself with a per-response nonce; no framing), `X-Frame-Options: DENY`,
  `nosniff`, and a strict referrer policy on every response.
- **Logging and monitoring (step 18a):** security events (sign-ups, logins and failed
  logins, sign-in codes sent, failed, locked out, and accepted, password changes and
  resets, email verification, Google sign-in, desktop sign-ins and sign-outs, banks
  linked and removed, account deletion, rate-limit hits)
  are logged with the user ID and IP, never passwords, tokens, codes, or email
  addresses. They're in Render's logs (search for `security event=`).
- **Consent and deletion:** users connect banks through Plaid Link; they can remove a
  bank in the app (removed at Plaid immediately) or delete their account (all banks
  removed at Plaid, then the account deleted). Banks are removed automatically when a
  subscription ends.
- **Retention:** bank connections are kept only while the subscription is active.
- **Privacy policy:** `https://workbenchbudgeting.com/privacy` (it covers Plaid and links
  Plaid's End User Privacy Policy).
- **Access control (you):** one person (you) has access to production; MFA on every
  account that reaches it, and the database's inbound IP allow list empty
  ([`access_control.md`](../security/access_control.md)).
- **Vulnerability management (you):** dependencies are pinned (`requirements.txt`,
  compiled from `requirements.in` with `pip-compile`); CI runs the tests and
  `pip-audit` on every pull request and push to `main`; Dependabot opens weekly update
  PRs for Python packages and GitHub Actions (step 18a); production scanned with SSL Labs
  and Mozilla Observatory; the laptop kept updated
  ([`vulnerability_management.md`](../security/vulnerability_management.md)). Turn on
  Dependabot alerts and security updates in both repos' GitHub settings.

---

## 2. Google sign-in

In Google Cloud Console, open the project that has the OAuth client you use for
development, then **Google Auth Platform** (formerly "OAuth consent screen").

1. **Branding:** app name "Workbench Budgeting", user support email, app home page
   `https://workbenchbudgeting.com`, privacy policy and terms URLs, authorized domain
   `workbenchbudgeting.com`, developer contact email. If Google asks you to verify the
   domain, add the TXT record it shows (Google Search Console) at your DNS provider.
   A logo is optional; adding one makes Google ask for brand verification (a few
   business days), so you can skip it to launch sooner.
2. **Data access:** only `openid`, `email`, and `profile` (what the code asks for). These
   are non-sensitive, so no security review is needed.
3. **Audience:** user type External. Click **Publish app** so the status reads "In
   production". While it says "Testing", only listed test users can sign in.
4. **Clients:** either add the production redirect URI to your existing Web client, or
   (cleaner, since the dev secret sits in your laptop's `.env`) create a new Web client,
   "Workbench Budgeting (production)", with:
   - Authorized redirect URI: `https://workbenchbudgeting.com/auth/google/callback`
   - No JavaScript origins needed (the sign-in runs on the server).
   Copy its client ID and secret for part 5.

If brand verification is pending, sign-in still works; Google shows the domain instead
of the app name until it finishes.

---

## 3. Stripe live mode

Switch the dashboard out of test mode for everything here.

1. **Activate the account** if Stripe asks: business details (Garden Studio), bank
   account for payouts, identity. Confirm **Managed Payments** (Stripe as merchant of
   record) is on in live mode too.
2. **Public details** (Settings → Business → Public details): website
   `https://workbenchbudgeting.com`, support email, privacy policy and terms URLs, and a
   statement descriptor customers will recognize (e.g. `WORKBENCH BUDGETING`, 22
   characters max). Checkout shows the privacy and terms links from here.
3. **Product:** Product catalog → your test-mode "Workbench Budgeting" product → **Copy
   to live mode** (or create it again). Check in live mode:
   - Name "Workbench Budgeting", description "Full access to the Workbench Budgeting
     app, including bank syncing" (Checkout shows both).
   - Tax code `txcd_10103000` (Software as a service, personal use).
   - One price: $8.99 USD, recurring, monthly. Copy its `price_…` ID.
   - No trial on the price: the server adds the 7-day trial at Checkout.
4. **Customer portal** (Settings → Billing → Customer portal, in live mode): turn on
   **Cancel subscriptions** set to **at the end of the billing period**, turn on
   **Update payment methods**, set the privacy policy and terms links, then **Save**.
   The portal has no live-mode settings until you save once.
5. **Failed payments** (Settings → Billing → Subscriptions and emails → Manage failed
   payments): Smart Retries on, and when all retries fail, **cancel the subscription**
   (not "mark as unpaid"). The server keeps banks connected while a subscription is
   `unpaid`, so "mark as unpaid" would leave Plaid billing you for non-payers
   indefinitely. Canceling removes their banks at Plaid automatically.
6. **Customer emails** (Settings → Customer emails): turn on receipts for successful
   payments and refunds. Stripe's own trial-ending email isn't needed; the server sends
   one.
7. **Webhook** (Developers → Webhooks → Add destination → your account):
   - Endpoint URL `https://workbenchbudgeting.com/stripe/webhook`
   - API version: the latest (the server re-fetches each subscription, so any recent
     version works).
   - Events (these seven):
     `checkout.session.completed`, `customer.subscription.created`,
     `customer.subscription.updated`, `customer.subscription.deleted`,
     `customer.subscription.trial_will_end`, `invoice.paid`, `invoice.payment_failed`
   - After creating it, reveal and copy the **signing secret** (`whsec_…`).
   - Only one endpoint for this URL: each has its own secret, and the server knows one.
8. **API key** (Developers → API keys): reveal the live **secret key** (`sk_live_…`).

---

## 4. Email (Resend)

Skip this if you already set up Resend for production (part 6's check tells you).

1. Resend → Domains → add `workbenchbudgeting.com` and add the DNS records it shows.
   Wait for "Verified".
2. API Keys → create a key (full access lets the preflight check the domain; "Sending
   access" also works, the check then just can't see the domain).

Emails come from `Workbench Budgeting <noreply@workbenchbudgeting.com>` unless you set
`EMAIL_FROM`.

---

## 5. Render environment variables

Render → the `workbench-budgeting` service → Environment. Set or check:

| Variable | Value |
| --- | --- |
| `STRIPE_SECRET_KEY` | `sk_live_…` |
| `STRIPE_PRICE_ID` | the live `price_…` |
| `STRIPE_WEBHOOK_SECRET` | the live endpoint's `whsec_…` |
| `PLAID_CLIENT_ID` | the chosen team's client ID |
| `PLAID_SECRET` | the **Production** secret |
| `PLAID_ENVIRONMENT` | `production` |
| `PLAID_TOKEN_KEY` | already set at step 11? If not, generate one (below) and save a copy in your password manager. Never change it once banks are linked (rotation: `NEW,OLD`) |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | the production client |
| `RESEND_API_KEY` | from part 4 |
| `ACCOUNTS_ENABLED` | `false` (leave it) |

Leave `TRIAL_DAYS` and `PUBLIC_BASE_URL` unset (7 days, and `https://workbenchbudgeting.com`).

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Save, then deploy (Render offers "Save, rebuild, and deploy"). With `ACCOUNTS_ENABLED`
off, none of these change anything visitors see.

---

## 6. Preflight

Render → the service → **Shell**, then:

```bash
python scripts/go_live_check.py
```

It only reads: it checks the config, the database and migrations, the Stripe key's mode,
the price ($8.99/month, active, live), the product's name and tax code, that the
webhook endpoint exists and sends all seven events, the portal settings, that Plaid
accepts the production keys (an unbilled call), that `PLAID_TOKEN_KEY` is valid and
decrypts any stored tokens, the Google client's format, the Resend sender domain, and
that `/healthz`, `/privacy`, `/terms`, `/refunds` answer 200. It ends with
`0 failed, N warnings` and exits 1 if anything failed.

Fix every `[FAIL]` and rerun. `[warn]` lines are worth reading but don't block, e.g.
"Account can receive payouts" until Stripe verifies your bank account.

Before the Plaid approval comes through, the Plaid line fails; finish the other parts
and come back.

---

## 7. End-to-end test (the "Done when")

This turns accounts on in production for a short window. The account pages aren't
linked anywhere for logged-out visitors, so the exposure is small. A staging service
would need its own domain, Google redirect URI, Stripe webhook, and database, which is
more setup than it's worth here.

1. **Accounts on:** Render → Environment → `ACCOUNTS_ENABLED=true` → save and deploy.
   `https://workbenchbudgeting.com/login` now loads (it 404s while off).
2. **Sign up** at `/signup` with your real email. The verification email arrives
   (Resend works). Click it.
3. **Google:** log out, then "Sign in with Google" with the same Gmail address. The
   consent screen shows Workbench Budgeting (or the domain while branding is pending)
   and you land on `/account`.
4. **Subscribe:** `/account` → **Start your free week** → Stripe Checkout (live) with
   your real card. Back on `/account`: "Free trial: ends on …". In Stripe (live) →
   Developers → Webhooks → your endpoint, the deliveries show 200.
5. **Real charge:** Stripe (live) → Customers → you → the subscription → end the trial
   now (Actions → Update subscription, set the trial to end immediately). Stripe
   charges $8.99; `/account` shows the subscription as active, and the webhook shows
   `invoice.paid` delivered with 200. (Alternative: set `TRIAL_DAYS=0` on Render for
   the test, subscribe, then delete the variable.)
6. **Desktop, on a throwaway database** so your real budget isn't touched and the old
   direct-path bank in `~/.zshrc` isn't picked up:

   ```bash
   cd ~/personal_projects_local/budget_app
   env -u PLAID_ACCESS_TOKEN -u PLAID_CLIENT_ID -u PLAID_SECRET \
     BUDGET_APP_CLOUD=1 python serve.py --db-path /tmp/workbench_go_live/budget.db
   ```

   Open `http://127.0.0.1:5002`. Actions menu → **Sign in** → the browser opens
   `https://workbenchbudgeting.com/app-login` → **Continue**. The menu shows you
   signed in and subscribed. Create a profile (not Sample), then **Connect bank**:
   Plaid Link opens in production; connect one of your real banks (one that doesn't
   need OAuth approval, if those are still pending). Transactions appear in the
   profile. That's the "Done when": a real subscription unlocks the app and a real
   bank link works in production.
7. Optional: Refresh the connection once more (no duplicates), and in the Plaid
   Dashboard (production) check the Item shows up under the team's usage/activity.

## 8. Turn accounts off again

1. Decide whether to keep your subscription and bank:
   - **Keep both:** nothing to do; you're the first customer. The desktop test database
     can go (`rm -r /tmp/workbench_go_live`), but remove the bank in the app first if
     you don't want it billed.
   - **Undo the test:** `/account` → Delete account (removes the bank at Plaid and
     deletes the Stripe customer, which cancels the subscription), then refund the
     $8.99 in Stripe → Payments → the charge → Refund.
2. Render → `ACCOUNTS_ENABLED=false` → save and deploy. `/login` 404s again.
3. In the desktop app, sign out (Actions menu) before deleting the test database, so
   the keychain entries for production are cleared.

Then set step 18 to `done` in PLAN.md.

---

## 9. Optional: daily cleanup job

`flask plaid-remove-lapsed` removes banks of users whose subscription ended without a
Stripe event announcing it (a subscription canceled immediately keeps access until its
period end) and retries removals that failed. Worth having before launch (step 19):

Render → New → **Cron Job**, same repo and branch, build command
`pip install -r requirements.txt`, command `flask plaid-remove-lapsed`, schedule
`0 9 * * *` (daily), and the same environment variables as the web service (simplest:
move them into an Environment Group and link it to both). It needs `ACCOUNTS_ENABLED`
on and the Plaid variables to register the command, so leave it until step 19 or give
the job `ACCOUNTS_ENABLED=true` itself (a cron job serves no pages).
