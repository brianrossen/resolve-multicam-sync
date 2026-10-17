"""Work out the parts of the day (getting ready, first looks, ceremony, dinner, dancing ...) from the audio
recordings' names and synced times, and turn them into time-of-day color windows + markers.

Enable with "auto_day_parts": true (or a dict of overrides) in the defaults file or a --config.
Explicit "time_colors" in a config still win over the derived ones."""
from __future__ import annotations

import datetime as dt
import re

from .build import label_of

PARTS = {   # part: (label, default color, file-name keywords)
    'vows':        ('Private vows', 'Yellow', ['vow', 'letter']),
    'first_looks': ('First looks', 'Orange', ['first look', 'firstlook', 'fob', 'mob', 'fog', 'mog', 'reveal']),
    'ceremony':    ('Ceremony', 'Orange', ['ceremony', 'officiant', 'processional', 'pastor', 'priest', 'rabbi']),
    'dinner':      ('Dinner + toasts', 'Green', ['welcome', 'speech', 'toast', 'dinner', 'blessing']),
    'exit':        ('Last dance + exit', 'Brown', ['exit', 'send off', 'sendoff', 'send-off', 'last dance', 'sparkler']),
    'dancing':     ('Dancing', 'Brown', ['dj', 'dance', 'party']),
}
FILL = {    # gaps between detected parts
    'getting_ready':  ('Getting ready', 'Pink'),
    'portraits':      ('Portraits', 'Yellow'),
    'after_ceremony': ('Portraits + cocktails', 'Yellow'),
}
EXCLUDE = ['prayer', 'test', 'backup', 'copy']


def _has(name: str, kw: str) -> bool:
    if len(kw) <= 3:                              # short tokens stand alone (plural ok): "fob", "vows", not "mobile"
        return re.search(rf'(^|[^a-z]){re.escape(kw)}s?([^a-z]|$)', name) is not None
    return kw in name


def classify(filename: str, keywords: dict) -> str | None:
    n = filename.lower()
    if any(x in n for x in EXCLUDE):
        return None
    for part, kws in keywords.items():
        if any(_has(n, k) for k in kws):
            return part
    return None


def derive(media, starts: dict, families: list[str], reference: str, opts) -> list[dict]:
    opts = opts if isinstance(opts, dict) else {}
    colors = {**{p: v[1] for p, v in PARTS.items()}, **{p: v[1] for p, v in FILL.items()}, **opts.get('colors', {})}
    keywords = {p: opts.get('keywords', {}).get(p, v[2]) for p, v in PARTS.items()}
    labels = {**{p: v[0] for p, v in PARTS.items()}, **{p: v[0] for p, v in FILL.items()}}

    vids = [m for m in media if m.kind == 'video']
    if not vids:
        return []
    day_a = min(starts[m.path] for m in vids)
    day_b = max(starts[m.path] + dt.timedelta(seconds=m.duration) for m in vids)
    spans = {}
    for m in media:
        if m.kind != 'audio':
            continue
        part = classify(m.name, keywords)
        if not part:
            continue
        a, b = starts[m.path], starts[m.path] + dt.timedelta(seconds=m.duration)
        spans[part] = (min(a, spans[part][0]), max(b, spans[part][1])) if part in spans else (a, b)
    if not spans:
        return []
    found = sorted(spans.items(), key=lambda x: x[1][0])
    seq = []                                       # (part, start, end) without overlaps
    for i, (part, (a, b)) in enumerate(found):
        if i + 1 < len(found):
            b = min(b, found[i + 1][1][0])
        if seq and a < seq[-1][2]:
            a = seq[-1][2]
        if b > a:
            seq.append((part, a, b))
    out = []                                       # fill the gaps
    cur = day_a
    for part, a, b in seq:
        if a > cur:
            prev = out[-1][0] if out else None
            gap = ('getting_ready' if prev in (None, 'vows') else
                   'after_ceremony' if prev == 'ceremony' else
                   'portraits' if part == 'ceremony' else
                   'dancing' if part == 'exit' and prev == 'dinner' else None)
            if gap:
                out.append((gap, cur, a))
            elif out:                              # otherwise the earlier part runs until the next one
                out[-1] = (out[-1][0], out[-1][1], a)
        out.append((part, a, b))
        cur = b
    if cur < day_b:                                # after the last detected part
        last = out[-1][0]
        tail = 'dancing' if last in ('dinner', 'dancing') else last
        if tail == last:
            out[-1] = (last, out[-1][1], day_b)
        else:
            out.append((tail, cur, day_b))

    # "second camera" = every camera except the main one (default: the camera with the most clips)
    main = opts.get('main_camera')
    others = [label_of(f) for f in families
              if (not label_of(f).startswith(main) if main else f != reference)]
    windows = []
    for part, a, b in out:
        fmt = lambda t: t.strftime('%H:%M:%S')
        end = b if b.date() == a.date() else dt.datetime.combine(a.date(), dt.time(23, 59, 59))
        # optional: a different color for every camera except the main one in this part (e.g. second shooter)
        alt = opts.get('second_camera', {}).get(part)
        if alt and others:
            windows.append(dict(**{'from': fmt(a), 'to': fmt(end)}, color=alt, cameras=others))
        windows.append(dict(**{'from': fmt(a), 'to': fmt(end)}, color=colors[part], label=labels[part]))
    return windows
