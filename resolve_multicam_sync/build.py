"""Lay the synced files out as a timeline plan, then build it in DaVinci Resolve."""
from __future__ import annotations

import datetime as dt
import os
import re
import sys
from collections import defaultdict, Counter
from dataclasses import dataclass

from .media import Media

PALETTE = ['Orange', 'Navy', 'Purple', 'Teal', 'Pink', 'Yellow', 'Green', 'Tan', 'Lime', 'Blue', 'Brown',
           'Apricot', 'Violet', 'Olive', 'Beige', 'Chocolate']


@dataclass
class Placement:
    path: str
    kind: str          # video / audio
    track: str         # track label
    rec: float         # seconds from timeline start
    src_in: float      # seconds into the file
    dur: float         # seconds
    color: str | None
    real: dt.datetime  # real time of this placement's first frame


def label_of(family: str) -> str:
    lab = re.sub(r'[_\-\s]{2,}', '_', family.replace('#', '')).strip(' _-.') or family
    return lab


# ---------------------------------------------------------------- colors
def _hhmm(s: str) -> dt.time:
    h, m, *rest = (s.split(':') + ['0'])[:3]
    return dt.time(int(h), int(m), int(rest[0]))


class Colors:
    """camera colors < time-of-day windows < all-day camera colors < per-file overrides. See examples/colors.example.json."""

    def __init__(self, cfg: dict | None, families: list[str]):
        cfg = cfg or {}
        self.clip = cfg.get('clip_colors', {})
        self.windows = [dict(w, _a=_hhmm(w['from']), _b=_hhmm(w['to'])) for w in cfg.get('time_colors', [])]
        cam = cfg.get('camera_colors', {})
        self.cam = {f: self._lookup(cam, f, families) or PALETTE[i % len(PALETTE)] for i, f in enumerate(families)}
        # cameras whose color holds all day, over the time-of-day windows (e.g. the drone)
        always = cfg.get('camera_colors_always', {})
        self.always = {f: c for f in families if (c := self._lookup(always, f, families))}

    @staticmethod
    def _lookup(table: dict, family: str, families: list[str]) -> str | None:
        """Exact family or label first; otherwise a key that starts the label, unless that key is
        exactly another camera's label (so "DJI" means the DJI_0001 camera, not DJI_..._D too)."""
        lab = label_of(family)
        exact = {label_of(f) for f in families}
        return table.get(family) or table.get(lab) or next(
            (v for k, v in table.items() if lab.startswith(k) and k not in exact), None)

    def __call__(self, m: Media, start: dt.datetime) -> str | None:
        if m.name in self.clip:
            return self.clip[m.name]
        if m.kind == 'video' and m.family in self.always:
            return self.always[m.family]
        t = start.time()
        for w in self.windows:
            cams = w.get('cameras')
            if cams and (m.kind != 'video' or (label_of(m.family) not in cams and m.family not in cams)):
                continue
            if w['_a'] <= t < w['_b']:
                return w['color']
        return self.cam.get(m.family) if m.kind == 'video' else None

    def markers(self):
        return [(w['_a'], w.get('marker_color', w['color']), w['label']) for w in self.windows if w.get('label')]


# ---------------------------------------------------------------- layout
def _keep_camera_audio(m: Media, mode: str, cams: set, clips: set) -> bool:
    if not m.has_audio or 'ltc' in m.extra:      # a channel carrying timecode is a loud tone, never usable sound
        return False
    if m.name in clips:
        return True
    if mode in ('all', 'auto'):
        return True
    return label_of(m.family) in cams or m.family in cams


def _subtract(a: float, b: float, cover: list) -> list:
    """Parts of [a, b) not covered by the (sorted, merged) intervals in `cover`."""
    out, cur = [], a
    for s, e in cover:
        if e <= cur or s >= b:
            continue
        if s > cur:
            out.append((cur, min(s, b)))
        cur = max(cur, e)
        if cur >= b:
            break
    if cur < b:
        out.append((cur, b))
    return [(x, y) for x, y in out if y - x >= 0.5]


def plan(media: list[Media], starts: dict, colors: Colors, close_gaps: bool = False, pad: float = 2.0,
         camera_audio: str = 'none', camera_audio_clips=()):
    """camera_audio: 'none' (default), 'all', 'auto' (only where no recorder was running), or a
    comma-separated list of camera labels.  camera_audio_clips: file names to keep regardless."""
    vids = [m for m in media if m.kind == 'video']
    auds = [m for m in media if m.kind == 'audio']
    base = min(starts[m.path] for m in media) - dt.timedelta(seconds=pad)

    def sec(t): return (t - base).total_seconds()

    # video tracks: one per camera, biggest camera first; a camera that still overlaps itself
    # (two bodies sharing a file-name pattern) spills onto an extra track for that camera
    fams = Counter(m.family for m in vids)
    order = [f for f, _ in fams.most_common()]
    out = []
    lanes = defaultdict(list)     # family -> [end of each lane]
    for m in sorted(vids, key=lambda m: starts[m.path]):
        s, e = sec(starts[m.path]), sec(starts[m.path]) + m.duration
        lane = next((i for i, end in enumerate(lanes[m.family]) if end <= s + 1e-3), None)
        if lane is None:
            lanes[m.family].append(e); lane = len(lanes[m.family]) - 1
        else:
            lanes[m.family][lane] = e
        name = label_of(m.family) + (f' ({lane + 1})' if lane else '')
        out.append(Placement(m.path, 'video', name, s, 0.0, m.duration, colors(m, starts[m.path]), starts[m.path]))
    # audio tracks: pack recorders that never overlap onto the same track
    alanes = []                   # [end, [labels]]
    amap = {}
    for m in sorted(auds, key=lambda m: starts[m.path]):
        s, e = sec(starts[m.path]), sec(starts[m.path]) + m.duration
        i = next((i for i, (end, _) in enumerate(alanes) if end <= s + 1e-3), None)
        if i is None:
            alanes.append([e, [m.name]]); i = len(alanes) - 1
        else:
            alanes[i][0] = e; alanes[i][1].append(m.name)
        amap[m.path] = i
    alabels = []
    for i, (_, names) in enumerate(alanes):
        stems = [os.path.splitext(n)[0] for n in names]
        lab = stems[0] if len(stems) == 1 else (os.path.commonprefix(stems).strip(' _-') or f'Audio {i + 1}') + f' (+{len(stems) - 1})'
        alabels.append(f'A{i + 1} {lab}'[:40])
    for m in auds:
        out.append(Placement(m.path, 'audio', alabels[amap[m.path]], sec(starts[m.path]), 0.0, m.duration,
                             colors(m, starts[m.path]), starts[m.path]))

    # camera scratch audio, one track per kept camera, never by default
    mode = (camera_audio or 'none').strip()
    cams = set() if mode in ('none', 'all', 'auto') else {c.strip() for c in mode.split(',') if c.strip()}
    clipset = set(camera_audio_clips or ())
    cover = []
    for a, b in sorted((p.rec, p.rec + p.dur) for p in out if p.kind == 'audio'):
        if cover and a <= cover[-1][1]:
            cover[-1][1] = max(cover[-1][1], b)
        else:
            cover.append([a, b])
    cam_tracks = []
    for p in [p for p in out if p.kind == 'video']:
        m = next(x for x in vids if x.path == p.path)
        if not _keep_camera_audio(m, mode, cams, clipset):
            continue
        tname = f'{p.track} cam audio'
        spans = _subtract(p.rec, p.rec + p.dur, cover) if mode == 'auto' and m.name not in clipset else [(p.rec, p.rec + p.dur)]
        if spans and tname not in cam_tracks:
            cam_tracks.append(tname)
        for a, b in spans:
            off = a - p.rec
            out.append(Placement(p.path, 'camaudio', tname, a, p.src_in + off, b - a, p.color,
                                 p.real + dt.timedelta(seconds=off)))
    alabels = alabels + sorted(cam_tracks)

    if close_gaps:
        out = _close_gaps(out)
    vtracks = []
    for f in order:
        for lane in range(len(lanes[f])):
            vtracks.append(label_of(f) + (f' ({lane + 1})' if lane else ''))
    return out, vtracks, alabels, base


def _close_gaps(ps: list[Placement]) -> list[Placement]:
    """Keep only time where at least one camera is rolling; audio is cut to those stretches."""
    iv = sorted((p.rec, p.rec + p.dur) for p in ps if p.kind == 'video')
    blocks = []
    for a, b in iv:
        if blocks and a <= blocks[-1][1] + 1e-3:
            blocks[-1][1] = max(blocks[-1][1], b)
        else:
            blocks.append([a, b])
    shift, acc, prev = [], 0.0, 0.0
    for a, b in blocks:
        acc += a - prev; shift.append(acc); prev = b
    import bisect
    starts = [a for a, _ in blocks]
    out = []
    for p in ps:
        if p.kind in ('video', 'camaudio'):
            j = bisect.bisect_right(starts, p.rec + 1e-6) - 1
            out.append(Placement(p.path, p.kind, p.track, p.rec - shift[j], p.src_in, p.dur, p.color, p.real))
            continue
        for j, (a, b) in enumerate(blocks):
            s, e = max(p.rec, a), min(p.rec + p.dur, b)
            if e - s < 0.1:
                continue
            off = s - p.rec
            out.append(Placement(p.path, p.kind, p.track, s - shift[j], p.src_in + off, e - s, p.color,
                                 p.real + dt.timedelta(seconds=off)))
    return out


# ---------------------------------------------------------------- Resolve
def connect_resolve():
    try:
        import DaVinciResolveScript as dvr  # noqa
    except ImportError:
        guesses = {
            'darwin': ('/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules',
                       '/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so'),
            'win32': (os.path.expandvars(r'%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules'),
                      r'C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll'),
            'linux': ('/opt/resolve/Developer/Scripting/Modules', '/opt/resolve/libs/Fusion/fusionscript.so'),
        }
        mod, lib = guesses.get(sys.platform, guesses['linux'])
        os.environ.setdefault('RESOLVE_SCRIPT_LIB', lib)
        sys.path.append(mod)
        import DaVinciResolveScript as dvr  # noqa
    r = dvr.scriptapp('Resolve')
    if not r:
        raise SystemExit('Could not reach DaVinci Resolve. Is it open, and is Preferences > System > General > '
                         '"External scripting using" set to Local? (External scripting needs DaVinci Resolve Studio.)')
    return r


def _fps(tl) -> float:
    v = float(tl.GetSetting('timelineFrameRate') or 24)
    for exact in (24000 / 1001, 30000 / 1001, 60000 / 1001):
        if abs(v - exact) < 0.01:
            return exact
    return v


def build(placements, vtracks, alabels, base, timeline_name, bin_name, markers=(), log=print):
    r = connect_resolve()
    proj = r.GetProjectManager().GetCurrentProject()
    if not proj:
        raise SystemExit('Open a project in Resolve first.')
    mp = proj.GetMediaPool()
    root = mp.GetRootFolder()
    folder = next((f for f in root.GetSubFolderList() or [] if f.GetName() == bin_name), None) or mp.AddSubFolder(root, bin_name)
    have = {c.GetClipProperty('File Path'): c for c in folder.GetClipList() or []}
    real = lambda path: os.path.realpath(path)     # import the real file, never a symlink that may vanish
    need = sorted({real(p.path) for p in placements} - set(have))
    if need:
        mp.SetCurrentFolder(folder)
        for c in mp.ImportMedia(need) or []:
            have[c.GetClipProperty('File Path')] = c
        log(f'  imported {len(need)} files into bin "{bin_name}"')
    existing = {proj.GetTimelineByIndex(i).GetName() for i in range(1, proj.GetTimelineCount() + 1)}
    name, k = timeline_name, 2
    while name in existing:
        name, k = f'{timeline_name} ({k})', k + 1
    mp.SetCurrentFolder(folder)
    tl = mp.CreateEmptyTimeline(name)
    proj.SetCurrentTimeline(tl)
    F = _fps(tl)
    T0 = tl.GetStartFrame()
    while tl.GetTrackCount('video') < len(vtracks):
        tl.AddTrack('video')
    while tl.GetTrackCount('audio') < len(alabels):
        tl.AddTrack('audio', 'stereo')
    for i, n in enumerate(vtracks, 1):
        tl.SetTrackName('video', i, n)
    for i, n in enumerate(alabels, 1):
        tl.SetTrackName('audio', i, n)
    vidx = {n: i for i, n in enumerate(vtracks, 1)}
    aidx = {n: i for i, n in enumerate(alabels, 1)}
    failed = []
    for p in sorted(placements, key=lambda p: p.rec):
        mpi = have.get(real(p.path))
        if not mpi:
            failed.append(p.path); continue
        fps = float(mpi.GetClipProperty('FPS') or F)
        rec = T0 + round(p.rec * F)
        n = round((p.rec + p.dur) * F) - round(p.rec * F)
        st = round(p.src_in * fps)
        track = vidx[p.track] if p.kind == 'video' else aidx[p.track]
        audio_like = p.kind != 'video'
        got = None
        for extra in (-1, 0, 0.5, 1.0, 1.5) if audio_like else (-1, -2, -3):
            end = st + n * fps / F + extra if audio_like else min(st + n * fps / F, st + p.dur * fps) + extra
            g = mp.AppendToTimeline([{'mediaPoolItem': mpi, 'startFrame': st, 'endFrame': end, 'recordFrame': rec,
                                      'trackIndex': track, 'mediaType': 2 if audio_like else 1}])
            g = g[0] if g else None
            if g and abs(g.GetDuration() - n) <= 2:
                got = g; break
            if g:
                tl.DeleteClips([g], False)
        if got and p.color:
            got.SetClipColor(p.color)
        if not got:
            failed.append(p.path)
    for sec, color, label in markers:      # (seconds on the timeline, color, name)
        tl.AddMarker(round(sec * F), color, label, '', 1)
    r.GetProjectManager().SaveProject()
    return name, failed
