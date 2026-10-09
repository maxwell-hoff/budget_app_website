import json
import logging
import urllib.request

from flask import current_app

logger = logging.getLogger(__name__)

RESEND_URL = 'https://api.resend.com/emails'
# Cloudflare in front of Resend rejects urllib's default "Python-urllib/x.y" with a 403 (error 1010).
USER_AGENT = 'workbench-budgeting/1.0'


def send_email(to, subject, text):
    """Send a plain-text email using the configured backend.

    Backends: ``resend`` (production), ``console`` (dev: prints the email),
    ``memory`` (tests: appends to ``app.extensions['mail_outbox']``).
    """
    backend = current_app.config['EMAIL_BACKEND']
    message = {'to': to, 'subject': subject, 'text': text}

    if backend == 'memory':
        current_app.extensions.setdefault('mail_outbox', []).append(message)
    elif backend == 'console':
        print(f"\n=== EMAIL to {to} ===\nSubject: {subject}\n\n{text}\n=== END EMAIL ===\n", flush=True)
    elif backend == 'resend':
        _send_resend(message)
    else:
        raise ValueError(f'Unknown EMAIL_BACKEND: {backend}')


def _send_resend(message):
    payload = {
        'from': current_app.config['EMAIL_FROM'],
        'to': [message['to']],
        'subject': message['subject'],
        'text': message['text'],
    }
    request = urllib.request.Request(
        RESEND_URL,
        data=json.dumps(payload).encode(),
        headers={
            'Authorization': f"Bearer {current_app.config['RESEND_API_KEY']}",
            'Content-Type': 'application/json',
            'User-Agent': USER_AGENT,
        },
        method='POST',
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read()


def try_send_email(to, subject, text):
    """Like send_email, but logs failures instead of raising, so a provider outage
    doesn't break the page that triggered the email."""
    try:
        send_email(to, subject, text)
        return True
    except Exception:
        logger.exception('Failed to send email "%s"', subject)
        return False
