import re
from functools import cache
from urllib.parse import urlsplit

import hmac

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from flask_wtf import FlaskForm
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash
from wtforms import EmailField, PasswordField, StringField
from wtforms.validators import DataRequired, Email, EqualTo, Length, Regexp

import login_codes
from extensions import db, limiter, login_manager
from mailer import try_send_email
from models import User, normalize_email, utcnow
from security_log import email_hash, log_event
from tokens import (
    RESET_MAX_AGE, VERIFY_MAX_AGE,
    load_reset_token, load_verify_token, make_reset_token, make_verify_token,
)

# Only registered when ACCOUNTS_ENABLED is on (see create_app), so every route 404s otherwise.
bp = Blueprint('auth', __name__)

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128


@login_manager.user_loader
def load_user(session_id):
    user_id, _, fingerprint = session_id.partition(':')
    if not user_id.isdigit():
        return None
    user = db.session.get(User, int(user_id))
    if not user or not hmac.compare_digest(fingerprint, user.password_fingerprint()):
        return None
    return user


def _strip(value):
    return value.strip() if isinstance(value, str) else value


class NewPasswordForm(FlaskForm):
    password = PasswordField('Password', validators=[
        DataRequired(),
        Length(
            min=MIN_PASSWORD_LENGTH,
            max=MAX_PASSWORD_LENGTH,
            message=f'Password must be {MIN_PASSWORD_LENGTH} to {MAX_PASSWORD_LENGTH} characters.',
        ),
    ])
    confirm = PasswordField('Confirm password', validators=[
        DataRequired(),
        EqualTo('password', message='Passwords must match.'),
    ])


class SignupForm(NewPasswordForm):
    email = EmailField('Email', filters=[_strip], validators=[DataRequired(), Email(), Length(max=320)])


class ForgotPasswordForm(FlaskForm):
    email = EmailField('Email', filters=[_strip], validators=[DataRequired(), Email(), Length(max=320)])


class LoginForm(FlaskForm):
    email = EmailField('Email', filters=[_strip], validators=[DataRequired(), Email(), Length(max=320)])
    password = PasswordField('Password', validators=[DataRequired(), Length(max=MAX_PASSWORD_LENGTH)])


def _without_spaces(value):
    return re.sub(r'[\s-]', '', value) if isinstance(value, str) else value


CODE_MESSAGE = 'Enter the 6-digit code from the email.'


class LoginCodeForm(FlaskForm):
    code = StringField('Code', filters=[_without_spaces], validators=[
        DataRequired(message=CODE_MESSAGE), Regexp(r'^[0-9]{6}$', message=CODE_MESSAGE),
    ], render_kw={'inputmode': 'numeric', 'maxlength': 12, 'autofocus': True})


class ResendCodeForm(FlaskForm):
    pass


class LogoutForm(FlaskForm):
    pass


class ResendVerificationForm(FlaskForm):
    pass


@cache
def _dummy_password_hash():
    return generate_password_hash('not-a-real-password')


def safe_next_url(target):
    if not target or not target.startswith('/') or target.startswith('//') or '\\' in target:
        return None
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return None
    return target


def after_login_url():
    return safe_next_url(request.args.get('next')) or url_for('account.account')


def external_url(endpoint, **values):
    base = current_app.config.get('PUBLIC_BASE_URL')
    if base:
        return base + url_for(endpoint, **values)
    return url_for(endpoint, _external=True, **values)


def send_verification_email(user):
    link = external_url('auth.verify_email', token=make_verify_token(user))
    return try_send_email(
        user.email,
        'Verify your email for Workbench Budgeting',
        f"Welcome to Workbench Budgeting.\n\n"
        f"Confirm your email address by opening this link:\n{link}\n\n"
        f"The link expires in {VERIFY_MAX_AGE // 3600} hours. "
        f"If you didn't create an account, you can ignore this email.\n",
    )


def send_reset_email(user):
    link = external_url('auth.reset_password', token=make_reset_token(user))
    return try_send_email(
        user.email,
        'Reset your Workbench Budgeting password',
        f"Someone asked to reset the password for this Workbench Budgeting account.\n\n"
        f"Choose a new password here:\n{link}\n\n"
        f"The link expires in {RESET_MAX_AGE // 60} minutes and works once. "
        f"If you didn't ask for this, you can ignore this email; your password won't change.\n",
    )


def message_page(title, heading, text, link=None, link_text=None, status=200):
    return render_template(
        'auth/message.html', title=title, heading=heading, text=text, link=link, link_text=link_text,
    ), status


@bp.route('/signup', methods=['GET', 'POST'])
@limiter.limit('10 per hour', methods=['POST'])
def signup():
    if current_user.is_authenticated:
        return redirect(after_login_url())

    form = SignupForm()
    if form.validate_on_submit():
        email = normalize_email(form.email.data)
        if db.session.scalar(db.select(User).filter_by(email=email)):
            form.email.errors.append('An account with that email already exists.')
        else:
            user = User(email=email)
            user.set_password(form.password.data)
            db.session.add(user)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                form.email.errors.append('An account with that email already exists.')
            else:
                log_event('signup', user.id, method='password')
                # Entering the emailed code logs in and verifies the address.
                return start_code_login(user, 'signup')

    return render_template('auth/signup.html', form=form)


def start_code_login(user, purpose):
    sent = login_codes.start(
        user, purpose, safe_next_url(request.args.get('next')), external_url('auth.forgot_password'),
    )
    if not sent:
        flash(CODE_NOT_SENT)
    return redirect(url_for('auth.login_code'))


CODE_NOT_SENT = "We couldn't send the email with your code. Wait a minute, then click “send a new code”."


@bp.route('/login', methods=['GET', 'POST'])
@limiter.limit('5 per minute;30 per hour', methods=['POST'])
def login():
    if current_user.is_authenticated:
        return redirect(after_login_url())

    form = LoginForm()
    error = None
    if form.validate_on_submit():
        user = db.session.scalar(db.select(User).filter_by(email=normalize_email(form.email.data)))
        if user and user.check_password(form.password.data):
            return start_code_login(user, 'login')
        if not user or not user.password_hash:
            # Same hashing cost whether or not the account exists, so timing doesn't reveal it.
            check_password_hash(_dummy_password_hash(), form.password.data)
        if user:
            log_event('login_failed', user.id, reason='bad_password' if user.password_hash else 'no_password')
        else:
            log_event('login_failed', reason='unknown_email', email_hash=email_hash(form.email.data))
        error = 'Invalid email or password.'

    return render_template('auth/login.html', form=form, error=error)


def _start_over(message, next_url=None):
    flash(message)
    return redirect(url_for('auth.login', next=next_url))


@bp.route('/login/code', methods=['GET', 'POST'])
@limiter.limit('10 per minute;60 per hour', methods=['POST'])
def login_code():
    if current_user.is_authenticated:
        return redirect(after_login_url())
    pending = login_codes.pending()
    if pending is None:
        return _start_over('Your sign-in code has expired. Log in again to get a new one.')

    form = LoginCodeForm()
    error = None
    if form.validate_on_submit():
        user = pending.user
        result = login_codes.check(pending, form.code.data)
        if result == 'ok':
            log_event('login_code_accepted', user.id, purpose=pending.purpose)
            # The code arrived in their inbox, which proves they own the address.
            if not user.email_verified_at:
                user.email_verified_at = utcnow()
                db.session.commit()
                log_event('email_verified', user.id, method='login_code')
            login_user(user)
            log_event('login', user.id, method='password')
            return redirect(pending.next_url or url_for('account.account'))
        if result == 'locked':
            log_event('login_code_locked_out', user.id)
            return _start_over('Too many wrong codes. Log in again to get a new one.', pending.next_url)
        if result == 'gone':
            return _start_over('That code was already used. Log in again to get a new one.', pending.next_url)
        if result == 'expired':
            error = 'This code has expired. Send a new code below.'
        else:
            log_event('login_code_failed', user.id, attempts=pending.row.attempts)
            error = "That code isn't right. Check the email and try again."
        form.code.data = ''

    return render_template(
        'auth/login_code.html', form=form, error=error, purpose=pending.purpose,
        masked_email=login_codes.mask_email(pending.user.email), resend_form=ResendCodeForm(),
        start_over_url=url_for('auth.login', next=pending.next_url),
        code_minutes=int(login_codes.CODE_TTL.total_seconds() // 60),
    )


@bp.route('/login/code/resend', methods=['POST'])
@limiter.limit('5 per hour')
def resend_login_code():
    if not ResendCodeForm().validate_on_submit():
        abort(400)
    if current_user.is_authenticated:
        return redirect(after_login_url())
    pending = login_codes.pending()
    if pending is None:
        return _start_over('Your sign-in code has expired. Log in again to get a new one.')
    if not login_codes.can_resend(pending):
        flash('We just sent a code. Wait a minute before asking for another one.')
    elif login_codes.send_code(pending.row, pending.purpose, external_url('auth.forgot_password')):
        flash(f'We sent a new code to {login_codes.mask_email(pending.user.email)}. Earlier codes no longer work.')
    else:
        flash(CODE_NOT_SENT)
    return redirect(url_for('auth.login_code'))


@bp.route('/logout', methods=['POST'])
def logout():
    if not LogoutForm().validate_on_submit():
        abort(400)
    if current_user.is_authenticated:
        log_event('logout', current_user.id)
    login_codes.clear()
    logout_user()
    return redirect(safe_next_url(request.args.get('next')) or url_for('index'))


@bp.route('/forgot-password', methods=['GET', 'POST'])
@limiter.limit('5 per hour', methods=['POST'])
def forgot_password():
    form = ForgotPasswordForm()
    if form.validate_on_submit():
        email = normalize_email(form.email.data)
        user = db.session.scalar(db.select(User).filter_by(email=email))
        if user:
            log_event('password_reset_requested', user.id)
            send_reset_email(user)
        else:
            log_event('password_reset_requested', reason='unknown_email', email_hash=email_hash(email))
        # Same response either way, so this form doesn't reveal which emails have accounts.
        return message_page(
            'Check your email', 'Check your email',
            f'If an account exists for {email}, we sent a link to reset its password. '
            f'The link expires in {RESET_MAX_AGE // 60} minutes.',
        )
    return render_template('auth/forgot_password.html', form=form)


@bp.route('/reset-password/<token>', methods=['GET', 'POST'])
@limiter.limit('10 per hour', methods=['POST'])
def reset_password(token):
    user = load_reset_token(token)
    if not user:
        return message_page(
            'Link expired', 'This link has expired',
            'Password reset links work once and expire after an hour. Request a new one.',
            link=url_for('auth.forgot_password'), link_text='Reset password', status=400,
        )

    form = NewPasswordForm()
    if form.validate_on_submit():
        user.set_password(form.password.data)
        # The link arrived in their inbox, which proves they own the address.
        newly_verified = not user.email_verified_at
        if newly_verified:
            user.email_verified_at = utcnow()
        db.session.commit()
        log_event('password_reset', user.id)
        if newly_verified:
            log_event('email_verified', user.id, method='password_reset')
        login_user(user)
        flash('Your password has been updated.')
        return redirect(url_for('account.account'))

    return render_template('auth/reset_password.html', form=form, email=user.email)


@bp.route('/verify-email/<token>')
def verify_email(token):
    user = load_verify_token(token)
    if not user:
        return message_page(
            'Link expired', 'This link has expired',
            f'Verification links expire after {VERIFY_MAX_AGE // 3600} hours. '
            f'Log in to get a new one.',
            link=url_for('auth.login'), link_text='Log in', status=400,
        )
    if not user.email_verified_at:
        user.email_verified_at = utcnow()
        db.session.commit()
        log_event('email_verified', user.id, method='link')
    return message_page(
        'Email verified', 'Email verified',
        f'Thanks — {user.email} is confirmed.',
        link=url_for('account.account'), link_text='Continue',
    )


@bp.route('/verify-email/resend', methods=['POST'])
@login_required
@limiter.limit('3 per hour')
def resend_verification():
    if not ResendVerificationForm().validate_on_submit():
        abort(400)
    if current_user.email_verified_at:
        flash('Your email is already verified.')
    else:
        send_verification_email(current_user)
        flash(f'We sent a new verification link to {current_user.email}.')
    # /app-login sends people back to itself.
    return redirect(safe_next_url(request.args.get('next')) or url_for('account.account'))
