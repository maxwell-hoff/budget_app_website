import pytest
from flask import Flask


def test_gunicorn_target_is_a_flask_app():
    import serve
    assert isinstance(serve.app, Flask)


def test_index_renders(client):
    resp = client.get('/')
    assert resp.status_code == 200
    assert b'Workbench' in resp.data


def test_about_renders(client):
    resp = client.get('/about')
    assert resp.status_code == 200
    assert b'Workbench' in resp.data


LEGAL_LINKS = (b'href="/privacy"', b'href="/terms"', b'href="/refunds"')


def footer(html):
    start = html.index(b'<footer')
    return html[start:html.index(b'</footer>', start)]


@pytest.mark.parametrize('path, heading, phrases', [
    ('/privacy', b'Privacy Policy', [b'plaid.com/legal/#end-user-privacy-policy', b'stored on your own computer',
                                     b'store your accounts or transactions on our servers', b'Limited Use']),
    ('/terms', b'Terms of Service', [b'$8.99 per month', b'Sample profile', b'one free week',
                                     b'your card is charged $8.99', b'nothing on your computer is deleted']),
    ('/refunds', b'Refund Policy', [b'within 7 days of the charge', b"don't refund partial months"]),
])
def test_legal_pages_render(client, path, heading, phrases):
    resp = client.get(path)
    assert resp.status_code == 200
    assert b'<h1 class="about-hero__title">' + heading + b'</h1>' in resp.data
    assert b'support@workbenchbudgeting.com' in resp.data
    assert b'Last updated' in resp.data
    for phrase in phrases:
        assert phrase in resp.data, phrase
    assert b'noindex' not in resp.data
    assert f'href="{path}" aria-current="page"'.encode() in footer(resp.data)


@pytest.mark.parametrize('path', ['/privacy', '/terms', '/refunds'])
def test_legal_pages_are_public_with_accounts_on(accounts_client, path):
    assert accounts_client.get(path).status_code == 200


def html_pages(client):
    """Every GET route without URL parameters that answers with an HTML page."""
    pages = {}
    for rule in client.application.url_map.iter_rules():
        if 'GET' not in rule.methods or rule.arguments or rule.endpoint == 'static':
            continue
        resp = client.get(rule.rule)
        if resp.mimetype == 'text/html' and resp.status_code in (200, 400):
            pages[rule.rule] = resp.data
        resp.close()
    return pages


def assert_legal_links(pages):
    for path, html in pages.items():
        for link in LEGAL_LINKS:
            assert link in footer(html), (path, link)


def test_every_page_footer_links_legal_pages(client):
    pages = html_pages(client)
    assert {'/', '/about', '/privacy', '/terms', '/refunds'} <= set(pages)
    assert_legal_links(pages)


def test_every_account_page_footer_links_legal_pages(accounts_client, make_user):
    signed_out = html_pages(accounts_client)
    assert {'/login', '/signup', '/forgot-password'} <= set(signed_out)
    assert_legal_links(signed_out)

    make_user()
    accounts_client.post('/login', data={'email': 'user@example.com', 'password': 'correct horse battery'})
    signed_in = html_pages(accounts_client)
    assert {'/account', '/app-login'} <= set(signed_in)
    assert_legal_links(signed_in)

    assert_legal_links({'/reset-password/bad': accounts_client.get('/reset-password/bad').data})


@pytest.mark.parametrize('platform', ['mac-arm', 'mac-x64', 'windows'])
def test_download_serves_installer(client, platform):
    resp = client.get(f'/download/{platform}')
    try:
        assert resp.status_code == 200
        assert resp.headers['Content-Disposition'].startswith('attachment;')
    finally:
        resp.close()


def test_download_unknown_platform_404(client):
    assert client.get('/download/linux').status_code == 404


def test_notify_accepts_email(client):
    resp = client.post('/notify', json={'email': 'someone@example.com'})
    assert resp.status_code == 200
    assert resp.get_json() == {'ok': True}


def test_notify_rejects_bad_email(client):
    resp = client.post('/notify', json={'email': 'nope'})
    assert resp.status_code == 400
