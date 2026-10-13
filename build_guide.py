#!/usr/bin/env python3
"""build_guide.py - turn big XMLTV guides into small files Cable Shot can load live.

Cable Shot can only load XMLTV files that are https, under 1 MB, not gzipped, no redirects.
Real guides are tens or hundreds of MB. This script (run on a schedule by GitHub Actions) downloads the
big guides, keeps ONLY the channels in your m3u lists, and writes guide-1.xml ... guide-N.xml, each under
1 MB. N is fixed (--files, default 8) so the plugin's list of addresses never has to change; unused
files are written empty. If the data does not fit, it keeps fewer hours ahead instead of dropping channels.

Matching, in this order: exact tvg-id; id/name with noise removed (HD, us2, (720p), East/West);
US local-station call signs (KRIS-TV matches KRIS-DT). East and West feeds are kept apart.

It also writes guide/report.txt: coverage overall, by country and by category, and the list of channels
that still have no guide.
"""
import argparse
import collections
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
JUNK = {'hd', 'sd', 'fhd', 'uhd', '4k', 'hevc', '1080p', '720p', '480p', '576p', '432p', '360p', 'tv'}
REGION_WORDS = {'east': 'east', 'eastern': 'east', 'west': 'west', 'pacific': 'west'}
CALLSIGN = re.compile(r'\b([KW][A-Z]{2,3})(?:-[A-Z]{2})?\b')
NOT_CALLSIGNS = {'WEST', 'KIDS', 'KIDZ', 'WILD', 'WORK', 'KING', 'WINE', 'WAVE', 'WORD', 'WIRE'}


def region_of(*texts):
    """'west' / 'east' if the text says so, else ''."""
    for t in texts:
        for w in re.findall(r'[a-z]+', str(t or '').lower()):
            if w in REGION_WORDS:
                return REGION_WORDS[w]
    return ''


def base_of(s, is_id=False):
    """Name or id with the noise removed: 'Syfy.HD.(Pacific).us2' and 'Syfy.us@West' -> 'syfy'."""
    t = str(s or '').split('@', 1)[0].lower()
    if is_id:
        t = re.sub(r'\.[a-z]{2,3}(_[a-z]+)?\d?$', '', t)      # .us  .us2  .us_locals1  .uk
    t = re.sub(r'\([^)]*\)', ' ', t)
    words = re.findall(r'[a-z0-9+]+', t)
    return ''.join(w for w in words if w not in JUNK and w not in REGION_WORDS)


def callsigns(*texts):
    out = set()
    for t in texts:
        for c in CALLSIGN.findall(str(t or '')):
            if c not in NOT_CALLSIGNS:
                out.add(c)
    return out


def country_of(tid):
    m = re.search(r'\.([a-z]{2,3})(?:@|$)', str(tid or '').lower())
    return m.group(1) if m else '?'


def load(src):
    if re.match(r'^https?://', src, re.I):
        req = urllib.request.Request(src, headers={'User-Agent': 'Mozilla/5.0 (build_guide)'})
        with urllib.request.urlopen(req, timeout=300) as r:
            data = r.read()
    else:
        with open(src, 'rb') as f:
            data = f.read()
    if data[:2] == b'\x1f\x8b':
        data = gzip.decompress(data)
    return data


def read_m3u(path):
    """-> [(tvg_id, name, group)] from an m3u (file path or https address)."""
    text = load(path).decode('utf-8', 'replace')
    out = []
    for line in text.splitlines():
        if line.upper().startswith('#EXTINF'):
            body = line.split(':', 1)[1] if ':' in line else ''
            at = {m.group(1).lower(): m.group(2) for m in ATTR.finditer(body)}
            rest = ATTR.sub('', body)
            name = rest.split(',', 1)[1].strip() if ',' in rest else at.get('tvg-name', '')
            out.append((at.get('tvg-id', ''), name, at.get('group-title', '')))
    return out


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


def collect(sources, chans, hours_back, hours_ahead, with_desc, find=()):
    """-> {m3u_key: [(start, stop, title, desc)]}"""
    by_exact = {}
    by_base = {}                                   # base -> [(m3u_key, region or None)]
    for tid, name, _grp in chans:
        key = tid or ('n:' + base_of(name))
        if tid:
            by_exact.setdefault(tid.lower(), set()).add(key)
        reg = region_of(tid.split('@', 1)[1] if '@' in tid else '', name) or 'east'
        for bs in {base_of(tid, True), base_of(name)}:
            if bs:
                by_base.setdefault(bs, []).append((key, reg))
        if country_of(tid) == 'us':                # local stations: match on call sign, any region
            for c in callsigns(name, tid.split('@', 1)[1] if '@' in tid else ''):
                by_base.setdefault('cs:' + c, []).append((key, None))
    now = time.time()
    lo, hi = now - hours_back * 3600, now + hours_ahead * 3600
    progs = {}
    for src in sources:
        print('reading', src, flush=True)
        try:
            data = load(src)
        except Exception as ex:                    # one dead source should not stop the others
            print('  SKIPPED (could not download):', ex)
            continue
        wanted = {}
        total, sample = 0, []
        for _ev, el in ET.iterparse(io.BytesIO(data), events=('end',)):
            if el.tag == 'channel':
                total += 1
                if len(sample) < 6:
                    sample.append('%s = %s' % (el.get('id'), el.findtext('display-name')))
                cid = (el.get('id') or '').lower()
                names = [dn.text or '' for dn in el.findall('display-name')]
                low = (cid + ' ' + ' '.join(names)).lower()
                for q in find:
                    if q.lower() in low:
                        print('  FOUND "%s" in guide: %s = %s' % (q, el.get('id'), ' / '.join(names)))
                greg = region_of(el.get('id'), *names)
                keys = set(by_exact.get(cid, ()))
                bases = {base_of(cid, True)} | {base_of(nm) for nm in names}
                bases |= {'cs:' + c for c in callsigns(el.get('id'), *names)}
                for bs in bases:
                    for key, reg in by_base.get(bs, ()):
                        if reg is None or not greg or greg == reg:   # unlabelled guide channel suits East and West
                            keys.add(key)
                if cid and keys:
                    wanted.setdefault(cid, set()).update(keys)
                el.clear()
        print('  guide has %d channels; matched %d of them. e.g.' % (total, len(wanted)))
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
        print('  kept %d programmes' % n, flush=True)
        del data
    for k in progs:                                # sort; drop duplicates when several guides cover a channel
        seen, out = set(), []
        for p in sorted(progs[k]):
            if (p[0], p[1]) not in seen:
                seen.add((p[0], p[1]))
                out.append(p)
        progs[k] = out
    return progs


def render(items, names):
    parts = ['<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="build_guide">\n']
    for key, _pl in items:
        parts.append('<channel id="%s"><display-name>%s</display-name></channel>\n' % (
            escape(key, {'"': '&quot;'}), escape(names.get(key, key))))
    for key, plist in items:
        cid = escape(key, {'"': '&quot;'})
        for s, e, title, desc in plist:
            parts.append('<programme start="%s" stop="%s" channel="%s"><title>%s</title>%s</programme>\n' % (
                fmt_time(s), fmt_time(e), cid, escape(title),
                ('<desc>%s</desc>' % escape(desc)) if desc else ''))
    parts.append('</tv>\n')
    return ''.join(parts)


def make_chunks(progs, names, max_bytes):
    chunks, cur, cur_size = [], [], 400
    for key in sorted(progs):
        item = (key, progs[key])
        size = len(render([item], names).encode('utf-8'))
        while size > max_bytes and item[1]:
            item = (key, item[1][:-5])
            size = len(render([item], names).encode('utf-8'))
        if cur and cur_size + size > max_bytes:
            chunks.append(cur)
            cur, cur_size = [], 400
        cur.append(item)
        cur_size += size
    if cur:
        chunks.append(cur)
    return chunks


def pct(a, b):
    return '%d%%' % round(100.0 * a / b) if b else '-'


def write_report(path, uniq, progs):
    have = [k for k in uniq if k in progs]
    lines = ['COVERAGE: %d of %d channels have a guide (%s)\n' % (len(have), len(uniq), pct(len(have), len(uniq)))]
    by_c, by_g = collections.defaultdict(lambda: [0, 0]), collections.defaultdict(lambda: [0, 0])
    for k, (_n, grp, cc) in uniq.items():
        ok = k in progs
        by_c[cc][0] += 1
        by_c[cc][1] += ok
        for g in (grp or 'none').split(';'):
            by_g[g.strip() or 'none'][0] += 1
            by_g[g.strip() or 'none'][1] += ok
    lines.append('BY COUNTRY (code: with guide / total)')
    for cc, (t, ok) in sorted(by_c.items(), key=lambda x: -x[1][0])[:25]:
        lines.append('  %-4s %5d / %-5d %s' % (cc, ok, t, pct(ok, t)))
    lines.append('\nBY CATEGORY')
    for g, (t, ok) in sorted(by_g.items(), key=lambda x: -x[1][0])[:25]:
        lines.append('  %-16s %5d / %-5d %s' % (g[:16], ok, t, pct(ok, t)))
    lines.append('\nWITH GUIDE (%d)' % len(have))
    for k in sorted(have, key=lambda x: uniq[x][0].lower()):
        lines.append('  %s   [%s]' % (uniq[k][0], k))
    lines.append('\nNO GUIDE (%d)' % (len(uniq) - len(have)))
    for k in sorted((k for k in uniq if k not in progs), key=lambda x: uniq[x][0].lower()):
        lines.append('  %s   [%s]' % (uniq[k][0], k))
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    return lines[:1] + lines[1:14]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--m3u', required=True, action='append', help='channel list (.m3u file or https address), repeatable')
    ap.add_argument('--xml', required=True, action='append', help='big XMLTV guide (file or https address, .gz ok), repeatable')
    ap.add_argument('--out', default='guide', help='folder for guide-1.xml ... (default: guide)')
    ap.add_argument('--files', type=int, default=8, help='always write exactly this many guide files (default 8; 0 = as many as needed)')
    ap.add_argument('--base-url', default='https://YOUR-HOST', help='where the files will be hosted (only used to print the add_m3u.py command)')
    ap.add_argument('--hours-ahead', type=int, default=24)
    ap.add_argument('--hours-back', type=int, default=2)
    ap.add_argument('--find', action='append', default=[], help='print guide channels whose id or name contains this text (debug), repeatable')
    ap.add_argument('--desc', action='store_true', help='keep short descriptions (bigger files)')
    ap.add_argument('--max-bytes', type=int, default=900_000, help='size limit per file (plugin reads at most 1 MB)')
    a = ap.parse_args()

    chans = []
    for m in a.m3u:
        chans += read_m3u(m)
    uniq = {}
    for tid, name, grp in chans:
        uniq.setdefault(tid or 'n:' + base_of(name), (name, grp, country_of(tid)))
    print('%d channels in the list (%d unique)' % (len(chans), len(uniq)))
    names = {k: v[0] for k, v in uniq.items()}
    progs = collect(a.xml, chans, a.hours_back, a.hours_ahead, a.desc, a.find)
    if not progs:
        sys.exit('No guide data matched your channels. Check that the guides cover these tvg-ids / names.')

    os.makedirs(a.out, exist_ok=True)
    for f in os.listdir(a.out):
        if re.match(r'^guide-\d+\.xml$', f):
            os.remove(os.path.join(a.out, f))

    now = time.time()
    options = sorted({a.hours_ahead, 18, 12, 9, 6, 4} & set(range(1, a.hours_ahead + 1)), reverse=True)
    used = a.hours_ahead
    for h in options:
        used = h
        trimmed = {k: [p for p in v if p[0] < now + h * 3600] for k, v in progs.items()}
        trimmed = {k: v for k, v in trimmed.items() if v}
        chunks = make_chunks(trimmed, names, a.max_bytes)
        if not a.files or len(chunks) <= a.files:
            break
    if a.files and len(chunks) > a.files:
        print('WARNING: data does not fit in %d files even at %dh; dropping the last channels. Raise --files.' % (a.files, used))
        chunks = chunks[:a.files]
    n_files = a.files or len(chunks)
    for i in range(1, n_files + 1):
        path = os.path.join(a.out, 'guide-%d.xml' % i)
        ch = chunks[i - 1] if i <= len(chunks) else []
        with open(path, 'w', encoding='utf-8') as f:
            f.write(render(ch, names))
        print('wrote %s (%d channels, %d KB)' % (path, len(ch), os.path.getsize(path) // 1024))

    final = {k for ch in chunks for k, _p in ch}
    summary = write_report(os.path.join(a.out, 'report.txt'), uniq, {k: 1 for k in final})
    print('\n=== %s' % summary[0])
    print('guide window: %d hours ahead' % used)
    for ln in summary[1:]:
        print(ln)
    print('\nFull list: %s/report.txt' % a.out)
    print('Plugin command (only needed once, the file count stays %d):' % n_files)
    print('  python3 add_m3u.py ' + ' '.join('--epg-url %s/guide-%d.xml' % (a.base_url.rstrip('/'), i) for i in range(1, n_files + 1)))


if __name__ == '__main__':
    main()
