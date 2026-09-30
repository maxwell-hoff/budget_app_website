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
