"""resolve-multicam-sync: sync every camera and audio recorder in a folder by sound, then build a
DaVinci Resolve timeline with each camera on its own track.

  python3 -m resolve_multicam_sync "/path/to/Shoot Day" [--timeline "Shoot Day"] [--keep-gaps]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from collections import Counter, defaultdict

from . import audio, build, media as mediamod, solve


def parse_hours(s: str) -> float:
    s = s.strip().lower()
    if s.endswith('h'):
        return float(s[:-1]) * 3600
    if s.endswith('m'):
        return float(s[:-1]) * 60
    return float(s)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='resolve-multicam-sync', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('folders', nargs='+', help='folder(s) with the camera files and audio recordings')
    ap.add_argument('--timeline', help='new timeline name (default: the first folder name)')
    ap.add_argument('--bin', help='media pool bin to import into (default: same as the timeline name)')
    ap.add_argument('--config', help='JSON with camera colors, time-of-day colors/markers, per-file colors')
    ap.add_argument('--close-gaps', dest='close_gaps', action='store_true', default=True,
                    help='remove time where no camera is rolling, audio trimmed to match (this is the default)')
    ap.add_argument('--keep-gaps', dest='close_gaps', action='store_false',
                    help='keep the real-time gaps between clips instead')
    ap.add_argument('--camera-audio', default='none',
                    help="keep camera scratch audio: none (default), all, auto (only where no recorder was running), "
                         "or camera labels like 'C,B'. Single clips: \"camera_audio_clips\" in --config")
    ap.add_argument('--fcpxml', help='write an FCPXML file (Resolve free/Studio, Final Cut, Premiere) instead of building in Resolve')
    ap.add_argument('--fps', type=float, default=None, help='timeline frame rate for --fcpxml (default 23.976)')
    ap.add_argument('--resolution', default=None, help='timeline size for --fcpxml, e.g. 3840x2160 (default: most common camera size)')
    ap.add_argument('--max-skew', default='2h', help='how wrong any clock may be, e.g. 2h, 90m (default 2h)')
    ap.add_argument('--no-recursive', action='store_true', help='only the top level of each folder')
    ap.add_argument('--skip', action='append', default=[], help='skip files whose name contains this text (repeatable)')
    ap.add_argument('--no-timecode', action='store_true',
                    help='ignore timecode (LTC on camera audio / recorder timecode) and sync by audio only')
    ap.add_argument('--dry-run', action='store_true', help='analyze and print the plan, do not touch Resolve')
    ap.add_argument('--report', help='write the full sync result as JSON here')
    # personal defaults: ~/.resolve-multicam-sync.json, e.g. {"camera_audio": "auto", "close_gaps": true,
    # "colors": {"camera_colors_always": {"DJI": "Lime"}}}  ("colors" is merged under any --config)
    dpath = os.path.expanduser(os.environ.get('RMS_DEFAULTS', '~/.resolve-multicam-sync.json'))
    defaults = json.load(open(dpath)) if os.path.exists(dpath) else {}
    ap.set_defaults(**{k: v for k, v in defaults.items() if k in ('camera_audio', 'close_gaps', 'max_skew', 'config', 'fps', 'resolution')})
    a = ap.parse_args(argv)
    a.skip = list(a.skip) + list(defaults.get('skip', []))
    if defaults:
        print(f'Using your defaults from {dpath}: ' + ', '.join(f'{k}={v}' for k, v in defaults.items() if not k.startswith('_')))

    t0 = time.time()
    paths = mediamod.scan(a.folders, recursive=not a.no_recursive)
    paths = [p for p in paths if not any(s.lower() in os.path.basename(p).lower() for s in a.skip)]
    if not paths:
        sys.exit('No video or audio files found.')
    print(f'Scanning {len(paths)} files...')
    media = mediamod.probe_all(paths)
    vids = [m for m in media if m.kind == 'video']
    recs = [m for m in media if m.kind == 'audio']
    print(f'  {len(vids)} video clips from {len({m.family for m in vids})} cameras, {len(recs)} audio recordings')

    res = None
    if not a.no_timecode:
        from . import timecode
        print('Checking for timecode (LTC on the camera audio + recorder timecode)...')
        if timecode.detect(media):
            ntsc = any(m.extra['ltc']['ntsc'] for m in media if 'ltc' in m.extra)
            tries = []
            for mult in ((1.001, 1.0) if ntsc else (1.0,)):   # recorder TC in wall-clock vs timecode-label seconds
                pl = timecode.place(media, log=lambda *_: None, mult=mult)
                if pl:
                    mv = timecode.verify(media, pl[0], timecode.envelope)
                    agree = [x for x in mv if abs(x) < 0.25]
                    tries.append((-len(agree), mult != 1.001, mult, pl, agree or mv))
            tries.sort(key=lambda t: (t[0], t[1]))         # most clips agreeing with the audio wins
            placed = tries[0][3] if tries else timecode.place(media)
            moves_checked = tries[0][4] if tries else []
            if placed:
                res = solve.Result(starts=placed[0], how=placed[1])
                fams = Counter(m.family for m in vids)
                res.reference = fams.most_common(1)[0][0] if fams else ''
                for m in media:                       # report how far each device's own clock was off
                    key = m.family if m.kind == 'video' else m.path
                    res.deltas.setdefault(key, []).append((res.starts[m.path] - m.clock).total_seconds())
                res.deltas = {k: sorted(v)[len(v) // 2] for k, v in res.deltas.items()}
                ref = res.deltas.get(res.reference, 0.0)
                res.deltas = {k: v - ref for k, v in res.deltas.items()}
                moves = moves_checked
                if moves:
                    worst = max(abs(x) for x in moves)
                    print(f'  synced by timecode; audio spot-check of {len(moves)} clips: within {worst * 1000:.0f} ms')
                    if worst > 0.2:
                        res.warnings.append(f'audio spot-check disagrees with timecode by up to {worst:.2f}s - check sync')
                else:
                    print('  synced by timecode (no clips long enough to spot-check against the recorder audio)')
    if res is None:
        print('Reading audio fingerprints (cached after the first run)...')
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(6) as ex:
            envs = dict(zip([m.path for m in media],
                            ex.map(lambda m: audio.envelope(m.path) if m.has_audio else None, media)))

        print('Syncing by audio...')
        res = solve.solve(media, envs, solve.Params(max_skew=parse_hours(a.max_skew)))

    # summary
    by_fam = defaultdict(list)
    for m in vids:
        by_fam[m.family].append(m)
    print('\nCameras')
    for f, ms in sorted(by_fam.items(), key=lambda x: -len(x[1])):
        how = Counter(res.how[m.path].split('+')[0] for m in ms)
        ref = '  (reference)' if f == res.reference else ''
        print(f'  {build.label_of(f):18s} {len(ms):4d} clips  clock off by {res.deltas.get(f, 0):+9.1f}s  ' +
              (f'timecode {how.get("timecode", 0):4d}  ' if how.get("timecode") else '') +
              f'audio-locked {how.get("audio", 0):4d}  by camera offset {how.get("camera-curve", 0) + how.get("clock", 0) + how.get("clock (timecode-corrected)", 0) + how.get("clock (corrected by other cameras)", 0):4d}{ref}')
    print('Audio recordings')
    for m in sorted(recs, key=lambda m: res.starts[m.path]):
        print(f'  {m.name:32s} clock off by {res.deltas.get(m.path, 0):+9.1f}s  starts {res.starts[m.path]:%H:%M:%S}  ({res.how[m.path]})')
    for w in res.warnings:
        print('  !', w)

    cfg = json.load(open(os.path.expanduser(a.config))) if a.config else None
    if defaults.get('colors'):            # personal color rules sit under the job's own config
        merged = dict(defaults['colors'])
        for k, v in (cfg or {}).items():
            merged[k] = {**merged[k], **v} if isinstance(v, dict) and isinstance(merged.get(k), dict) else v
        cfg = merged
    families = [f for f, _ in Counter(m.family for m in vids).most_common()]
    if (cfg or {}).get('auto_day_parts'):
        from . import dayparts
        derived = dayparts.derive(media, res.starts, families, res.reference, cfg['auto_day_parts'])
        cfg = dict(cfg); cfg['time_colors'] = list(cfg.get('time_colors', [])) + derived
        if derived:
            print('Parts of the day (from the recording names):')
            for w in derived:
                if w.get('label'):
                    print(f"  {w['from'][:5]}-{w['to'][:5]}  {w['label']:24s} {w['color']}")
        else:
            print('  ! auto_day_parts: no recording names matched a part of the day; using camera colors')
    for name, cut in ((cfg or {}).get('trim_out') or {}).items():     # e.g. a camera left recording: keep the first N seconds
        for m in media:
            if m.name == name and cut < m.duration:
                print(f'  trimming {name} to its first {cut:g}s (was {m.duration:.0f}s)')
                m.duration = float(cut)
    colors = build.Colors(cfg, families)
    placements, vtracks, alabels, base = build.plan(media, res.starts, colors, close_gaps=a.close_gaps,
                                                    camera_audio=a.camera_audio,
                                                    camera_audio_clips=(cfg or {}).get('camera_audio_clips', []))
    end = max(p.rec + p.dur for p in placements)
    markers = []
    for t, col, label in colors.markers():
        real = dt.datetime.combine(base.date(), t)
        first = min((p for p in placements if p.kind == 'video' and p.real + dt.timedelta(seconds=p.dur) > real),
                    key=lambda p: p.rec, default=None)
        if first:
            markers.append((max(first.rec, first.rec + (real - first.real).total_seconds()), col, label))
    print(f'\nTimeline plan: {len(vtracks)} video tracks, {len(alabels)} audio tracks, '
          f'{end / 3600:.2f} h long{" (gaps closed)" if a.close_gaps else ""}')

    if a.report:
        json.dump(dict(reference=res.reference, deltas=res.deltas, edges=res.edges, warnings=res.warnings,
                       files={m.path: dict(start=res.starts[m.path].isoformat(), how=res.how[m.path], family=m.family,
                                           kind=m.kind, duration=m.duration) for m in media}),
                  open(os.path.expanduser(a.report), 'w'), indent=1, default=str)
        print(f'Report written to {a.report}')
    tname = a.timeline or os.path.basename(os.path.normpath(os.path.expanduser(a.folders[0])))
    if a.dry_run:
        print(f'Dry run: nothing written, Resolve untouched. ({time.time() - t0:.0f}s)')
        return
    if a.fcpxml:
        from . import fcpxml
        sizes = Counter((m.extra.get('width'), m.extra.get('height')) for m in vids if m.extra.get('width'))
        w, h = (map(int, a.resolution.lower().split('x')) if a.resolution else (sizes.most_common(1)[0][0] if sizes else (3840, 2160)))
        out = os.path.expanduser(a.fcpxml)
        n = fcpxml.write(placements, {m.path: m for m in media}, vtracks, alabels, out, tname,
                         fps=a.fps or 23.976, width=w, height=h, markers=markers)
        print(f'Wrote {out} ({n} clips, {w}x{h} @ {a.fps or 23.976}). Import it in Resolve: File > Import > Timeline; '
              f'Final Cut: File > Import > XML. ({time.time() - t0:.0f}s)')
        return
    print(f'Building "{tname}" in Resolve...')
    name, failed = build.build(placements, vtracks, alabels, base, tname, a.bin or tname, markers)
    print(f'Done: timeline "{name}" ({len(placements) - len(failed)} placed, {len(failed)} failed) in {time.time() - t0:.0f}s')
    for f in failed[:20]:
        print('  ! could not place', os.path.basename(f))


if __name__ == '__main__':
    main()
