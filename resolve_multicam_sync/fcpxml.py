"""Write the synced layout as FCPXML 1.10, which imports into DaVinci Resolve (free or Studio),
Final Cut Pro, and (via XML import) Premiere Pro.

Layout: a gap clip spans the whole timeline as the primary storyline; every camera is a lane above it
(lane 1 = first camera track), every audio track a lane below it (lane -1 = first audio track).
Times are exact rationals on frame / sample boundaries."""
from __future__ import annotations

import os
from fractions import Fraction
from urllib.parse import quote
from xml.sax.saxutils import quoteattr

KNOWN = {23.976: Fraction(1001, 24000), 24: Fraction(1, 24), 25: Fraction(1, 25), 29.97: Fraction(1001, 30000),
         30: Fraction(1, 30), 50: Fraction(1, 50), 59.94: Fraction(1001, 60000), 60: Fraction(1, 60)}


def frame_duration(fps: float) -> Fraction:
    for k, v in KNOWN.items():
        if abs(fps - k) < 0.01:
            return v
    return Fraction(1, round(fps)) if fps > 0 else Fraction(1001, 24000)


def t(x: Fraction) -> str:
    x = Fraction(x)
    return f'{x.numerator}s' if x.denominator == 1 else f'{x.numerator}/{x.denominator}s'


def _src_time(seconds: float, m) -> Fraction:
    """Snap a time inside a file to that file's own frames (video) or samples (audio)."""
    if m.kind == 'video' and m.extra.get('fps'):
        fd = frame_duration(m.extra['fps'])
        return round(Fraction(seconds) / fd) * fd
    sr = m.extra.get('sample_rate') or 48000
    return Fraction(round(seconds * sr), sr)


def write(placements, media_by_path: dict, vtracks: list, alabels: list, path: str, name: str,
          fps: float = 23.976, width: int = 3840, height: int = 2160, markers=()):
    fd = frame_duration(fps)
    used = sorted({p.path for p in placements})
    formats, fmt_id = {}, {}
    res = [f'<format id="r1" name="FFVideoFormatRateUndefined" frameDuration="{t(fd)}" width="{width}" height="{height}"/>']
    nid = 2
    for pth in used:
        m = media_by_path[pth]
        if m.kind == 'video':
            key = (m.extra.get('width') or width, m.extra.get('height') or height, round(m.extra.get('fps') or fps, 3))
            if key not in formats:
                formats[key] = f'r{nid}'; nid += 1
                res.append(f'<format id="{formats[key]}" name="FFVideoFormatRateUndefined" '
                           f'frameDuration="{t(frame_duration(key[2]))}" width="{key[0]}" height="{key[1]}"/>')
    assets = {}
    for pth in used:
        m = media_by_path[pth]
        aid = f'r{nid}'; nid += 1
        assets[pth] = aid
        start = _src_time(m.extra.get('tc_start') or 0.0, m)
        dur = _src_time(m.duration, m)
        attrs = [f'id="{aid}"', f'name={quoteattr(m.name)}', f'start="{t(start)}"', f'duration="{t(dur)}"',
                 f'hasVideo="{1 if m.kind == "video" else 0}"', f'hasAudio="{1 if m.has_audio else 0}"']
        if m.kind == 'video':
            key = (m.extra.get('width') or width, m.extra.get('height') or height, round(m.extra.get('fps') or fps, 3))
            attrs.append(f'format="{formats[key]}"')
        if m.has_audio:
            attrs += ['audioSources="1"', f'audioChannels="{m.extra.get("channels") or 2}"',
                      f'audioRate="{m.extra.get("sample_rate") or 48000}"']
        src = 'file://' + quote(os.path.realpath(pth))
        res.append(f'<asset {" ".join(attrs)}><media-rep kind="original-media" src={quoteattr(src)}/></asset>')

    vlane = {n: i for i, n in enumerate(vtracks, 1)}
    alane = {n: -i for i, n in enumerate(alabels, 1)}
    total = max(round((p.rec + p.dur) / fd) for p in placements) + 1
    clips = []
    for p in sorted(placements, key=lambda p: (p.rec, p.kind)):
        m = media_by_path[p.path]
        a = round(Fraction(p.rec) / fd)
        n = round(Fraction(p.rec + p.dur) / fd) - a
        if n <= 0:
            continue
        start = _src_time(m.extra.get('tc_start') or 0.0, m) + _src_time(p.src_in, m)
        if p.kind == 'video':
            lane, enable = vlane[p.track], 'video'
        elif p.kind == 'camaudio':
            lane, enable = alane[p.track], 'audio'
        else:
            lane, enable = alane[p.track], 'all'
        clips.append(f'<asset-clip ref="{assets[p.path]}" lane="{lane}" offset="{t(a * fd)}" start="{t(start)}" '
                     f'duration="{t(n * fd)}" name={quoteattr(m.name)} srcEnable="{enable}"/>')
    mk = [f'<marker start="{t(round(Fraction(sec) / fd) * fd)}" duration="{t(fd)}" value={quoteattr(label)}/>'
          for sec, _, label in markers]
    xml = ['<?xml version="1.0" encoding="UTF-8"?>', '<!DOCTYPE fcpxml>', '<fcpxml version="1.10">',
           '<resources>', *res, '</resources>',
           '<library>', f'<event name={quoteattr(name)}>', f'<project name={quoteattr(name)}>',
           f'<sequence format="r1" duration="{t(total * fd)}" tcStart="0s" tcFormat="NDF" audioLayout="stereo" audioRate="48k">',
           '<spine>', f'<gap name="Timeline" offset="0s" start="0s" duration="{t(total * fd)}">', *mk, *clips, '</gap>',
           '</spine>', '</sequence>', '</project>', '</event>', '</library>', '</fcpxml>']
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(xml) + '\n')
    return len(clips)
