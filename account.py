from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user
from wtforms import PasswordField
from wtforms.validators import Length, Optional

from auth import MAX_PASSWORD_LENGTH, LogoutForm, NewPasswordForm, ResendVerificationForm
from billing import CheckoutForm, is_subscribed
from extensions import db, limiter
from google_auth import UnlinkGoogleForm

# Only registered when ACCOUNTS_ENABLED is on (see create_app), so every route 404s otherwise.
bp = Blueprint('account', __name__)


class ChangePasswordForm(NewPasswordForm):
    # Optional because Google-only users (step 7) have no password yet; checked in the view.
    current_password = PasswordField('Current password', validators=[Optional(), Length(max=MAX_PASSWORD_LENGTH)])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.password.label.text = 'New password'
        self.confirm.label.text = 'Confirm new password'


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

    subscribed = is_subscribed(user.subscription)
    if request.method == 'GET' and request.args.get('checkout') == 'success':
        # Stripe redirects here before (or just after) the webhook arrives.
        flash('Thanks for subscribing!' if subscribed else
              "Thanks for subscribing! It can take a few seconds to show up here; refresh if it doesn't.")

    return render_template(
        'account/account.html',
        form=form,
        has_password=has_password,
        logout_form=LogoutForm(),
        resend_form=ResendVerificationForm(),
        unlink_form=UnlinkGoogleForm(),
        google_identity=user.identity('google'),
        subscription=user.subscription,
        subscribed=subscribed,
        checkout_form=CheckoutForm(),
    )
