"""First-party visit counting: one site_visits row per marketing page view or installer
download, plus an `engaged` row the first time a page's visitor scrolls, clicks, taps, or
types (sent by _engagement.html). No cookies and no IP addresses are stored; `visitor` is a
keyed hash of the IP and browser that changes every UTC day, so it counts unique visitors
per day but can't follow anyone across days. Read the numbers with `flask site-stats`.

Each visitor-day is classed as:
  bot         its user agent names a bot, tool, link previewer, or long-dead browser, or a
              request came without Accept-Language
  human       it interacted with a page (a real person, or a bot built to fake input)
  unverified  everything else: bots wearing a browser user agent, and people who left
              without touching the page or who block scripts
"""

import hashlib
import hmac
import json
import logging
import random
import re
from collections import Counter, defaultdict
from datetime import timedelta
from urllib.parse import urlsplit

import click
from flask import current_app, request, url_for
from flask.cli import with_appcontext

from extensions import db, limiter
from models import SiteVisit, utcnow

logger = logging.getLogger(__name__)

PAGE_ENDPOINTS = {'index', 'about', 'privacy', 'terms', 'refunds'}
RETENTION_DAYS = 365
# Old rows are pruned on roughly one recorded visit in this many.
PRUNE_ONE_IN = 200

BOT_PATTERN = re.compile(
    # Self-declared crawlers, link previewers, monitors, and AI fetchers.
    r'bot|crawl|spider|slurp|scrape|preview|monitor|uptime|lighthouse|headless|phantom|'
    r'facebookexternalhit|facebookcatalog|meta-external|embedly|iframely|whatsapp|mastodon|'
    r'pleroma|misskey|akkoma|cardyb|bluesky|google-|googleother|mediapartners|feedfetcher|'
    r'feedly|archive|chatgpt|oai-search|perplexity|claude-|anthropic|cohere|diffbot|'
    r'semrush|ahrefs|mj12|dataforseo|pingdom|statuscake|site24x7|datadog|newrelic|'
    r'health.?check|synthetic|validator|checker|'
    # HTTP libraries and command-line tools.
    r'curl|wget|httpie|python|aiohttp|httpx|go-http-client|java/|okhttp|axios|node-fetch|'
    r'undici|libwww|scrapy|http\.rb|ruby|perl|php|guzzle|reqwest|deno/|bun/|'
    # Vulnerability scanners.
    r'nuclei|zgrab|masscan|censys|expanse|nmap|l9explore|scan|'
    # Browsers nobody runs any more, a common disguise for scripts.
    r'msie |trident/|windows nt 5\.|chrome/[1-9]\d?\.|firefox/[1-9]\d?\.',
    re.I,
)

# A trailing dot matches any top-level domain (google.com, google.co.uk).
SOURCE_GROUPS = (
    ('X/Twitter', ('t.co', 'twitter.com', 'x.com')),
    ('Hacker News', ('news.ycombinator.com', 'hn.algolia.com')),
    ('Reddit', ('reddit.com',)),
    ('LinkedIn', ('linkedin.com', 'lnkd.in')),
    ('Search', ('google.', 'bing.com', 'duckduckgo.com', 'search.brave.com', 'yahoo.',
                'ecosia.org', 'kagi.com', 'startpage.com')),
)
OTHER = 'Other sites'
DIRECT = 'Direct'


def looks_like_bot(headers):
    if is_bot_agent(headers.get('User-Agent', '')):
        return True
    # Every real browser sends Accept-Language on page loads and downloads; scripts rarely do.
    return not headers.get('Accept-Language')


def is_bot_agent(user_agent):
    return not user_agent or bool(BOT_PATTERN.search(user_agent))


def visitor_hash(ip, user_agent, day):
    key = current_app.config['SECRET_KEY'].encode()
    message = f'{day.isoformat()}|{ip}|{user_agent}'.encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:16]


def _without_www(host):
    host = host.lower()
    return host[4:] if host.startswith('www.') else host


def external_referrer():
    host = urlsplit(request.referrer or '').hostname
    # www and the bare domain are the same site.
    if not host or _without_www(host) == _without_www(request.host.split(':')[0]):
        return None
    return host[:255]


def source_group(host):
    if not host:
        return DIRECT
    dotted = f'.{_without_www(host)}'
    for name, domains in SOURCE_GROUPS:
        for domain in domains:
            if domain.endswith('.') and f'.{domain}' in dotted:
                return name
            if dotted.endswith(f'.{domain}'):
                return name
    return OTHER


def visit_kind(response):
    if request.method != 'GET':
        return None
    if request.endpoint in PAGE_ENDPOINTS and response.status_code == 200:
        return 'page'
    if request.endpoint == 'download' and response.status_code in (200, 206):
        # Resumed downloads ask for the rest of the file; only the first request counts.
        range_header = request.headers.get('Range', '')
        if not range_header or range_header.replace(' ', '').startswith('bytes=0-'):
            return 'download'
    return None


def _add_visit(kind, path, referrer):
    now = utcnow()
    user_agent = request.headers.get('User-Agent', '')
    try:
        db.session.add(SiteVisit(
            created_at=now,
            kind=kind,
            path=path[:255],
            visitor=visitor_hash(request.remote_addr or '', user_agent, now.date()),
            is_bot=looks_like_bot(request.headers),
            user_agent=user_agent[:255] or None,
            referrer=referrer,
        ))
        if random.randrange(PRUNE_ONE_IN) == 0:
            cutoff = now - timedelta(days=RETENTION_DAYS)
            db.session.execute(db.delete(SiteVisit).where(SiteVisit.created_at < cutoff))
        db.session.commit()
    except Exception:
        # Counting must never break the page.
        db.session.rollback()
        logger.exception('Could not record site visit')


def record_visit(response):
    kind = visit_kind(response)
    if kind is not None:
        _add_visit(kind, request.path, external_referrer())
    return response


def engaged():
    """The page's visitor scrolled, clicked, tapped, or typed (sent once per page load)."""
    try:
        path = json.loads(request.get_data(as_text=True) or '{}').get('path')
    except (ValueError, AttributeError):
        path = None
    if isinstance(path, str) and path in {url_for(endpoint) for endpoint in PAGE_ENDPOINTS}:
        _add_visit('engaged', path, None)
    return '', 204


def init_analytics(app):
    app.after_request(record_visit)
    app.add_url_rule('/visit/engaged', 'engaged', limiter.limit('30 per minute')(engaged),
                     methods=['POST'])
    app.cli.add_command(site_stats_command)


# --- Report --------------------------------------------------------------------------


def classify(visits):
    """Group time-ordered visits into visitor-days: {(date, visitor): entry}, where entry has
    class, source, host (first outside referrer), first (time), pages, downloads, agent."""
    days = {}
    for visit in visits:
        entry = days.setdefault((visit.created_at.date(), visit.visitor), {
            'bot': False, 'engaged': False, 'host': None, 'first': visit.created_at,
            'pages': 0, 'downloads': [], 'agent': visit.user_agent,
        })
        # Bot patterns are re-applied to stored user agents, so improving them fixes past reports.
        entry['bot'] |= visit.is_bot or is_bot_agent(visit.user_agent)
        if visit.kind == 'engaged':
            entry['engaged'] = True
        elif visit.kind == 'page':
            entry['pages'] += 1
            entry['host'] = entry['host'] or visit.referrer
        elif visit.kind == 'download':
            entry['downloads'].append(visit)
    for entry in days.values():
        entry['class'] = 'bot' if entry['bot'] else 'human' if entry['engaged'] else 'unverified'
        entry['source'] = source_group(entry['host'])
    return days


def _platform(visit):
    return visit.path.rsplit('/', 1)[-1]


def _table(header, rows, widths):
    """First column left-aligned, the rest right-aligned."""
    def line(cells):
        first, *rest = (str(cell) for cell in cells)
        return f'{first:<{widths[0]}}' + ''.join(f'{cell:>{w}}' for cell, w in zip(rest, widths[1:]))
    click.echo(line(header))
    for row in rows:
        click.echo(line(row))


@click.command('site-stats')
@click.option('--days', default=30, show_default=True, help='How many days back to report.')
@click.option('--downloads', 'list_downloads', is_flag=True,
              help='Also list every download with its time, class, referrer, and user agent.')
@click.option('--visitors', 'list_visitors', is_flag=True,
              help='Also list every non-bot visitor-day with its first time, class, referrer, and user agent.')
@with_appcontext
def site_stats_command(days, list_downloads, list_visitors):
    """Print daily visitors and downloads split into humans, unverified, and bots, by source."""
    since = (utcnow() - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    visits = db.session.scalars(
        db.select(SiteVisit).where(SiteVisit.created_at >= since).order_by(SiteVisit.created_at, SiteVisit.id)
    ).all()
    visitor_days = classify(visits)
    dates = sorted({date for date, _ in visitor_days})

    click.echo(f'Last {days} day(s), UTC. A visitor is one browser on one IP address for one day.')
    click.echo('  human       scrolled, clicked, tapped, or typed on a page')
    click.echo('  unverified  looked like a browser but never interacted: bots with a browser user agent,')
    click.echo('              or people who left right away or block scripts')
    click.echo('  bot         bot/tool user agent, link previewer, long-dead browser, or no Accept-Language')
    tracking_start = db.session.scalar(
        db.select(db.func.min(SiteVisit.created_at)).where(SiteVisit.kind == 'engaged'))
    if tracking_start is None:
        click.echo('No interactions recorded yet, so every non-bot visitor shows as unverified.')
    elif dates and tracking_start.date() > dates[0]:
        click.echo(f'Interaction tracking started {tracking_start.date().isoformat()}; '
                   'earlier days show every non-bot visitor as unverified.')

    # Every visitor-day has exactly one class, so the class columns always add up to the totals.
    daily = defaultdict(Counter)
    for (date, _), entry in visitor_days.items():
        row = daily[date]
        row['views'] += entry['pages']
        row[f'views_{entry["class"]}'] += entry['pages']
        row['dl'] += len(entry['downloads'])
        row[f'dl_{entry["class"]}'] += len(entry['downloads'])
    keys = ('views', 'views_human', 'views_bot', 'views_unverified', 'dl', 'dl_human', 'dl_bot', 'dl_unverified')
    totals = sum(daily.values(), Counter())
    click.echo('\nPage views and downloads by day:')
    _table(('Date', 'Total views', 'Human views', 'Bot views', 'Unverified views',
            'Total DL', 'Human DL', 'Bot DL', 'Unverified DL'),
           [(d.isoformat(), *(daily[d][k] for k in keys)) for d in dates]
           + [('Total', *(totals[k] for k in keys))],
           (12, 13, 13, 11, 18, 10, 10, 8, 15))

    by_source = defaultdict(Counter)
    per_day = defaultdict(Counter)
    for (date, _), entry in visitor_days.items():
        by_source[entry['source']][entry['class']] += 1
        per_day[(date, entry['source'])][entry['class']] += 1
        if entry['class'] == 'human':
            by_source[entry['source']]['dl_human'] += len(entry['downloads'])
    sources = [s for s in [name for name, _ in SOURCE_GROUPS] + [OTHER, DIRECT] if by_source[s]]
    if sources:
        click.echo('\nVisitors by source (the first outside site that linked them here that day):')
        _table(('Source', 'Humans', 'Unverified', 'Bots', 'DL human'),
               [(s, *(by_source[s][k] for k in ('human', 'unverified', 'bot', 'dl_human'))) for s in sources],
               (14, 7, 11, 6, 10))

    linked = [s for s in sources if s != DIRECT and (by_source[s]['human'] or by_source[s]['unverified'])]
    if linked:
        click.echo('\nNon-bot visitors by source and day, as humans/unverified:')
        rows = []
        for date in dates:
            cells = [per_day[(date, s)] for s in linked]
            if any(c['human'] or c['unverified'] for c in cells):
                rows.append((date.isoformat(), *(f'{c["human"]}/{c["unverified"]}' for c in cells)))
        _table(('Date', *linked), rows, (12, *(max(len(s), 7) + 2 for s in linked)))

    hosts = defaultdict(Counter)
    for entry in visitor_days.values():
        if entry['host']:
            hosts[entry['host']][entry['class']] += 1
    if hosts:
        click.echo('\nTop referring sites:')
        top = sorted(hosts.items(), key=lambda item: -sum(item[1].values()))[:15]
        _table(('Site', 'Humans', 'Unverified', 'Bots'),
               [(h, c['human'], c['unverified'], c['bot']) for h, c in top], (28, 7, 11, 6))

    platforms = defaultdict(Counter)
    for entry in visitor_days.values():
        for visit in entry['downloads']:
            platforms[_platform(visit)][entry['class']] += 1
    if platforms:
        click.echo('\nDownloads by platform:')
        _table(('Platform', 'Human', 'Unverified', 'Bot'),
               [(p, c['human'], c['unverified'], c['bot'])
                for p, c in sorted(platforms.items(), key=lambda item: -sum(item[1].values()))],
               (12, 6, 11, 5))

    for title, cls in (('Top unverified user agents (look here for disguised bots)', 'unverified'),
                       ('Top bot user agents', 'bot')):
        agents = Counter(e['agent'] or '(no user agent)' for e in visitor_days.values() if e['class'] == cls)
        if agents:
            click.echo(f'\n{title}, by visitor-days:')
            for name, count in agents.most_common(10):
                click.echo(f'{count:>6}  {name}')

    if list_visitors:
        _echo_visitors(visitor_days)
    if list_downloads:
        _echo_downloads(visitor_days)


def _echo_visitors(visitor_days):
    click.echo('\nNon-bot visitors (UTC, first time seen that day; visitor IDs match only within a day):')
    click.echo(f'{"First seen":<21}{"Visitor":<10}{"Class":<12}{"Pages":>5}{"DLs":>5}  {"Referrer":<24}User agent')
    for (_, visitor), e in sorted(visitor_days.items(), key=lambda item: item[1]['first']):
        if e['class'] != 'bot':
            click.echo(f'{e["first"]:%Y-%m-%d %H:%M:%S}  {visitor[:8]:<10}{e["class"]:<12}{e["pages"]:>5}'
                       f'{len(e["downloads"]):>5}  {e["host"] or "-":<24}{e["agent"] or "(no user agent)"}')


def _echo_downloads(visitor_days):
    """One line per download. `Pages` is how many pages the same visitor viewed that day."""
    rows = [(visit, visitor, e) for (_, visitor), e in visitor_days.items() for visit in e['downloads']]
    click.echo('\nDownloads (UTC; visitor IDs match only within a day):')
    click.echo(f'{"Time":<21}{"Platform":<10}{"Visitor":<10}{"Pages":>5}  {"Class":<12}{"Referrer":<24}User agent')
    for visit, visitor, e in sorted(rows, key=lambda row: (row[0].created_at, row[0].id)):
        click.echo(
            f'{visit.created_at:%Y-%m-%d %H:%M:%S}  {_platform(visit):<10}{visitor[:8]:<10}'
            f'{e["pages"]:>5}  {e["class"]:<12}{e["host"] or "-":<24}'
            f'{visit.user_agent or "(no user agent)"}'
        )
