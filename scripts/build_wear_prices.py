#!/usr/bin/env python3
"""Build a per-wear price table without scraping.

Source: SKINSTRACK CS2 price feed, which is keyed by market_hash_name.
Because the market hash name already encodes wear and StatTrak, one download
gives us the full per-wear matrix - no browser, no Cloudflare, no rate limits.

Output: prices/wear_prices.json
  {
    "items": {
      "AK-47 | Redline": {
        "normal":   {"FN": 12.3, "MW": 8.1, ...},
        "stattrak": {"FN": 30.0, ...}
      }
    }
  }
plus prices/unresolved.json with case items that got no price at all.
"""
import json
import re
import sys
import pathlib
import datetime

import requests

FEED = 'https://raw.githubusercontent.com/SKINSTRACK/CS2-Price-API/main/free_prices.json'
CRATES = 'https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/crates.json'
OUT = pathlib.Path('prices')
OUT.mkdir(exist_ok=True)

WEAR = {
    'Factory New': 'FN',
    'Minimal Wear': 'MW',
    'Field-Tested': 'FT',
    'Well-Worn': 'WW',
    'Battle-Scarred': 'BS',
}
STAR = '\u2605'
TM = '\u2122'


def get_json(url, what):
    r = requests.get(url, timeout=300)
    r.raise_for_status()
    print('%s: %d bytes' % (what, len(r.content)))
    return r.json()


def split_name(mhn):
    """'StatTrak(tm) AK-47 | Redline (Field-Tested)' -> ('AK-47 | Redline', 'FT', True)"""
    s = mhn.strip()
    if s.startswith('Souvenir '):
        return None
    star = False
    if s.startswith(STAR):
        star = True
        s = s[1:].strip()
    stattrak = False
    if s.startswith('StatTrak' + TM):
        stattrak = True
        s = s[len('StatTrak' + TM):].strip()
    elif s.startswith('StatTrak'):
        stattrak = True
        s = s[len('StatTrak'):].strip()
    wear = 'V'
    m = re.search(r'\s\(([^()]+)\)$', s)
    if m:
        if m.group(1) not in WEAR:
            return None
        wear = WEAR[m.group(1)]
        s = s[:m.start()].strip()
    if '|' not in s and not star:
        return None
    base = (STAR + ' ' + s) if star else s
    return base, wear, stattrak


def pick_price(item):
    """Prefer the Steam price, fall back to any other provider."""
    best = None
    for p in item.get('prices') or []:
        if not isinstance(p, dict):
            continue
        v = p.get('price')
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v <= 0:
            continue
        if p.get('provider') == 'steam':
            return v
        if best is None:
            best = v
    return best


def load_case_names():
    crates = get_json(CRATES, 'crates.json')
    names = set()
    for c in crates:
        if c.get('type') != 'Case':
            continue
        for it in (c.get('contains') or []) + (c.get('contains_rare') or []):
            n = it.get('name')
            if n:
                names.add(n.strip())
    print('case items: %d' % len(names))
    return names


def main():
    wanted = load_case_names()
    feed = get_json(FEED, 'free_prices.json')
    fetched_at = None
    if isinstance(feed, dict):
        fetched_at = feed.get('fetched_at')
        rows = feed.get('data') or []
    else:
        rows = feed
    print('feed rows: %d | fetched_at: %s' % (len(rows), fetched_at))

    result = {}
    seen_rows = 0
    for row in rows:
        mhn = row.get('market_hash_name') if isinstance(row, dict) else None
        if not mhn:
            continue
        parsed = split_name(mhn)
        if not parsed:
            continue
        base, wear, stattrak = parsed
        if base not in wanted:
            continue
        price = pick_price(row)
        if price is None:
            continue
        seen_rows += 1
        entry = result.setdefault(base, {'normal': {}, 'stattrak': {}})
        entry['stattrak' if stattrak else 'normal'][wear] = round(price, 2)

    missing = sorted(n for n in wanted if n not in result)
    payload = {
        'generated_at': datetime.datetime.now(datetime.timezone.utc)
        .strftime('%Y-%m-%dT%H:%M:%SZ'),
        'source': 'SKINSTRACK/CS2-Price-API free_prices.json (per market_hash_name)',
        'source_fetched_at': fetched_at,
        'complete': True,
        'total': len(result),
        'items': result,
    }
    (OUT / 'wear_prices.json').write_text(
        json.dumps(payload, ensure_ascii=False, indent=1))
    (OUT / 'unresolved.json').write_text(
        json.dumps(missing, ensure_ascii=False, indent=1))

    full = sum(1 for v in result.values() if len(v['normal']) >= 4)
    with_st = sum(1 for v in result.values() if v['stattrak'])
    print('matched rows: %d' % seen_rows)
    print('items priced: %d / %d (missing %d)'
          % (len(result), len(wanted), len(missing)))
    print('items with 4+ normal wears: %d | with any StatTrak: %d' % (full, with_st))
    for name in list(result)[:5]:
        print('  sample %s -> %s' % (name, json.dumps(result[name], ensure_ascii=False)))
    for name in missing[:15]:
        print('  no price: %s' % name)
    if not result:
        sys.exit(1)


if __name__ == '__main__':
    main()
