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


def engage(client, ip, path='/', headers=BROWSER):
    return client.post('/visit/engaged', data='{"path": "%s"}' % path, content_type='text/plain',
                       headers=headers, environ_base={'REMOTE_ADDR': ip})


def site_stats(app, *args):
    result = app.test_cli_runner().invoke(args=['site-stats', '--days', '7', *args])
    assert result.exit_code == 0, result.output
    return result.output


def section(output, title):
    return output.split(title, 1)[1].split('\n\n', 1)[0]


def daily_row(date, *counts):
    """A by-day row: total/human/bot/unverified views, then total/human/bot/unverified downloads."""
    return f'{date:<12}' + ''.join(f'{c:>{w}}' for c, w in zip(counts, (13, 13, 11, 18, 10, 10, 8, 15)))


def test_engagement_is_recorded(app, client):
    assert engage(client, '203.0.113.7', '/about').status_code == 204
    [visit] = visits(app)
    assert (visit.kind, visit.path, visit.is_bot, visit.referrer) == ('engaged', '/about', False, None)


@pytest.mark.parametrize('body', ['{"path": "/nope"}', '{"path": 3}', 'not json', '[1]', ''])
def test_engagement_ignores_unknown_pages_and_junk(app, client, body):
    assert client.post('/visit/engaged', data=body, headers=BROWSER).status_code == 204
    assert visits(app) == []


def test_pages_include_the_engagement_script(client):
    for path in ('/', '/about', '/privacy'):
        page = client.get(path, headers=BROWSER).get_data(as_text=True)
        assert '/visit/engaged' in page and 'isTrusted' in page


@pytest.mark.parametrize('user_agent', [
    'WhatsApp/2.23.20.0',
    'Mozilla/5.0 (compatible; MSIE 9.0; Windows NT 6.1; Trident/5.0)',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/79.0.3945.88 Safari/537.36',
    'Mastodon/4.3.0 (http.rb/5.2.0; +https://mastodon.social/)',
    'Mozilla/5.0 (compatible; Google-InspectionTool/1.0)',
    'Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; ChatGPT-User/1.0)',
])
def test_more_bots_are_recognized(user_agent):
    assert analytics.is_bot_agent(user_agent)


@pytest.mark.parametrize('user_agent', [
    BROWSER['User-Agent'],
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/142.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64; rv:144.0) Gecko/20100101 Firefox/144.0',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) '
    'Mobile/15E148 Twitter for iPhone/11.30',
])
def test_browsers_are_not_bots(user_agent):
    assert not analytics.is_bot_agent(user_agent)


@pytest.mark.parametrize('host, group', [
    (None, 'Direct'), ('t.co', 'X/Twitter'), ('mobile.twitter.com', 'X/Twitter'), ('x.com', 'X/Twitter'),
    ('news.ycombinator.com', 'Hacker News'), ('old.reddit.com', 'Reddit'), ('www.google.co.uk', 'Search'),
    ('notgoogle.com', 'Other sites'), ('xt.co', 'Other sites'), ('example.com', 'Other sites'),
])
def test_source_group(host, group):
    assert analytics.source_group(host) == group


def test_site_stats_splits_humans_unverified_and_bots(app, client):
    # A person from X who views two pages, interacts, and downloads.
    client.get('/', headers={**BROWSER, 'Referer': 'https://t.co/abc'}, environ_base={'REMOTE_ADDR': '203.0.113.7'})
    client.get('/about', headers=BROWSER, environ_base={'REMOTE_ADDR': '203.0.113.7'})
    engage(client, '203.0.113.7')
    client.get('/download/mac-arm', headers=BROWSER, environ_base={'REMOTE_ADDR': '203.0.113.7'}).close()
    # A browser-looking visitor from Hacker News that never interacts, then downloads.
    client.get('/', headers={**BROWSER, 'Referer': 'https://news.ycombinator.com/item?id=1'},
               environ_base={'REMOTE_ADDR': '203.0.113.8'})
    client.get('/download/windows', headers=BROWSER, environ_base={'REMOTE_ADDR': '203.0.113.8'}).close()
    # Bots: a declared crawler following the X link, and a script downloading.
    client.get('/', headers={'User-Agent': 'AhrefsBot/7.0', 'Referer': 'https://t.co/abc'})
    client.get('/download/windows', headers={'User-Agent': 'python-requests/2.32'},
               environ_base={'REMOTE_ADDR': '203.0.113.9'}).close()

    output = site_stats(app)
    today = utcnow().date().isoformat()
    daily = section(output, 'Page views and downloads by day:')
    # Views: 2 human, 1 bot (the crawler), 1 unverified. Downloads: 1 of each.
    assert daily_row(today, 4, 2, 1, 1, 3, 1, 1, 1) in daily
    assert daily_row('Total', 4, 2, 1, 1, 3, 1, 1, 1) in daily
    sources = section(output, 'Visitors by source')
    assert f'{"X/Twitter":<14}{1:>7}{0:>11}{1:>6}{1:>10}' in sources
    assert f'{"Hacker News":<14}{0:>7}{1:>11}{0:>6}{0:>10}' in sources
    assert f'{"Direct":<14}{0:>7}{0:>11}{1:>6}{0:>10}' in sources
    by_day = section(output, 'by source and day')
    assert today in by_day and '1/0' in by_day and '0/1' in by_day
    platforms = section(output, 'Downloads by platform:')
    assert f'{"windows":<12}{0:>6}{1:>11}{1:>5}' in platforms
    assert f'{"mac-arm":<12}{1:>6}{0:>11}{0:>5}' in platforms
    assert '1  AhrefsBot/7.0' in output
    assert 'Downloads (UTC' not in output and 'Non-bot visitors (UTC' not in output
    assert '203.0.113' not in output


def test_site_stats_notes_when_nothing_has_interacted(app, client):
    client.get('/', headers=BROWSER)
    output = site_stats(app)
    assert 'No interactions recorded yet' in output
    assert daily_row(utcnow().date().isoformat(), 1, 0, 0, 1, 0, 0, 0, 0) in output


def test_site_stats_reclassifies_old_rows_with_current_bot_patterns(app):
    with app.app_context():
        db.session.add(SiteVisit(kind='page', path='/', visitor='v1', is_bot=False,
                                 user_agent='WhatsApp/2.23.20.0'))
        db.session.commit()
    output = site_stats(app)
    assert daily_row(utcnow().date().isoformat(), 1, 0, 1, 0, 0, 0, 0, 0) in output


def test_site_stats_lists_visitors_and_downloads(app, client):
    client.get('/', headers={**BROWSER, 'Referer': 'https://t.co/abc'}, environ_base={'REMOTE_ADDR': '203.0.113.7'})
    engage(client, '203.0.113.7')
    client.get('/download/mac-arm', headers=BROWSER, environ_base={'REMOTE_ADDR': '203.0.113.7'}).close()
    crawler = {**BROWSER, 'User-Agent': 'Mozilla/5.0 (Macintosh) Chrome/142.0.0.0 Safari/537.36'}
    client.get('/download/windows', headers=crawler, environ_base={'REMOTE_ADDR': '198.51.100.1'}).close()
    client.get('/download/windows', headers={'User-Agent': 'python-requests/2.32'},
               environ_base={'REMOTE_ADDR': '203.0.113.9'}).close()

    output = site_stats(app, '--downloads', '--visitors')
    people = section(output, 'Non-bot visitors (UTC').splitlines()[2:]
    assert len(people) == 2
    [human] = [line for line in people if 'human' in line]
    assert '    1    1  t.co' in human and human.endswith(BROWSER['User-Agent'])
    [unverified] = [line for line in people if 'unverified' in line]
    assert '    0    1  -' in unverified

    lines = output.split('Downloads (UTC')[1].splitlines()
    [person] = [line for line in lines if 'mac-arm' in line]
    [crawled, script] = [line for line in lines if 'windows' in line]
    assert '    1  human       t.co' in person and person.endswith(BROWSER['User-Agent'])
    assert '    0  unverified  -' in crawled
    assert '    0  bot         -' in script and script.endswith('python-requests/2.32')
    assert '203.0.113' not in output and '198.51.100' not in output
