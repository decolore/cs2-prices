#!/usr/bin/env python3
"""Build a per-wear price table without scraping.

Source: SKINSTRACK CS2 price feed, keyed by market_hash_name. The market hash
name already encodes wear and StatTrak, so one download gives the full per-wear
matrix - no browser, no Cloudflare, no rate limits.

The feed layout is not documented, so this script does two things:
  1. prints the real structure it sees (keys, types, one sample record)
  2. finds price records anywhere in the tree, whatever the nesting is

Output: prices/wear_prices.json + prices/unresolved.json
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
PRICE_KEYS = ('price', 'steam', 'steam_price', 'last_price', 'lowest_price',
              'safe_price', 'median_price', 'median', 'avg', 'average',
              'value', 'min', 'starting_at')


def get_json(url, what):
    r = requests.get(url, timeout=300)
    r.raise_for_status()
    print('%s: %d bytes' % (what, len(r.content)))
    return r.json()


def describe(obj, label='root', depth=0, max_depth=4):
    """Print the actual shape of the payload so we stop guessing."""
    pad = '  ' * depth
    if isinstance(obj, dict):
        keys = list(obj.keys())
        print('%s%s: dict(%d) keys=%s' % (pad, label, len(keys),
                                          [str(k)[:40] for k in keys[:8]]))
        if depth < max_depth:
            for k in keys[:4]:
                describe(obj[k], str(k)[:40], depth + 1, max_depth)
    elif isinstance(obj, list):
        print('%s%s: list(%d)' % (pad, label, len(obj)))
        if obj and depth < max_depth:
            describe(obj[0], label + '[0]', depth + 1, max_depth)
    else:
        print('%s%s: %s %r' % (pad, label, type(obj).__name__, str(obj)[:80]))


def looks_like_item_name(s):
    return isinstance(s, str) and ('|' in s or s.startswith(STAR)) and len(s) < 200


def collect(obj, out, depth=0):
    """Find (name, node) pairs anywhere in the tree."""
    if depth > 7 or len(out) > 400000:
        return
    if isinstance(obj, list):
        for x in obj:
            collect(x, out, depth + 1)
        return
    if not isinstance(obj, dict):
        return
    name = obj.get('market_hash_name') or obj.get('markethashname') or obj.get('name')
    if looks_like_item_name(name):
        out.append((name, obj))
        return
    keys = list(obj.keys())
    namey = sum(1 for k in keys[:60] if looks_like_item_name(k))
    if namey >= 3:
        for k, v in obj.items():
            if looks_like_item_name(k):
                out.append((k, v))
        return
    for v in obj.values():
        collect(v, out, depth + 1)


def as_price(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def pick_price(node, depth=0):
    """Pull a usable price out of whatever shape the record has."""
    if depth > 4:
        return None
    direct = as_price(node)
    if direct is not None and not isinstance(node, (dict, list)):
        return direct
    if isinstance(node, list):
        steam = [p for p in node if isinstance(p, dict) and p.get('provider') == 'steam']
        for p in steam + [p for p in node if isinstance(p, dict)]:
            got = pick_price(p, depth + 1)
            if got is not None:
                return got
        return None
    if not isinstance(node, dict):
        return None
    if 'provider' in node or 'price' in node:
        got = as_price(node.get('price'))
        if got is not None:
            return got
    for key in ('prices', 'steam', 'data'):
        if key in node:
            got = pick_price(node[key], depth + 1)
            if got is not None:
                return got
    for key in PRICE_KEYS:
        if key in node:
            got = pick_price(node[key], depth + 1)
            if got is not None:
                return got
    return None


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
    for prefix in ('StatTrak' + TM, 'StatTrak'):
        if s.startswith(prefix):
            stattrak = True
            s = s[len(prefix):].strip()
            break
    wear = 'V'
    m = re.search(r'\s\(([^()]+)\)$', s)
    if m:
        if m.group(1) not in WEAR:
            return None
        wear = WEAR[m.group(1)]
        s = s[:m.start()].strip()
    if '|' not in s and not star:
        return None
    return (STAR + ' ' + s) if star else s, wear, stattrak


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

    print('--- feed structure ---')
    describe(feed)
    print('--- end structure ---')

    records = []
    collect(feed, records)
    print('records found: %d' % len(records))
    for name, node in records[:3]:
        print('  raw %r -> %s' % (name, json.dumps(node, ensure_ascii=False)[:300]))

    result = {}
    matched = 0
    no_price = 0
    for name, node in records:
        parsed = split_name(name)
        if not parsed:
            continue
        base, wear, stattrak = parsed
        if base not in wanted:
            continue
        price = pick_price(node)
        if price is None:
            no_price += 1
            continue
        matched += 1
        entry = result.setdefault(base, {'normal': {}, 'stattrak': {}})
        entry['stattrak' if stattrak else 'normal'][wear] = round(price, 2)

    missing = sorted(n for n in wanted if n not in result)
    payload = {
        'generated_at': datetime.datetime.now(datetime.timezone.utc)
        .strftime('%Y-%m-%dT%H:%M:%SZ'),
        'source': 'SKINSTRACK/CS2-Price-API free_prices.json (per market_hash_name)',
        'source_fetched_at': feed.get('fetched_at') if isinstance(feed, dict) else None,
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
    print('matched rows: %d | records skipped for no price: %d' % (matched, no_price))
    print('items priced: %d / %d (missing %d)'
          % (len(result), len(wanted), len(missing)))
    print('items with 4+ normal wears: %d | with any StatTrak: %d' % (full, with_st))
    for name in list(result)[:5]:
        print('  sample %s -> %s' % (name, json.dumps(result[name], ensure_ascii=False)))
    for name in missing[:15]:
        print('  no price: %s' % name)


if __name__ == '__main__':
    main()
