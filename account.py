import logging

import stripe
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from flask_wtf import FlaskForm
from wtforms import PasswordField, StringField
from wtforms.validators import DataRequired, Length, Optional

import billing
import plaid_api
from auth import MAX_PASSWORD_LENGTH, LogoutForm, NewPasswordForm, ResendVerificationForm
from billing import BillingForm, subscription_summary
from extensions import db, limiter
from google_auth import UnlinkGoogleForm
from models import normalize_email

logger = logging.getLogger(__name__)

# Only registered when ACCOUNTS_ENABLED is on (see create_app), so every route 404s otherwise.
bp = Blueprint('account', __name__)


class ChangePasswordForm(NewPasswordForm):
    # Optional because Google-only users (step 7) have no password yet; checked in the view.
    current_password = PasswordField('Current password', validators=[Optional(), Length(max=MAX_PASSWORD_LENGTH)])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.password.label.text = 'New password'
        self.confirm.label.text = 'Confirm new password'


class DeleteAccountForm(FlaskForm):
    confirm_email = StringField('Type your email address to confirm', id='delete_confirm_email',
                                validators=[DataRequired(), Length(max=320)])
    # Required in the view when the user has a password; Google-only users have none.
    # Its own id because the change-password form on the same page also has current_password.
    current_password = PasswordField('Current password', id='delete_current_password',
                                     validators=[Optional(), Length(max=MAX_PASSWORD_LENGTH)])


@bp.route('/account', methods=['GET', 'POST'])
@login_required
@limiter.limit('10 per hour', methods=['POST'])
def account():
    user = current_user._get_current_object()
    has_password = bool(user.password_hash)
    form = ChangePasswordForm()

    if form.validate_on_submit():
        if has_password and not user.check_password(form.current_password.data or ''):
            form.current_password.errors.append('Current password is incorrect.')
        else:
            user.set_password(form.password.data)
            db.session.commit()
            # The session ID includes a password fingerprint; log in again so this session
            # survives while every other session is ended.
            login_user(user)
            flash('Your password has been updated.' if has_password else 'Your password has been set.')
            return redirect(url_for('account.account'))

    billing_summary = subscription_summary(user)
    if request.method == 'GET' and request.args.get('checkout') == 'success':
        # Stripe redirects here before (or just after) the webhook arrives.
        if not billing_summary['access']:
            flash("Thanks for subscribing! It can take a few seconds to show up here; refresh if it doesn't.")
        elif billing_summary['status'] == 'trialing':
            flash('Your free trial has started. Enjoy!')
        else:
            flash('Thanks for subscribing!')

    return render_template(
        'account/account.html',
        form=form,
        has_password=has_password,
        logout_form=LogoutForm(),
        resend_form=ResendVerificationForm(),
        unlink_form=UnlinkGoogleForm(),
        delete_form=DeleteAccountForm(formdata=None),
        google_identity=user.identity('google'),
        billing=billing_summary,
        billing_form=BillingForm(),
    )


def delete_user_everywhere(user):
    """Stop Plaid and Stripe billing, then delete the user (their sign-in methods, desktop
    sessions, subscription row, and bank connections go with them). Returns an error
    message and keeps the account if anything outside this database couldn't be undone."""
    if user.plaid_items:
        if not current_app.config.get('PLAID_ENABLED'):
            return "Bank connections can't be removed right now. Please try again later."
        failed = plaid_api.remove_items_at_plaid(list(user.plaid_items))
        db.session.commit()
        if failed:
            return "We couldn't disconnect your banks from Plaid. Please try again in a few minutes."

    sub = user.subscription
    if sub is not None:
        if current_app.config.get('STRIPE_ENABLED'):
            try:
                billing.delete_customer(sub.stripe_customer_id)
            except stripe.StripeError:
                logger.exception('Deleting Stripe customer failed for user %s', user.id)
                return billing.PAYMENT_PROVIDER_ERROR
        elif billing.is_subscribed(sub):
            return "Your subscription can't be canceled right now. Please try again later."

    user_id = user.id
    db.session.delete(user)
    db.session.commit()
    logger.info('Deleted user %s', user_id)
    return None


@bp.route('/account/delete', methods=['POST'])
@login_required
@limiter.limit('5 per hour')
def delete_account():
    user = current_user._get_current_object()
    form = DeleteAccountForm()
    if not form.validate_on_submit():
        if 'csrf_token' in form.errors:
            abort(400)
        flash('Type your email address to confirm deleting your account.')
        return redirect(url_for('account.account', _anchor='delete-heading'))
    if normalize_email(form.confirm_email.data) != user.email:
        flash("The email address you typed doesn't match your account.")
        return redirect(url_for('account.account', _anchor='delete-heading'))
    if user.password_hash and not user.check_password(form.current_password.data or ''):
        flash('Current password is incorrect.')
        return redirect(url_for('account.account', _anchor='delete-heading'))

    error = delete_user_everywhere(user)
    if error:
        flash(error)
        return redirect(url_for('account.account', _anchor='delete-heading'))
    logout_user()
    flash('Your account has been deleted.')
    return redirect(url_for('auth.login'))
