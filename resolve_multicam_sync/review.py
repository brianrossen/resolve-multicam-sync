"""Color clips by what's in them (bride, groom, details, dancing ...) after a sync.

The script can't see footage, so this is a three-step loop:

  1. sheets:  python3 -m resolve_multicam_sync.review sheets REPORT.json OUTDIR
              -> numbered contact sheets of every clip in shooting order + OUTDIR/index.json
  2. review:  someone looks at the sheets (Claude Code does this well) and writes a colors file, one rule per line:
                  0-5 bride            (sheet numbers, a category from "content_colors" ...)
                  41 groom
                  42-60 Teal           (... or a Resolve clip color name)
                  A0123.MP4 portraits       (or a file name)
  3. apply:   python3 -m resolve_multicam_sync.review apply OUTDIR/index.json COLORS.txt --timeline "Day 2"
              -> recolors that timeline's video clips. Nothing is moved, trimmed or added.

Categories and their colors come from "content_colors" in ~/.resolve-multicam-sync.json (or --scheme FILE):
  {"content_colors": {"categories": {"bride": "Pink", "groom": "Navy", "dancing": "Brown", ...},
                      "second_camera": {"dancing": "Tan"}, "main_camera": "A"}}
"second_camera" gives every camera except the main one its own color for that category.
REPORT.json is written by a sync run with --report.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from .build import PALETTE, label_of

DEFAULT_SCHEME = {
    'categories': {
        'bride': 'Pink', 'groom': 'Navy', 'bride_girls': 'Purple', 'groom_guys': 'Blue', 'details': 'Teal',
        'venue': 'Beige', 'first_look': 'Orange', 'ceremony': 'Orange', 'vows': 'Yellow', 'portraits': 'Yellow',
        'wedding_party': 'Orange', 'dinner': 'Green', 'toasts': 'Green', 'dancing': 'Brown', 'exit': 'Chocolate',
        'drone': 'Lime',
    },
    'second_camera': {},
    'main_camera': None,
}
COLORS = set(PALETTE) | {'Orange', 'Apricot', 'Yellow', 'Lime', 'Olive', 'Green', 'Teal', 'Navy', 'Blue', 'Purple',
                         'Violet', 'Pink', 'Tan', 'Beige', 'Brown', 'Chocolate'}


# ---------------------------------------------------------------- 1. contact sheets
def _thumb(path: str, out: str, dur: float):
    if os.path.exists(out) and os.path.getsize(out):
        return
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', f'{max(0.0, dur * 0.45):.2f}', '-i', path,
                    '-frames:v', '1', '-vf', 'scale=320:-2', '-q:v', '4', out])


def sheets(report: str, outdir: str, cols: int = 8, rows: int = 6):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        sys.exit('Contact sheets need Pillow: python3 -m pip install pillow')
    files = json.load(open(os.path.expanduser(report)))['files']
    vids = sorted((f['start'], p, f) for p, f in files.items() if f['kind'] == 'video')
    os.makedirs(f'{outdir}/thumbs', exist_ok=True)
    print(f'Grabbing a frame from {len(vids)} clips...')
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(lambda v: _thumb(v[1], f"{outdir}/thumbs/{os.path.basename(v[1])}.jpg", v[2]['duration']), vids))
    try:
        font = ImageFont.truetype('/System/Library/Fonts/Helvetica.ttc', 15)
    except OSError:
        font = ImageFont.load_default()
    TW, TH, n = 240, 135, cols * rows
    index = []
    for s in range(0, len(vids), n):
        sheet = Image.new('RGB', (cols * TW, rows * (TH + 18)), 'black')
        d = ImageDraw.Draw(sheet)
        for k, (start, path, f) in enumerate(vids[s:s + n]):
            i, name = s + k, os.path.basename(path)
            x, y = (k % cols) * TW, (k // cols) * (TH + 18)
            try:
                im = Image.open(f'{outdir}/thumbs/{name}.jpg')
                im.thumbnail((TW, TH))
                sheet.paste(im, (x + (TW - im.width) // 2, y))
            except OSError:
                d.text((x + 5, y + 50), 'no frame', fill='red', font=font)
            d.text((x + 3, y + TH + 1), f"{i} {os.path.splitext(name)[0]} {start[11:16]} {f['duration']:.0f}s",
                   fill='white', font=font)
            index.append(dict(i=i, name=name, path=path, start=start, duration=f['duration'], family=f['family']))
        sheet.save(f'{outdir}/sheet_{s // n:02d}.jpg', quality=80)
    json.dump(index, open(f'{outdir}/index.json', 'w'), indent=0)
    print(f'{(len(vids) + n - 1) // n} sheets + index.json in {outdir}')


# ---------------------------------------------------------------- 2./3. colors file -> timeline
def load_scheme(path: str | None) -> dict:
    scheme = json.loads(json.dumps(DEFAULT_SCHEME))
    src = path or os.path.expanduser('~/.resolve-multicam-sync.json')
    user = json.load(open(src)) if os.path.exists(src) else {}
    user = user.get('content_colors', user)
    scheme['categories'].update(user.get('categories', {}))
    scheme['second_camera'].update(user.get('second_camera', {}))
    scheme['main_camera'] = user.get('main_camera', scheme['main_camera'])
    return scheme


def resolve_colors(index: list[dict], rules_text: str, scheme: dict) -> dict[str, str]:
    by_i = {e['i']: e for e in index}
    by_name = {e['name']: e for e in index}
    fams = [e['family'] for e in index]
    main = scheme['main_camera'] or max(set(fams), key=fams.count)
    out, bad = {}, []
    for ln, line in enumerate(rules_text.splitlines(), 1):
        line = line.split('#')[0].strip()
        if not line:
            continue
        what, _, tag = line.rpartition(' ')
        what = what.strip()
        if tag not in COLORS and tag not in scheme['categories']:
            bad.append(f'line {ln}: unknown color or category "{tag}"')
            continue
        if what in by_name:
            entries = [by_name[what]]
        else:
            try:
                a, _, b = what.partition('-')
                entries = [by_i[i] for i in range(int(a), int(b or a) + 1) if i in by_i]
            except ValueError:
                bad.append(f'line {ln}: "{what}" is not a sheet number, range or file name')
                continue
        for e in entries:
            color = tag if tag in COLORS else scheme['categories'][tag]
            alt = scheme['second_camera'].get(tag)
            is_main = label_of(e['family']).startswith(main) or e['family'] == main
            out[e['name']] = alt if alt and not is_main else color
    for b in bad:
        print('  !', b)
    return out


def apply(index_path: str, rules_path: str, timeline: str, scheme_path: str | None = None, dry_run=False):
    from .build import connect_resolve
    index = json.load(open(index_path))
    colors = resolve_colors(index, open(rules_path).read(), load_scheme(scheme_path))
    missing = [e['name'] for e in index if e['name'] not in colors]
    print(f'{len(colors)} of {len(index)} clips have a color' + (f'; no rule for {len(missing)}: {", ".join(missing[:10])}'
                                                                   + (' ...' if len(missing) > 10 else '') if missing else ''))
    if dry_run:
        from collections import Counter
        for c, n in Counter(colors.values()).most_common():
            print(f'  {c:10s} {n}')
        return
    p = connect_resolve().GetProjectManager().GetCurrentProject()
    tl = next((p.GetTimelineByIndex(i) for i in range(1, p.GetTimelineCount() + 1)
               if p.GetTimelineByIndex(i).GetName() == timeline), None)
    if not tl:
        sys.exit(f'No timeline named "{timeline}" in the open project.')
    done = 0
    for k in range(1, tl.GetTrackCount('video') + 1):
        for it in tl.GetItemListInTrack('video', k) or []:
            mpi = it.GetMediaPoolItem()
            name = mpi.GetName() if mpi else it.GetName()
            if name in colors and it.SetClipColor(colors[name]):
                done += 1
    print(f'Colored {done} clips on "{timeline}".')


def main(argv=None):
    ap = argparse.ArgumentParser(prog='resolve_multicam_sync.review', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('sheets', help='make contact sheets from a sync --report')
    s.add_argument('report')
    s.add_argument('outdir')
    a = sub.add_parser('apply', help='recolor a timeline from a colors file')
    a.add_argument('index')
    a.add_argument('rules')
    a.add_argument('--timeline', required=True)
    a.add_argument('--scheme', help='JSON with "content_colors" (default: ~/.resolve-multicam-sync.json)')
    a.add_argument('--dry-run', action='store_true')
    o = ap.parse_args(argv)
    if o.cmd == 'sheets':
        sheets(o.report, os.path.expanduser(o.outdir))
    else:
        apply(o.index, o.rules, o.timeline, o.scheme, o.dry_run)


if __name__ == '__main__':
    main()
