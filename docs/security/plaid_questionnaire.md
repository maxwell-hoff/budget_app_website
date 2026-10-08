# Plaid Security Questionnaire: Draft Answers

Draft answers to Plaid's production security questionnaire (Plaid Dashboard → production
checklist → Security questionnaire), as of 2026-10-08. Each has the text to paste, then what
it rests on. Submit only when every item under **Before submitting** is done, since several
answers depend on them.

## Before submitting

- [ ] MFA is on for every account in [access_control.md](access_control.md): Render, Plaid,
  Stripe, GitHub, Apple Developer, Google Cloud, Resend, the DNS registrar, the
  `max@gardenstudiosoftware.com` mailbox, and the password manager.
- [ ] Render → the Postgres database → Networking (Access Control): the inbound IP allow
  list is empty (Render's default allows `0.0.0.0/0`, any IP with the password).
- [ ] Laptop: macOS updated (14.8.9 was pending on 2026-10-08), firewall on, password
  required immediately after sleep or screen saver. Then run the
  [laptop checks](vulnerability_management.md#laptop-checks).
- [ ] GitHub → both repositories → Settings → Code security: Dependabot alerts and Dependabot
  security updates on.
- [ ] You've read every document in `docs/security/` and they match what you do.

## 1. Information security policy

> Please explain why you do not have a documented information security policy in place.

Plaid asks this only when the previous question ("Do you have a documented information
security policy?") is answered **No**. Answer that one **Yes**: the policy was adopted on
2026-10-08 ([information_security_policy.md](information_security_policy.md)). If the
dashboard still shows this follow-up, paste:

> We now do. Garden Studio is a one-person company, and until October 2026 our security
> practices were in place but not written down. While preparing for Plaid production access
> we documented them in an Information Security Policy (owner: the founder; reviewed
> yearly) with supporting policies for access control, vulnerability management, incident
> response, data retention and deletion, and vendors. Its principles: store as little
> consumer data as possible (transactions are passed to the user's own computer and never
> stored on our servers), encrypt data in transit and at rest, require MFA for consumers
> and for every administrative account, and keep software patched.

## 2. Access controls

> What access controls does your organization have in place to limit access to production
> assets (physical or virtual) and sensitive data?

> Garden Studio is a one-person company: only the founder has access to production, and
> nobody else holds an account on any production system. Production runs on Render (managed
> web service and Postgres); we operate no physical servers or offices. Every system that
> can reach production or consumer data (Render, Plaid, Stripe, GitHub, Google Cloud, our
> email provider, DNS registrar, and the admin mailbox) uses a unique password from a
> password manager and has MFA enabled. Secrets exist only in Render's environment
> variables and the password manager, never in source code. The database accepts
> connections only from our web service over Render's private network (no public access).
> Plaid access tokens are additionally encrypted by the application with a key stored
> separately from the database. The application has no admin interface, and every request
> is limited to the signed-in user's own data. Access is reviewed quarterly; if we add
> people, they get individual least-privilege accounts with MFA, and access is removed and
> secrets rotated the day their work ends. The only workstation is the founder's laptop,
> with full-disk encryption and automatic updates.

Rests on: [access_control.md](access_control.md); `plaid_api.py` (`encrypt_token`,
per-user queries), tests `test_*someone_elses_item*`; the Before submitting items.

## 3. MFA for consumers before Plaid Link

> Does your organization provide multi-factor authentication (MFA) for consumers on the
> mobile and/or web applications before Plaid Link is surfaced?

**Yes.**

> Yes, MFA is required. Plaid Link is only shown inside our desktop app to a user who has
> signed the app in through our website. Website sign-in with a password always requires a
> second factor: a one-time 6-digit code emailed to the account's verified address (valid
> 10 minutes, single use, 5 wrong attempts end the sign-in, rate limited, stored only as a
> keyed HMAC). It can't be skipped and there's no "remember this device". Users can
> instead use Sign in with Google, which relies on Google's own authentication, including
> the user's Google 2-Step Verification. The desktop app can only be signed in with a
> verified email address. It then receives a revocable session token (stored in the OS
> keychain, valid at most 180 days, ended by a password change or sign-out), after which
> the user must sign in again with MFA.

Rests on: `login_codes.py`, `auth.py` (`/login`, `/login/code`), `app_auth.py` (verified
email at `/app-login`), `tests/test_login_codes.py`, `tests/test_security.py`. Caveats
stated in the answer: Google sign-ins depend on the user's own Google settings, and MFA is
checked at sign-in, not each time Link opens within the session.

## 4. MFA for critical systems

> Is multi-factor authentication (MFA) in place for access to critical systems that store
> or process consumer financial data?

**Yes** (once the first Before submitting item is checked).

> Yes. MFA is enabled on every account that can access systems storing or processing
> consumer financial data: our hosting provider (Render, which runs the application
> servers and database), the Plaid Dashboard, Stripe, GitHub (source code and deploys),
> Google Cloud, our email-sending provider, our DNS registrar, and the administrative
> mailbox used for account recovery. Only the founder has these accounts. There is no
> other path into production: the database isn't reachable from the internet, and the
> application has no admin interface.

Rests on: [access_control.md](access_control.md).

## 5. TLS 1.2 or better in transit

> Does your organization encrypt data in-transit between clients and servers using TLS 1.2
> or better?

**Yes.**

> Yes. Our server (workbenchbudgeting.com) accepts only TLS 1.2 and TLS 1.3; TLS 1.0 and
> 1.1 are refused (verified with Qualys SSL Labs, grade A+, and openssl on 2026-10-08).
> HTTP redirects to HTTPS, and HSTS is set for one year. The desktop app talks to our
> server only over HTTPS with certificate verification, and our server calls Plaid, Stripe,
> Google, and our email provider over HTTPS with certificate verification. Plaid Link runs
> in the user's browser over HTTPS directly with Plaid.

Rests on: [vulnerability_management.md](vulnerability_management.md) (scan log);
`serve.security_headers`; `plaid_api.py` (Plaid client with `certifi`). Not covered by the
question and not stated: the desktop app's own UI talks to its local server over plain HTTP
on `127.0.0.1` (never leaves the computer), and the web service reaches Postgres over
Render's private network.

## 6. Encryption at rest of Plaid data

> Does your organization encrypt consumer data you receive from the Plaid API at-rest?

**Yes** (on our systems; see the note below).

> Yes. Plaid access tokens are the only Plaid data we store on our servers. They're
> encrypted by the application with Fernet (AES-128-CBC with HMAC-SHA256) using a key held
> in a separate environment variable, never in the database or source code, and they're
> never sent to the client or logged. The database itself, including backups, is encrypted
> at rest with AES-256 by our hosting provider (Render). Account and transaction data from
> Plaid is passed through to the user's own computer for their personal budget and is not
> stored on our servers or in our logs. On the user's computer it's stored in a local
> database under the user's control, protected by their device's login and disk encryption.

Rests on: `plaid_api.py` (`encrypt_token`, `/v1/plaid/sync` passes data through),
`models.PlaidItem`, Render's encryption documentation. This is the one answer with a
judgment call: the desktop's local database isn't encrypted by the app (an accepted gap in
the [policy](information_security_policy.md#exceptions-and-known-gaps)). The answer says so
plainly rather than claiming it is.

## 7. Vulnerability scanning

> Do you actively perform vulnerability scans against your employee and contractor machines
> (e.g., laptops) and production assets (e.g., server instances) to detect and patch
> vulnerabilities?

**Yes.**

> Yes. We have no employees or contractors; the founder's laptop is the only workstation.
> It runs macOS with automatic updates, built-in malware protection (XProtect and
> Gatekeeper), full-disk encryption, and the firewall on, and is checked monthly for
> pending updates; security updates are installed within 7 days. Our production servers
> are managed by Render, which patches the operating system and database. For our own code,
> our server's dependencies are pinned and scanned with pip-audit in CI on every change,
> Dependabot alerts cover both of our repositories, and Dependabot opens weekly update PRs
> for the server. Desktop app releases are audited with pip-audit before they ship. We scan the
> production site quarterly with Qualys SSL Labs and Mozilla HTTP Observatory (both A+ on
> 2026-10-08). Patch targets: critical within 7 days, high within 30 days.

Rests on: [vulnerability_management.md](vulnerability_management.md). "Desktop app
releases are audited before they ship" starts with the next release, step 19's, which is
the first to show users this service's Plaid Link: the currently published build predates
the audit, and the 2026-10-08 audit found fixable issues in the desktop's packages. Step 19
pins the desktop's dependencies and adds `pip-audit` and Dependabot to its CI before that
build. If you'd rather say so in the answer, replace that sentence with: "Our desktop
app's dependencies are being pinned and added to the same CI scanning before its next
release."

## 8. Privacy policy

> Does your organization have a privacy policy for the application where Plaid Link will be
> deployed?

**Yes.**

> Yes: https://workbenchbudgeting.com/privacy. It covers the desktop app and the website,
> explains that bank connections use Plaid, what data our server receives from Plaid and
> that transactions are passed to the user's computer and not stored on our servers, links
> Plaid's End User Privacy Policy, and describes retention, deletion, and users' rights. It's
> linked from the footer of every page of our website.

Rests on: `frontend/templates/legal/privacy.html`, `_legal_links.html` (a test checks every
page links it).

## 9. Consent

> Does your organization obtain consent from consumers for the collection, processing, and
> storage of their data?

**Yes.**

> Yes. Users create an account and connect each bank themselves; nothing is collected
> until they do. Before any bank is connected, Plaid Link shows its consent screen, which
> explains what data Workbench Budgeting will receive and links Plaid's End User Privacy
> Policy, and the user must agree to continue. Our privacy policy, linked on every page
> including sign-up and sign-in, describes what we collect and why. Users can remove a bank
> (disconnected at Plaid immediately) or delete their account at any time from the app or
> website.

Rests on: Plaid Link's consent pane (shown by Plaid for every new link); the privacy policy
link in every footer. Gap: the sign-up page doesn't yet say "By creating an account, you
agree to the Terms and Privacy Policy" next to the button, which would make consent at
sign-up explicit. A one-line change to `auth/signup.html` (and the Google button); worth
doing before submitting, then add "and agree to them when they sign up" to the answer.

## 10. Data deletion and retention policy

> Does your organization have a defined and enforced data deletion and retention policy that
> is in compliance with applicable data privacy laws, and is this policy reviewed
> periodically?

**Yes.**

> Yes. Our data retention and deletion policy is documented and enforced in code, and
> reviewed yearly together with our privacy policy. We store as little as possible: account
> details, subscription status, and encrypted Plaid access tokens; transactions are never
> stored on our servers. A bank connection is removed at Plaid (/item/remove) and deleted
> from our database when the user removes it, when their subscription ends (automatically,
> with a daily retry for failures), or when they delete their account. Account deletion is
> self-service and removes all of the user's data from our database, their banks at Plaid,
> and their customer record at our payment provider. Short-lived codes are purged
> automatically, logs are kept for a limited period by our host, and backups age out within
> days. We honor access, correction, and deletion requests from every user regardless of
> location (as the CCPA and GDPR require for their residents) within 30 days.

Rests on: [data_retention_and_deletion.md](data_retention_and_deletion.md);
`account.delete_user_everywhere`, `billing.subscription_ended`, `flask plaid-remove-lapsed`
("daily retry" holds once the Render Cron Job from `GO_LIVE.md` part 9 exists; until then,
drop "with a daily retry for failures"). The policy hasn't been reviewed by a lawyer; the
answer doesn't claim it has.
