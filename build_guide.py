#!/usr/bin/env python3
"""build_guide.py - turn big XMLTV guides into small files Cable Shot can load live.

Cable Shot can only load XMLTV files that are https, under 1 MB, not gzipped, no redirects.
Real guides are tens of MB. This script (run on your computer, or on a schedule by GitHub Actions)
downloads the big guides, keeps ONLY the channels in your m3u, and writes guide-1.xml, guide-2.xml ...
each under 1 MB. Host those files anywhere that serves them directly over https (GitHub Pages or
raw.githubusercontent.com), then point the plugin at them once:

    python3 build_guide.py --m3u us.m3u --xml URL_OR_FILE [--xml URL_OR_FILE ...] --out guide
    python3 add_m3u.py --epg-url https://YOU.github.io/REPO/guide-1.xml --epg-url https://.../guide-2.xml --logo-hosts 62

After that the plugin re-reads those addresses every time it opens and every 30 minutes, so the guide
stays current as long as this script keeps refreshing the hosted files (see the GitHub Actions file).

Matching: a channel matches a guide channel by tvg-id (the "@SD" part is ignored), else by name.
Output channel ids are your m3u tvg-ids, so the plugin matches them exactly.
"""
import argparse
import datetime
import gzip
import io
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

ATTR = re.compile(r'([A-Za-z0-9_-]+)\s*=\s*"([^"]*)"')


JUNK = {'hd', 'sd', 'fhd', 'uhd', '4k', 'hevc', '1080p', '720p', '480p', '576p', '360p', 'tv'}
REGION_WORDS = {'east': 'east', 'eastern': 'east', 'west': 'west', 'pacific': 'west'}


def region_of(*texts):
    """'west' / 'east' if the text says so, else ''."""
    for t in texts:
        for w in re.findall(r'[a-z]+', str(t or '').lower()):
            if w in REGION_WORDS:
                return REGION_WORDS[w]
    return ''


def base_of(s, is_id=False):
    """Channel name or id with the noise removed: 'Syfy.HD.(Pacific).us2' and 'Syfy.us@West' -> 'syfy'."""
    t = str(s or '').split('@', 1)[0].lower()
    t = re.sub(r'\([^)]*\)', ' ', t)
    words = [w for w in re.findall(r'[a-z0-9+]+', t)]
    if is_id and len(words) > 1 and re.fullmatch(r'[a-z]{2,3}\d?', words[-1]):
        words.pop()                                   # trailing country code: us, us2, ca ...
    words = [w for w in words if w not in JUNK and w not in REGION_WORDS]
    return ''.join(words)


def read_m3u(path):
    """-> [(tvg_id, name)] from the m3u (file path or https address)."""
    text = load(path).decode('utf-8', 'replace')
    out = []
    for line in text.splitlines():
        if line.upper().startswith('#EXTINF'):
            body = line.split(':', 1)[1] if ':' in line else ''
            at = {m.group(1).lower(): m.group(2) for m in ATTR.finditer(body)}
            rest = ATTR.sub('', body)
            name = rest.split(',', 1)[1].strip() if ',' in rest else at.get('tvg-name', '')
            out.append((at.get('tvg-id', ''), name))
    return out


def load(src):
    if re.match(r'^https?://', src, re.I):
        req = urllib.request.Request(src, headers={'User-Agent': 'Mozilla/5.0 (build_guide)'})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
    else:
        with open(src, 'rb') as f:
            data = f.read()
    if data[:2] == b'\x1f\x8b':
        data = gzip.decompress(data)
    return data


def xmltv_time(s):
    m = re.match(r'^\s*(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})?\s*([+-]\d{4})?\s*$', s or '')
    if not m:
        return 0
    t = datetime.datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]), int(m[6] or 0),
                          tzinfo=datetime.timezone.utc).timestamp()
    if m[7]:
        off = int(m[7][1:3]) * 3600 + int(m[7][3:5]) * 60
        t -= -off if m[7][0] == '-' else off
    return int(t)


def fmt_time(sec):
    return time.strftime('%Y%m%d%H%M%S +0000', time.gmtime(sec))


def collect(sources, chans, hours_back, hours_ahead, with_desc):
    """-> {m3u_key: [(start, stop, title, desc)]} where m3u_key is the m3u tvg-id or normalised name."""
    by_exact = {}
    by_base = {}                                  # base name -> [(m3u_key, region)]
    for tid, name in chans:
        key = tid or ('n:' + base_of(name))
        if tid:
            by_exact.setdefault(tid.lower(), set()).add(key)
        reg = region_of(tid.split('@', 1)[1] if '@' in tid else '', name) or 'east'
        for bs in {base_of(tid, True), base_of(name)}:
            if bs:
                by_base.setdefault(bs, []).append((key, reg))
    now = time.time()
    lo, hi = now - hours_back * 3600, now + hours_ahead * 3600
    progs = {}
    for src in sources:
        print('reading', src)
        try:
            data = load(src)
        except Exception as ex:                  # one dead source should not stop the others
            print('  SKIPPED (could not download):', ex)
            continue
        wanted = {}
        total, sample = 0, []
        for _ev, el in ET.iterparse(io.BytesIO(data), events=('end',)):
            if el.tag == 'channel':
                total += 1
                if len(sample) < 12:
                    sample.append('%s = %s' % (el.get('id'), el.findtext('display-name')))
                cid = (el.get('id') or '').lower()
                names = [dn.text or '' for dn in el.findall('display-name')]
                greg = region_of(el.get('id'), *names)
                keys = set(by_exact.get(cid, ()))
                for bs in {base_of(cid, True)} | {base_of(nm) for nm in names}:
                    for key, reg in by_base.get(bs, ()):
                        if not greg or greg == reg:   # an unlabelled guide channel suits both East and West
                            keys.add(key)
                if cid and keys:
                    wanted.setdefault(cid, set()).update(keys)
                el.clear()
        print('  guide has %d channels; matched %d of them. Examples (id = name):' % (total, len(wanted)))
        for x in sample:
            print('     ', x)
        n = 0
        for _ev, el in ET.iterparse(io.BytesIO(data), events=('end',)):
            if el.tag == 'programme':
                cid = (el.get('channel') or '').lower()
                if cid in wanted:
                    s, e = xmltv_time(el.get('start')), xmltv_time(el.get('stop'))
                    title = ' '.join((el.findtext('title') or '').split())
                    if title and s and e and e > lo and s < hi:
                        desc = ' '.join((el.findtext('desc') or '').split())[:160] if with_desc else ''
                        for k in wanted[cid]:
                            progs.setdefault(k, []).append((s, e, title[:80], desc))
                            n += 1
                el.clear()
        print('  kept %d programmes for %d guide channels' % (n, len(wanted)))
    for k in progs:                              # sort, drop duplicates when several guides cover a channel
        seen, out = set(), []
        for p in sorted(progs[k]):
            if (p[0], p[1]) not in seen:
                seen.add((p[0], p[1]))
                out.append(p)
        progs[k] = out
    return progs


def render(items, names):
    parts = ['<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="build_guide">\n']
    for key, plist in items:
        cid = escape(key, {'"': '&quot;'})
        parts.append('<channel id="%s"><display-name>%s</display-name></channel>\n' % (cid, escape(names.get(key, key))))
    for key, plist in items:
        cid = escape(key, {'"': '&quot;'})
        for s, e, title, desc in plist:
            parts.append('<programme start="%s" stop="%s" channel="%s"><title>%s</title>%s</programme>\n' % (
                fmt_time(s), fmt_time(e), cid, escape(title),
                ('<desc>%s</desc>' % escape(desc)) if desc else ''))
    parts.append('</tv>\n')
    return ''.join(parts)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--m3u', required=True, action='append', help='your channel list (.m3u file or https address), repeatable')
    ap.add_argument('--xml', required=True, action='append', help='big XMLTV guide (file or https address, .gz ok), repeatable')
    ap.add_argument('--out', default='guide', help='folder for guide-1.xml, guide-2.xml ... (default: guide)')
    ap.add_argument('--hours-ahead', type=int, default=30)
    ap.add_argument('--hours-back', type=int, default=2)
    ap.add_argument('--desc', action='store_true', help='keep short descriptions (bigger files)')
    ap.add_argument('--max-bytes', type=int, default=900_000, help='size limit per file (plugin reads at most 1 MB)')
    a = ap.parse_args()

    chans = []
    for m in a.m3u:
        chans += read_m3u(m)
    names = {(t or 'n:' + base_of(n)): n for t, n in chans}
    print('%d channels in the list' % len(chans))
    progs = collect(a.xml, chans, a.hours_back, a.hours_ahead, a.desc)
    if not progs:
        sys.exit('No guide data matched your channels. Check that the guide covers these tvg-ids / names.')

    os.makedirs(a.out, exist_ok=True)
    for f in os.listdir(a.out):
        if re.match(r'^guide-\d+\.xml$', f):
            os.remove(os.path.join(a.out, f))
    chunks, cur, cur_size = [], [], 400
    for key in sorted(progs):
        item = (key, progs[key])
        size = len(render([item], names).encode('utf-8'))
        if size > a.max_bytes:                   # one channel alone is too big: trim to the soonest programmes
            while item[1] and len(render([(key, item[1])], names).encode('utf-8')) > a.max_bytes:
                item = (key, item[1][:-5])
            size = len(render([item], names).encode('utf-8'))
        if cur and cur_size + size > a.max_bytes:
            chunks.append(cur)
            cur, cur_size = [], 400
        cur.append(item)
        cur_size += size
    if cur:
        chunks.append(cur)
    for i, ch in enumerate(chunks, 1):
        path = os.path.join(a.out, 'guide-%d.xml' % i)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(render(ch, names))
        print('wrote %s (%d channels, %d KB)' % (path, len(ch), os.path.getsize(path) // 1024))
    print('\n%d channels have guide data; %d files. Host them, then:' % (len(progs), len(chunks)))
    print('  python3 add_m3u.py ' + ' '.join('--epg-url https://YOUR-HOST/guide-%d.xml' % i for i in range(1, len(chunks) + 1)))


if __name__ == '__main__':
    main()
