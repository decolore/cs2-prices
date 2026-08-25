"""Scrape real per-wear prices for the items the free feed capped at $1800.

Plain HTTP requests from GitHub runners get an instant Cloudflare 403, so every
page is opened in a real Chromium instance instead. Only 20 pages are visited,
well below the volume that made the earlier bulk scrape get challenged.

For each marketplace link on a page we decode the item name out of the href,
which tells us the wear and whether it is StatTrak, then keep the lowest price
seen - the same 'lowest price by variant' figure the site shows at the top.
"""
import functools
import json
import pathlib
import random
import re
import sys
import time
import urllib.parse

from playwright.sync_api import sync_playwright

print = functools.partial(print, flush=True)  # noqa: A001 - CI needs live logs

BASE = 'https://www.csgodatabase.com/skins/'
OUT = pathlib.Path('prices')
NAV_TIMEOUT = 60_000
SEL_TIMEOUT = 25_000
PRICE_SEL = 'a[href*="Factory%20New"], a[href*="factory-new"], a[href*="market/listings/730"]'
ATTEMPTS = 2

ITEMS = [
    '\u2605 M9 Bayonet | Ultraviolet',
    '\u2605 M9 Bayonet | Doppler',
    '\u2605 M9 Bayonet | Night',
    '\u2605 Karambit | Fade',
    '\u2605 Karambit | Night',
    '\u2605 Karambit | Autotronic',
    '\u2605 Karambit | Slaughter',
    '\u2605 Butterfly Knife | Doppler',
    '\u2605 Butterfly Knife | Case Hardened',
    '\u2605 Butterfly Knife | Marble Fade',
    '\u2605 Butterfly Knife | Slaughter',
    '\u2605 Butterfly Knife | Gamma Doppler',
    '\u2605 Huntsman Knife | Night',
    '\u2605 Huntsman Knife | Forest DDPAT',
    '\u2605 Bowie Knife | Crimson Web',
    '\u2605 Flip Knife | Crimson Web',
    '\u2605 Nomad Knife | Urban Masked',
    '\u2605 Driver Gloves | Crimson Weave',
    '\u2605 Driver Gloves | Black Tie',
    '\u2605 Sport Gloves | Hedge Maze',
]

WEARS = {
    'Factory New': 'FN',
    'Minimal Wear': 'MW',
    'Field-Tested': 'FT',
    'Well-Worn': 'WW',
    'Battle-Scarred': 'BS',
}

ANCHOR = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
MONEY = re.compile(r'\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)')
TAGS = re.compile(r'<[^>]+>')
BLOCK_GLOBS = ('**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf,mp4}',)


def slug(name):
    s = name.replace('\u2605', '').replace('|', '').replace('\u2122', '')
    s = re.sub(r'\s+', ' ', s).strip().lower()
    return s.replace(' ', '-')


def parse(html):
    """Lowest price per variant/wear across every marketplace link on the page."""
    found = {'normal': {}, 'stattrak': {}}
    for href, inner in ANCHOR.findall(html):
        money = MONEY.search(TAGS.sub(' ', inner))
        if not money:
            continue
        price = float(money.group(1).replace(',', ''))
        if price <= 0:
            continue
        low = urllib.parse.unquote(href).lower()
        wear = None
        for label, key in WEARS.items():
            if label.lower() in low or label.lower().replace(' ', '-') in low:
                wear = key
                break
        if wear is None:
            continue
        group = 'stattrak' if 'stattrak' in low else 'normal'
        current = found[group].get(wear)
        if current is None or price < current:
            found[group][wear] = price
    return found


def new_context(browser):
    ctx = browser.new_context(
        viewport={'width': 1280, 'height': 900},
        locale='en-US',
        user_agent=('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                    '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'),
    )
    for glob in BLOCK_GLOBS:
        ctx.route(glob, lambda route: route.abort())
    return ctx


def scrape_one(ctx, url):
    page = ctx.new_page()
    page.set_default_timeout(SEL_TIMEOUT)
    try:
        page.goto(url, wait_until='domcontentloaded', timeout=NAV_TIMEOUT)
        try:
            page.wait_for_selector(PRICE_SEL, timeout=SEL_TIMEOUT, state='attached')
        except Exception:
            print('   price links did not appear, parsing whatever loaded')
        return page.content()
    finally:
        page.close()


def main():
    OUT.mkdir(exist_ok=True)
    result, missing = {}, []
    started = time.time()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=[
            '--no-sandbox',
            '--disable-dev-shm-usage',
            '--disable-gpu',
            '--disable-extensions',
            '--blink-settings=imagesEnabled=false',
        ])
        ctx = new_context(browser)

        for i, name in enumerate(ITEMS, 1):
            url = BASE + slug(name) + '/'
            print('[%2d/%d] %s' % (i, len(ITEMS), url))
            prices = None
            for attempt in range(1, ATTEMPTS + 1):
                try:
                    html = scrape_one(ctx, url)
                except Exception as exc:
                    print('   attempt %d failed: %s' % (attempt, exc))
                    ctx.close()
                    ctx = new_context(browser)
                    continue
                if 'challenge-platform' in html and '$' not in html:
                    print('   attempt %d hit a Cloudflare challenge' % attempt)
                    time.sleep(10)
                    continue
                candidate = parse(html)
                if candidate['normal'] or candidate['stattrak']:
                    prices = candidate
                    break
                print('   attempt %d parsed no prices (%d bytes)' % (attempt, len(html)))

            if prices:
                result[name] = prices
                print('   normal   %s' % prices['normal'])
                print('   stattrak %s' % prices['stattrak'])
            else:
                missing.append(name)

            payload = {'source': 'csgodatabase.com lowest price per variant',
                       'items': result, 'missing': missing}
            (OUT / 'capped_fix.json').write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
            print('   elapsed %.0fs | ok %d | missing %d'
                  % (time.time() - started, len(result), len(missing)))
            if i < len(ITEMS):
                time.sleep(random.uniform(2.0, 4.0))

        ctx.close()
        browser.close()

    print('\nresolved: %d / %d | missing: %s' % (len(result), len(ITEMS), missing))
    print('--- copy everything below this line ---')
    print(json.dumps({'items': result, 'missing': missing}, ensure_ascii=False))
    print('--- end ---')
    if not result:
        sys.exit(1)


if __name__ == '__main__':
    main()
