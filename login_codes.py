"""Emailed one-time codes for password sign-in and sign-up (the second factor).

A correct password (or a new sign-up) starts a *pending login*: the session holds the
user ID, their password fingerprint, the start time, the `login_codes` row, and where to
go afterwards. Entering the emailed code turns it into a real login.
"""
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta

from flask import current_app, session

from extensions import db
from mailer import try_send_email
from models import LoginCode, User, as_utc, utcnow
from security_log import log_event

CODE_TTL = timedelta(minutes=10)
# How long one pending login can go on (resends included) before starting over.
PENDING_TTL = timedelta(minutes=30)
RESEND_INTERVAL = timedelta(minutes=1)
MAX_ATTEMPTS = 5
SESSION_KEY = 'pending_login'


@dataclass
class PendingLogin:
    row: LoginCode
    user: User
    purpose: str  # 'login' or 'signup'
    next_url: str | None


def code_hmac(user_id, code):
    # Keyed, because a plain hash of a 6-digit code is reversed by trying all million.
    key = current_app.config['SECRET_KEY'].encode()
    return hmac.new(key, f'login-code:{user_id}:{code}'.encode(), hashlib.sha256).hexdigest()


def mask_email(email):
    local, _, domain = email.partition('@')
    return f'{local[:1]}•••@{domain}'


def _email_text(code, purpose, change_password_url):
    minutes = int(CODE_TTL.total_seconds() // 60)
    if purpose == 'signup':
        return (
            f"Use this code to finish creating your Workbench Budgeting account:\n\n"
            f"    {code}\n\n"
            f"It expires in {minutes} minutes and works once. "
            f"If you didn't create an account, you can ignore this email.\n"
        )
    return (
        f"Your Workbench Budgeting sign-in code is:\n\n"
        f"    {code}\n\n"
        f"It expires in {minutes} minutes and works once.\n\n"
        f"If you didn't try to sign in, someone may know your password. "
        f"Change your password here:\n{change_password_url}\n"
    )


def send_code(row, purpose, change_password_url):
    """Make a new code for the row (replacing any earlier one) and email it. Returns
    False if the email couldn't be sent."""
    now = utcnow()
    code = f'{secrets.randbelow(10 ** 6):06d}'
    row.code_hmac = code_hmac(row.user_id, code)
    row.expires_at = now + CODE_TTL
    row.sent_at = now
    db.session.commit()
    sent = try_send_email(
        row.user.email, 'Your Workbench Budgeting sign-in code',
        _email_text(code, purpose, change_password_url),
    )
    if sent:
        log_event('login_code_sent', row.user_id, purpose=purpose)
    else:
        row.sent_at = None
        db.session.commit()
    return sent


def start(user, purpose, next_url, change_password_url):
    """Begin a pending login for a user whose password was just checked (or who just
    signed up) and email the code. Returns False if the email couldn't be sent."""
    now = utcnow()
    # Old rows from abandoned logins; also this browser's previous pending login.
    db.session.execute(db.delete(LoginCode).where(LoginCode.expires_at < now - timedelta(days=1)))
    previous = (session.get(SESSION_KEY) or {}).get('code_id')
    if previous:
        db.session.execute(db.delete(LoginCode).where(LoginCode.id == previous))
    row = LoginCode(user=user, code_hmac='', created_at=now, expires_at=now + CODE_TTL)
    db.session.add(row)
    db.session.flush()
    session[SESSION_KEY] = {
        'code_id': row.id,
        'uid': user.id,
        'pw': user.password_fingerprint(),
        'at': int(now.timestamp()),
        'purpose': purpose,
        'next': next_url,
    }
    return send_code(row, purpose, change_password_url)


def clear():
    session.pop(SESSION_KEY, None)


def _drop(row):
    db.session.execute(db.delete(LoginCode).where(LoginCode.id == row.id))
    db.session.commit()
    clear()


def pending():
    """This browser's pending login, or None. It's dropped if it's too old, its row is
    gone, or the password changed since it started (a change or reset voids codes)."""
    data = session.get(SESSION_KEY)
    if not isinstance(data, dict):
        return None
    row = db.session.get(LoginCode, data.get('code_id')) if isinstance(data.get('code_id'), int) else None
    started = data.get('at')
    if (
        row is None
        or row.user_id != data.get('uid')
        or not isinstance(started, int)
        or utcnow().timestamp() - started > PENDING_TTL.total_seconds()
        or not hmac.compare_digest(str(data.get('pw', '')), row.user.password_fingerprint())
    ):
        if row is not None:
            _drop(row)
        clear()
        return None
    return PendingLogin(row=row, user=row.user, purpose=data.get('purpose', 'login'), next_url=data.get('next'))


def can_resend(p):
    return p.row.sent_at is None or utcnow() >= as_utc(p.row.sent_at) + RESEND_INTERVAL


def check(p, code):
    """'ok' (the pending login is used up), 'wrong', 'expired', 'locked' (too many wrong
    tries), or 'gone' (another request used the code first). After 'ok', 'locked', and
    'gone' the pending login is over."""
    row = p.row
    if utcnow() >= as_utc(row.expires_at):
        return 'expired'
    if hmac.compare_digest(code_hmac(row.user_id, code), row.code_hmac):
        # Single use, even if two requests race with the same code.
        claimed = db.session.execute(
            db.delete(LoginCode).where(LoginCode.id == row.id, LoginCode.code_hmac == row.code_hmac)
        ).rowcount
        db.session.commit()
        clear()
        return 'ok' if claimed else 'gone'
    db.session.execute(
        db.update(LoginCode).where(LoginCode.id == row.id).values(attempts=LoginCode.attempts + 1)
    )
    db.session.commit()
    db.session.refresh(row)
    if row.attempts >= MAX_ATTEMPTS:
        _drop(row)
        return 'locked'
    return 'wrong'
