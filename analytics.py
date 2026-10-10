"""First-party visit counting: one site_visits row per marketing page view or installer
download. No cookies and no IP addresses are stored; `visitor` is a keyed hash of the IP and
browser that changes every UTC day, so it counts unique visitors per day but can't follow
anyone across days. Read the numbers with `flask site-stats`."""

import hashlib
import hmac
import logging
import random
import re
from collections import Counter, defaultdict
from datetime import timedelta
from urllib.parse import urlsplit

import click
from flask import current_app, request
from flask.cli import with_appcontext

from extensions import db
from models import SiteVisit, utcnow

logger = logging.getLogger(__name__)

PAGE_ENDPOINTS = {'index', 'about', 'privacy', 'terms', 'refunds'}
RETENTION_DAYS = 365
# Old rows are pruned on roughly one recorded visit in this many.
PRUNE_ONE_IN = 200

BOT_PATTERN = re.compile(
    r'bot|crawl|spider|slurp|scrape|preview|monitor|uptime|lighthouse|headless|phantom|'
    r'facebookexternalhit|embedly|curl|wget|httpie|python|aiohttp|httpx|go-http-client|'
    r'java/|okhttp|axios|node-fetch|undici|libwww|scrapy|semrush|ahrefs|mj12|dataforseo',
    re.I,
)


def looks_like_bot(headers):
    user_agent = headers.get('User-Agent', '')
    if not user_agent or BOT_PATTERN.search(user_agent):
        return True
    # Every real browser sends Accept-Language on page loads and downloads; scripts rarely do.
    return not headers.get('Accept-Language')


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


def record_visit(response):
    kind = visit_kind(response)
    if kind is None:
        return response
    now = utcnow()
    user_agent = request.headers.get('User-Agent', '')
    try:
        db.session.add(SiteVisit(
            created_at=now,
            kind=kind,
            path=request.path[:255],
            visitor=visitor_hash(request.remote_addr or '', user_agent, now.date()),
            is_bot=looks_like_bot(request.headers),
            user_agent=user_agent[:255] or None,
            referrer=external_referrer(),
        ))
        if random.randrange(PRUNE_ONE_IN) == 0:
            cutoff = now - timedelta(days=RETENTION_DAYS)
            db.session.execute(db.delete(SiteVisit).where(SiteVisit.created_at < cutoff))
        db.session.commit()
    except Exception:
        # Counting must never break the page.
        db.session.rollback()
        logger.exception('Could not record site visit')
    return response


def init_analytics(app):
    app.after_request(record_visit)
    app.cli.add_command(site_stats_command)


@click.command('site-stats')
@click.option('--days', default=30, show_default=True, help='How many days back to report.')
@click.option('--downloads', 'list_downloads', is_flag=True,
              help='Also list every download with its time, visitor, and user agent.')
@with_appcontext
def site_stats_command(days, list_downloads):
    """Print daily visitors, page views, and downloads, split into people and bots."""
    since = (utcnow() - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    visits = db.session.scalars(
        db.select(SiteVisit).where(SiteVisit.created_at >= since).order_by(SiteVisit.created_at)
    ).all()

    daily = defaultdict(lambda: {'people': set(), 'views': 0, 'downloads': Counter(), 'bot_hits': 0})
    referrers, bot_agents, downloads = Counter(), Counter(), Counter()
    for visit in visits:
        day = daily[visit.created_at.date()]
        if visit.is_bot:
            day['bot_hits'] += 1
            bot_agents[visit.user_agent or '(no user agent)'] += 1
            continue
        day['people'].add(visit.visitor)
        if visit.kind == 'page':
            day['views'] += 1
            if visit.referrer:
                referrers[visit.referrer] += 1
        else:
            platform = visit.path.rsplit('/', 1)[-1]
            day['downloads'][platform] += 1
            downloads[platform] += 1

    click.echo(f'Last {days} day(s), UTC. "Visitors" are unique per day; bots are counted separately.\n')
    click.echo(f'{"Date":<12}{"Visitors":>9}{"Views":>7}{"Downloads":>11}{"Bot hits":>10}')
    totals = Counter()
    for date in sorted(daily):
        day = daily[date]
        row = {'visitors': len(day['people']), 'views': day['views'],
               'downloads': sum(day['downloads'].values()), 'bot_hits': day['bot_hits']}
        totals.update(row)
        click.echo(f'{date.isoformat():<12}{row["visitors"]:>9}{row["views"]:>7}'
                   f'{row["downloads"]:>11}{row["bot_hits"]:>10}')
    click.echo(f'{"Total":<12}{totals["visitors"]:>9}{totals["views"]:>7}'
               f'{totals["downloads"]:>11}{totals["bot_hits"]:>10}')

    for title, counter in (('Downloads by platform (people)', downloads),
                           ('Top referrers (people)', referrers),
                           ('Top bot user agents', bot_agents)):
        if counter:
            click.echo(f'\n{title}:')
            for name, count in counter.most_common(10):
                click.echo(f'{count:>6}  {name}')

    if list_downloads:
        _echo_downloads(visits)


def _echo_downloads(visits):
    """One line per download. `Pages` is how many pages the same visitor viewed that day:
    a person usually opens the landing page first, so 0 often means a script."""
    pages = Counter((v.created_at.date(), v.visitor) for v in visits if v.kind == 'page')
    click.echo('\nDownloads (UTC; visitor IDs match only within a day):')
    click.echo(f'{"Time":<21}{"Platform":<10}{"Visitor":<10}{"Pages":>5}  {"Who":<7}{"Referrer":<24}User agent')
    for visit in visits:
        if visit.kind != 'download':
            continue
        click.echo(
            f'{visit.created_at:%Y-%m-%d %H:%M:%S}  {visit.path.rsplit("/", 1)[-1]:<10}'
            f'{visit.visitor[:8]:<10}{pages[(visit.created_at.date(), visit.visitor)]:>5}  '
            f'{"bot" if visit.is_bot else "person":<7}{visit.referrer or "-":<24}'
            f'{visit.user_agent or "(no user agent)"}'
        )
