from functools import cache
from urllib.parse import urlsplit

from flask import Blueprint, abort, redirect, render_template, request, url_for
from flask_login import current_user, login_user, logout_user
from flask_wtf import FlaskForm
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash
from wtforms import EmailField, PasswordField
from wtforms.validators import DataRequired, Email, EqualTo, Length

from extensions import db, limiter, login_manager
from models import User, normalize_email

# Only registered when ACCOUNTS_ENABLED is on (see create_app), so every route 404s otherwise.
bp = Blueprint('auth', __name__)

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def _strip(value):
    return value.strip() if isinstance(value, str) else value


class SignupForm(FlaskForm):
    email = EmailField('Email', filters=[_strip], validators=[DataRequired(), Email(), Length(max=320)])
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


class LoginForm(FlaskForm):
    email = EmailField('Email', filters=[_strip], validators=[DataRequired(), Email(), Length(max=320)])
    password = PasswordField('Password', validators=[DataRequired(), Length(max=MAX_PASSWORD_LENGTH)])


class LogoutForm(FlaskForm):
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
    return safe_next_url(request.args.get('next')) or url_for('auth.login')


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
                login_user(user)
                return redirect(after_login_url())

    return render_template('auth/signup.html', form=form)


@bp.route('/login', methods=['GET', 'POST'])
@limiter.limit('5 per minute;30 per hour', methods=['POST'])
def login():
    if current_user.is_authenticated:
        return render_template('auth/signed_in.html', logout_form=LogoutForm())

    form = LoginForm()
    error = None
    if form.validate_on_submit():
        user = db.session.scalar(db.select(User).filter_by(email=normalize_email(form.email.data)))
        if user and user.check_password(form.password.data):
            login_user(user)
            return redirect(after_login_url())
        if not user or not user.password_hash:
            # Same hashing cost whether or not the account exists, so timing doesn't reveal it.
            check_password_hash(_dummy_password_hash(), form.password.data)
        error = 'Invalid email or password.'

    return render_template('auth/login.html', form=form, error=error)


@bp.route('/logout', methods=['POST'])
def logout():
    if not LogoutForm().validate_on_submit():
        abort(400)
    logout_user()
    return redirect(url_for('index'))
