from datetime import timedelta

import pytest

import analytics
from extensions import db
from models import SiteVisit, utcnow

BROWSER = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 Safari/605.1.15',
    'Accept-Language': 'en-US,en;q=0.9',
}


def visits(app):
    with app.app_context():
        return db.session.scalars(db.select(SiteVisit).order_by(SiteVisit.id)).all()


def test_page_view_is_recorded_without_ip(app, client):
    client.get('/', headers={**BROWSER, 'Referer': 'https://news.ycombinator.com/item?id=1'},
               environ_base={'REMOTE_ADDR': '203.0.113.7'})
    [visit] = visits(app)
    assert (visit.kind, visit.path, visit.is_bot, visit.referrer) == ('page', '/', False, 'news.ycombinator.com')
    assert '203.0.113.7' not in (visit.visitor, visit.user_agent, visit.referrer)


def test_same_site_referrer_is_not_stored(app, client):
    client.get('/about', headers={**BROWSER, 'Referer': 'http://localhost/'})
    assert visits(app)[0].referrer is None


@pytest.mark.parametrize('host, referrer', [
    ('workbenchbudgeting.com', 'https://www.workbenchbudgeting.com/'),
    ('www.workbenchbudgeting.com', 'https://workbenchbudgeting.com/about'),
    ('workbenchbudgeting.com', 'https://WorkbenchBudgeting.com/'),
])
def test_www_and_bare_domain_count_as_the_same_site(app, client, host, referrer):
    client.get('/', headers={**BROWSER, 'Referer': referrer}, base_url=f'https://{host}')
    assert visits(app)[0].referrer is None


def test_lookalike_domain_is_still_external(app, client):
    client.get('/', headers={**BROWSER, 'Referer': 'https://www.workbenchbudgeting.com.example/'},
               base_url='https://workbenchbudgeting.com')
    assert visits(app)[0].referrer == 'www.workbenchbudgeting.com.example'


@pytest.mark.parametrize('headers', [
    {},
    {'User-Agent': 'Googlebot/2.1 (+http://www.google.com/bot.html)', 'Accept-Language': 'en'},
    {'User-Agent': 'curl/8.4.0'},
    {'User-Agent': BROWSER['User-Agent']},  # a browser string without Accept-Language
])
def test_bots_are_flagged(app, client, headers):
    client.get('/', headers=headers)
    assert visits(app)[0].is_bot


def test_download_is_recorded(app, client):
    resp = client.get('/download/windows', headers=BROWSER)
    resp.close()
    [visit] = visits(app)
    assert (visit.kind, visit.path, visit.is_bot) == ('download', '/download/windows', False)


def test_resumed_download_is_not_counted_twice(app, client):
    client.get('/download/windows', headers={**BROWSER, 'Range': 'bytes=0-'}).close()
    client.get('/download/windows', headers={**BROWSER, 'Range': 'bytes=1000-'}).close()
    assert len(visits(app)) == 1


@pytest.mark.parametrize('path', ['/healthz', '/download/linux', '/static/styles.css', '/nope'])
def test_other_requests_are_not_recorded(app, client, path):
    client.get(path, headers=BROWSER).close()
    assert visits(app) == []


def test_visitor_is_stable_within_a_day_and_changes_across_days(app):
    today = utcnow().date()
    with app.app_context():
        first = analytics.visitor_hash('203.0.113.7', 'ua', today)
        assert analytics.visitor_hash('203.0.113.7', 'ua', today) == first
        assert analytics.visitor_hash('203.0.113.7', 'ua', today + timedelta(days=1)) != first
        assert analytics.visitor_hash('203.0.113.8', 'ua', today) != first


def test_recording_failure_still_serves_page(app, client, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError('database down')
    monkeypatch.setattr(analytics, 'visitor_hash', broken)
    assert client.get('/', headers=BROWSER).status_code == 200


def test_old_visits_are_pruned(app, client, monkeypatch):
    with app.app_context():
        db.session.add(SiteVisit(created_at=utcnow() - timedelta(days=analytics.RETENTION_DAYS + 1),
                                 kind='page', path='/', visitor='old', is_bot=False))
        db.session.commit()
    monkeypatch.setattr(analytics, 'PRUNE_ONE_IN', 1)
    client.get('/', headers=BROWSER)
    [visit] = visits(app)
    assert visit.visitor != 'old'


def test_site_stats_command(app, client):
    client.get('/', headers=BROWSER, environ_base={'REMOTE_ADDR': '203.0.113.7'})
    client.get('/about', headers=BROWSER, environ_base={'REMOTE_ADDR': '203.0.113.7'})
    client.get('/', headers=BROWSER, environ_base={'REMOTE_ADDR': '203.0.113.8'})
    client.get('/download/mac-arm', headers=BROWSER, environ_base={'REMOTE_ADDR': '203.0.113.8'}).close()
    client.get('/', headers={'User-Agent': 'AhrefsBot/7.0'})

    result = app.test_cli_runner().invoke(args=['site-stats', '--days', '7'])
    assert result.exit_code == 0, result.output
    today = utcnow().date().isoformat()
    # 2 people, 3 page views, 1 download, 1 bot hit.
    assert f'{today:<12}{2:>9}{3:>7}{1:>11}{1:>10}' in result.output
    assert '1  mac-arm' in result.output
    assert '1  AhrefsBot/7.0' in result.output
    assert 'Downloads (UTC' not in result.output


def test_site_stats_lists_downloads(app, client):
    client.get('/', headers=BROWSER, environ_base={'REMOTE_ADDR': '203.0.113.7'})
    client.get('/download/mac-arm', headers=BROWSER, environ_base={'REMOTE_ADDR': '203.0.113.7'}).close()
    client.get('/download/windows', headers={'User-Agent': 'python-requests/2.32'},
               environ_base={'REMOTE_ADDR': '203.0.113.9'}).close()

    result = app.test_cli_runner().invoke(args=['site-stats', '--days', '7', '--downloads'])
    assert result.exit_code == 0, result.output
    lines = result.output.split('Downloads (UTC')[1].splitlines()
    [person] = [line for line in lines if 'mac-arm' in line]
    [bot] = [line for line in lines if 'windows' in line]
    assert '    1  person -' in person and person.endswith(BROWSER['User-Agent'])
    assert '    0  bot    -' in bot and bot.endswith('python-requests/2.32')
    assert '203.0.113' not in result.output
