#!/usr/bin/env python3
"""Scrape per-wear Steam prices from csgodatabase.com for every item in CS2 cases.

Output: prices/wear_prices.json
  {
    "generated_at": "...",
    "items": {
      "AK-47 | Redline": {
        "slug": "ak-47-redline",
        "normal":   {"FN": 12.3, "MW": 8.1, "FT": 5.0, "WW": 4.2, "BS": 3.9},
        "stattrak": {"FN": 30.0, ...}
      }
    }
  }

Partial results are flushed to disk periodically so a timeout still leaves data.
"""
import json
import os
import re
import sys
import time
import pathlib
import threading
import datetime
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote

import requests

BASE = 'https://www.csgodatabase.com'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')
CRATES_URL = 'https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/crates.json'
LIMIT = int(os.environ.get('LIMIT') or 0)
WORKERS = int(os.environ.get('WORKERS') or 0)
OUT = pathlib.Path('prices')
OUT.mkdir(exist_ok=True)

WEAR = {
    'Factory New': 'FN',
    'Minimal Wear': 'MW',
    'Field-Tested': 'FT',
    'Well-Worn': 'WW',
    'Battle-Scarred': 'BS',
}
STEAM = re.compile(r'steamcommunity\.com/market/listings/730/([^"\'<>\s\\]+)')
MONEY = re.compile(r'\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)')
SECTIONS = ('skins', 'knives', 'gloves')
STEAM_SEL = 'a[href*="market/listings/730"]'

# Globs are matched by the browser itself, so non-matching requests never enter
# Python. A catch-all route handler is orders of magnitude slower.
ASSET_GLOBS = (
    '**/*.{png,jpg,jpeg,gif,webp,avif,svg,ico,css,woff,woff2,ttf,otf,eot,mp4,webm,mp3}',
    '**/googletagmanager.com/**',
    '**/google-analytics.com/**',
    '**/analytics.google.com/**',
    '**/doubleclick.net/**',
    '**/googlesyndication.com/**',
    '**/adservice.google.*/**',
    '**/adsystem.*/**',
    '**/facebook.net/**',
    '**/connect.facebook.*/**',
    '**/cloudflareinsights.com/**',
    '**/hotjar.com/**',
    '**/clarity.ms/**',
)

ses = requests.Session()
ses.headers.update({'User-Agent': UA, 'Accept-Language': 'en-US,en;q=0.9'})

RENDER = False
_local = threading.local()
_lock = threading.Lock()
_diag = {'left': 30, 'seen': set()}


def fetch_plain(url):
    for i in range(3):
        try:
            r = ses.get(url, timeout=30)
            if r.status_code == 200:
                return r.text
            if r.status_code in (404, 410):
                return ''
        except Exception as exc:
            print('  ! %s %s' % (url, exc))
        time.sleep(1 + i)
    return ''


def _note_request(req):
    """Log the API calls the page makes, so we can hit them directly later."""
    try:
        if req.resource_type not in ('xhr', 'fetch'):
            return
        base = req.url.split('?')[0]
        with _lock:
            if _diag['left'] <= 0 or base in _diag['seen']:
                return
            _diag['seen'].add(base)
            _diag['left'] -= 1
            print('  XHR %s %s' % (req.method, req.url[:220]))
    except Exception:
        pass


def _page():
    """One reusable page per worker thread, with heavy assets blocked."""
    page = getattr(_local, 'page', None)
    if page is None:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch(args=[
            '--no-sandbox',
            '--disable-dev-shm-usage',
            '--disable-gpu',
            '--disable-extensions',
            '--blink-settings=imagesEnabled=false',
        ])
        ctx = browser.new_context(user_agent=UA, java_script_enabled=True)
        for glob in ASSET_GLOBS:
            ctx.route(glob, lambda route: route.abort())
        page = ctx.new_page()
        page.set_default_timeout(25000)
        page.on('request', _note_request)
        _local.pw = pw
        _local.browser = browser
        _local.ctx = ctx
        _local.page = page
    return page


def fetch_rendered(url):
    try:
        page = _page()
        page.goto(url, wait_until='commit', timeout=30000)
        try:
            page.wait_for_selector(STEAM_SEL, timeout=12000, state='attached')
        except Exception:
            pass
        return page.content()
    except Exception as exc:
        print('  ! render %s %s' % (url, str(exc)[:120]))
        try:
            _local.ctx.close()
            _local.browser.close()
            _local.pw.stop()
        except Exception:
            pass
        _local.page = None
        return ''


def fetch(url):
    return fetch_rendered(url) if RENDER else fetch_plain(url)


def parse_prices(html):
    """Pull every Steam market link and read the price inside its anchor text."""
    res = {'normal': {}, 'stattrak': {}}
    matches = list(STEAM.finditer(html))
    for idx, m in enumerate(matches):
        name = unquote(m.group(1)).replace('+', ' ')
        if name.startswith('Souvenir'):
            continue
        wm = re.search(r'\(([^()]+)\)\s*$', name)
        if not wm or wm.group(1) not in WEAR:
            continue
        key = WEAR[wm.group(1)]
        bucket = 'stattrak' if 'StatTrak' in name else 'normal'
        stop = matches[idx + 1].start() if idx + 1 < len(matches) else len(html)
        tail = html[m.end():min(stop, m.end() + 400)]
        pm = MONEY.search(tail)
        if not pm:
            continue
        val = float(pm.group(1).replace(',', ''))
        if key not in res[bucket]:
            res[bucket][key] = val
    return res


def slug_variants(name):
    s = name.replace('\u2605', '').replace('\u2122', '').replace('StatTrak', '')
    s = s.lower().replace('&', 'and').replace('|', ' ')
    a = re.sub(r'[^a-z0-9]+', '-', s).strip('-')
    b = re.sub(r'[^a-z0-9.]+', '-', s).strip('-')
    return [a] if a == b else [a, b]


def guess_sections(name):
    low = name.lower()
    if 'gloves' in low or 'hand wraps' in low:
        return ('gloves', 'skins')
    knife_words = ('knife', 'bayonet', 'karambit', 'daggers')
    if any(w in low for w in knife_words):
        return ('knives', 'skins')
    return ('skins',)


def load_index():
    """Collect canonical slugs from the site index pages, if reachable."""
    known = {}
    for sec in SECTIONS:
        html = fetch('%s/%s' % (BASE, sec))
        if not html:
            continue
        found = re.findall(r'/(%s)/([a-z0-9][a-z0-9.\-]*)/?["\'#?]' % sec, html)
        for s, slug in found:
            known.setdefault(slug, s)
        print('index /%s -> %d slugs' % (sec, len(found)))
    return known


def load_items():
    crates = ses.get(CRATES_URL, timeout=180).json()
    items = {}
    for c in crates:
        if c.get('type') != 'Case':
            continue
        pool = (c.get('contains') or []) + (c.get('contains_rare') or [])
        for it in pool:
            n = it.get('name')
            if n:
                items.setdefault(n, []).append(c.get('name'))
    return items


def candidate_urls(name, index):
    """At most a couple of URLs per item: index hit first, then a guess."""
    urls = []
    for slug in slug_variants(name):
        secs = (index[slug],) if slug in index else guess_sections(name)
        for sec in secs:
            u = '%s/%s/%s/' % (BASE, sec, slug)
            if u not in urls:
                urls.append((u, slug, sec))
    return urls


def scrape_one(name, index):
    for url, slug, sec in candidate_urls(name, index):
        html = fetch(url)
        if not html:
            continue
        got = parse_prices(html)
        if got['normal'] or got['stattrak']:
            got['slug'] = slug
            got['section'] = sec
            return got
    return None


def save(result, missing, final=False):
    payload = {
        'generated_at': datetime.datetime.now(datetime.timezone.utc)
        .strftime('%Y-%m-%dT%H:%M:%SZ'),
        'source': 'csgodatabase.com (Steam prices, per wear)',
        'render_mode': RENDER,
        'complete': final,
        'total': len(result),
        'items': result,
    }
    (OUT / 'wear_prices.json').write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    (OUT / 'unresolved.json').write_text(json.dumps(sorted(missing), ensure_ascii=False, indent=1))


def main():
    global RENDER
    items = load_items()
    names = sorted(items)
    if LIMIT:
        names = names[:LIMIT]
    print('items to scrape: %d' % len(names))

    probe = names[:3]
    index = load_index()
    if not any(scrape_one(n, index) for n in probe):
        print('plain HTTP found no prices -> switching to rendered mode')
        RENDER = True
        index = load_index()
    print('render mode: %s | index slugs: %d' % (RENDER, len(index)))

    result = {}
    missing = []
    done = [0]
    started = time.time()

    def work(name):
        t0 = time.time()
        got = scrape_one(name, index)
        dt = time.time() - t0
        with _lock:
            done[0] += 1
            if got:
                result[name] = got
            else:
                missing.append(name)
            n = done[0]
            if n % 10 == 0:
                rate = n / max(time.time() - started, 1)
                left = (len(names) - n) / rate / 60 if rate else 0
                print('  %d/%d ok=%d missing=%d | last %.1fs | %.2f items/s '
                      '| ~%.0f min left'
                      % (n, len(names), len(result), len(missing), dt, rate, left))
                sys.stdout.flush()
            if n % 100 == 0:
                save(dict(result), list(missing))

    workers = WORKERS or (10 if RENDER else 8)
    print('workers: %d' % workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, names))

    save(result, missing, final=True)
    print('DONE ok=%d missing=%d in %.1f min'
          % (len(result), len(missing), (time.time() - started) / 60))
    if not result:
        sys.exit(1)


if __name__ == '__main__':
    main()
