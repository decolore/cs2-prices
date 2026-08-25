"""Scrape real per-wear prices for the few items the free feed capped at $1800.

Only 20 pages are requested, so csgodatabase does not trigger Cloudflare here.
For every marketplace link on a page we decode the item name out of the href,
which tells us the wear and whether it is StatTrak, then keep the lowest price
seen for that combination - the same 'lowest price by variant' number the site
shows at the top of each page.
"""
import functools
import json
import pathlib
import random
import re
import sys
import time
import urllib.parse

import requests

print = functools.partial(print, flush=True)  # noqa: A001 - CI needs live logs

BASE = 'https://www.csgodatabase.com/skins/'
OUT = pathlib.Path('prices')
TIMEOUT = 20
ATTEMPTS = 2

# items whose feed price hit the 1800.00 ceiling, with the values we must replace
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

HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/126.0 Safari/537.36'),
    'Accept': 'text/html,application/xhtml+xml',
    'Accept-Language': 'en-US,en;q=0.9',
}

ANCHOR = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
MONEY = re.compile(r'\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)')
TAGS = re.compile(r'<[^>]+>')


def slug(name):
    s = name.replace('\u2605', '').replace('|', '').replace('\u2122', '')
    s = re.sub(r'\s+', ' ', s).strip().lower()
    return s.replace(' ', '-')


def parse(html):
    """Lowest price per variant/wear across every marketplace link on the page."""
    found = {'normal': {}, 'stattrak': {}}
    for href, inner in ANCHOR.findall(html):
        text = TAGS.sub(' ', inner)
        money = MONEY.search(text)
        if not money:
            continue
        price = float(money.group(1).replace(',', ''))
        if price <= 0:
            continue
        target = urllib.parse.unquote(href)
        low = target.lower()
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


def fetch(url):
    for attempt in range(1, ATTEMPTS + 1):
        started = time.time()
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            took = time.time() - started
            print('   http %s in %.1fs (%d bytes)' % (r.status_code, took, len(r.content)))
            if r.status_code == 200 and 'Lowest' in r.text:
                return r.text
            if r.status_code in (403, 503):
                print('   looks like a Cloudflare challenge')
        except Exception as exc:
            print('   error after %.1fs: %s' % (time.time() - started, exc))
        if attempt < ATTEMPTS:
            time.sleep(4)
    return None


def main():
    OUT.mkdir(exist_ok=True)
    result, missing = {}, []
    started = time.time()
    for i, name in enumerate(ITEMS, 1):
        url = BASE + slug(name) + '/'
        print('[%2d/%d] %s' % (i, len(ITEMS), url))
        html = fetch(url)
        if not html:
            missing.append(name)
        else:
            prices = parse(html)
            if prices['normal'] or prices['stattrak']:
                result[name] = prices
                print('   normal   %s' % prices['normal'])
                print('   stattrak %s' % prices['stattrak'])
            else:
                print('   page loaded but no prices parsed')
                missing.append(name)
        # save after every page so a timeout still leaves usable data
        payload = {'source': 'csgodatabase.com lowest price per variant',
                   'items': result, 'missing': missing}
        (OUT / 'capped_fix.json').write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
        print('   elapsed %.0fs | ok %d | missing %d' % (time.time() - started, len(result), len(missing)))
        if i < len(ITEMS):
            time.sleep(random.uniform(1.5, 3.0))

    print('\nresolved: %d / %d | missing: %s' % (len(result), len(ITEMS), missing))
    print('--- copy everything below this line ---')
    print(json.dumps({'items': result, 'missing': missing}, ensure_ascii=False))
    print('--- end ---')
    if not result:
        sys.exit(1)


if __name__ == '__main__':
    main()
