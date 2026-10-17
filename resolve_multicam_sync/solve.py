"""Work out where every file really happened in time, using audio.

Model: every camera family (and every audio recorder) has a clock that is off by some unknown amount,
`delta`.  real_start = file_clock + delta (+ a small per-clip correction for cameras).

Pass 1 (wide):  the longest clips of each camera are matched against every recorder within
               `max_skew` of their clock; recorders are matched against each other.  Each match is a
               vote for "delta(A) - delta(B) = v".  Agreeing votes become an edge; a spanning tree over
               the strongest edges gives every node's delta, relative to the biggest camera.
Pass 2 (narrow): every clip with audio is matched again, only a few seconds around where pass 1
               predicts it, against whatever recorder was running.  Strong matches that agree with the
               camera's overall offset are used as-is; everything else follows the camera's offset curve.
Finally no camera is allowed to overlap itself (tiny overlaps are nudged)."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

from . import audio
from .media import Media

R = audio.RATE


@dataclass
class Params:
    max_skew: float = 7200.0      # how far (s) any clock may be wrong in pass 1
    search: float = 8.0           # pass-2 search window (+/- s)
    seg_len: float = 30.0         # seconds of each clip used for matching
    min_r: float = 0.5            # minimum correlation to count as a match
    min_prom: float = 0.12        # how much the best match must beat the next best
    clip_r: float = 0.6           # per-clip: trust its own match at/above this r ...
    clip_agree: float = 1.0       # ... if it agrees with the camera curve within this many seconds
    strong_r: float = 0.8         # ... or regardless of the curve at/above this r
    curve_window: float = 1800.0  # camera-offset curve: median over +/- this many seconds
    longest_per_family: int = 12  # pass-1 clips per (camera, recorder)
    nudge: float = 1.5            # self-overlaps smaller than this are nudged, larger ones kept apart


@dataclass
class Result:
    deltas: dict = field(default_factory=dict)          # node -> seconds
    starts: dict = field(default_factory=dict)          # path -> real start (datetime)
    how: dict = field(default_factory=dict)             # path -> 'audio' | 'camera-curve' | 'recorder' | 'same-device' | 'clock'
    edges: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    reference: str = ''


def _nm(node: str) -> str:
    return node.rsplit('/', 1)[-1].replace('#', '')


def _secs(a: dt.datetime, b: dt.datetime) -> float:
    return (a - b).total_seconds()


def _window(env, center_s, half_s, n):
    lo = int(round((center_s - half_s) * R))
    hi = int(round((center_s + half_s) * R)) + n
    lo_c, hi_c = max(0, lo), min(len(env), hi)
    return lo_c, env[lo_c:hi_c]


def _segment(env, seg_len):
    """Middle `seg_len` seconds of a clip's envelope -> (start_s, segment)."""
    total = len(env) / R
    w = min(seg_len, total - 0.25)
    if w < 3:
        return None, None
    s0 = (total - w) / 2
    return s0, env[int(s0 * R):int((s0 + w) * R)]


def _segments(env, seg_len, most=8, every=45.0):
    """Several evenly spaced segments for long clips (each one is a separate vote), else the middle one."""
    total = len(env) / R
    if total < 2 * seg_len + every:
        s0, seg = _segment(env, seg_len)
        return [] if seg is None else [(s0, seg)]
    k = int(min(most, max(2, total // every)))
    out = []
    for f in np.linspace(0.05, 0.95, k):
        s0 = f * (total - seg_len)
        out.append((s0, env[int(s0 * R):int((s0 + seg_len) * R)]))
    return out


def _match(rec_env, rec_clock, clip_clock, clip_seg_t, seg, guess_extra, half, p):
    """Find `seg` inside the recorder near the clock-predicted spot.
    Returns (r, prominence, rec_t) or None. guess_extra shifts the prediction (known deltas)."""
    expect = _secs(clip_clock, rec_clock) + clip_seg_t + guess_extra
    lo, sl = _window(rec_env, expect, half, len(seg))
    c = audio.xcorr(sl, seg)
    k, r, prom = audio.peak(c)
    if k < 0:
        return None
    return r, prom, (lo + k) / R


def _consensus(votes, tol=0.3):
    """Densest cluster of votes [(v, r)] -> (value, support, mean_r) or None."""
    if not votes:
        return None
    vs = sorted(votes)
    best = None
    for i, (v, _) in enumerate(vs):
        group = [(x, r) for x, r in vs if abs(x - v) <= tol]
        score = sum(r for _, r in group)
        if best is None or score > best[0]:
            best = (score, group)
    group = best[1]
    vals = np.array([x for x, _ in group])
    return float(np.median(vals)), len(group), float(np.mean([r for _, r in group]))


def solve(media: list[Media], envs: dict, p: Params = Params(), log=print) -> Result:
    res = Result()
    vids = [m for m in media if m.kind == 'video']
    recs = [m for m in media if m.kind == 'audio' and envs.get(m.path) is not None]
    fam_clips = defaultdict(list)
    for m in vids:
        fam_clips[m.family].append(m)
    if not vids:
        res.warnings.append('no video found')
        return res
    res.reference = max(fam_clips, key=lambda f: (len(fam_clips[f]), sum(m.duration for m in fam_clips[f])))

    # ---------- pass 1: wide matching -> pairwise clock relations ----------
    votes = defaultdict(list)            # (u, w) -> [(v, r)] meaning delta(u) - delta(w) = v
    for rec in recs:
        renv = envs[rec.path]
        r0, r1 = rec.clock, rec.clock + dt.timedelta(seconds=rec.duration)
        for fam, clips in fam_clips.items():
            near = [c for c in clips if envs.get(c.path) is not None and
                    c.clock + dt.timedelta(seconds=c.duration + p.max_skew) > r0 and
                    c.clock - dt.timedelta(seconds=p.max_skew) < r1]
            for c in sorted(near, key=lambda c: -c.duration)[:p.longest_per_family]:
                for s0, seg in _segments(envs[c.path], p.seg_len):
                    m = _match(renv, rec.clock, c.clock, s0, seg, 0.0, p.max_skew, p)
                    if m and m[0] >= p.min_r and m[1] >= p.min_prom:
                        r, prom, rec_t = m
                        v = _secs(rec.clock, c.clock) + rec_t - s0          # delta(fam) - delta(rec)
                        votes[(fam, rec.path)].append((v, r))
    # recorder <-> recorder
    for i, a in enumerate(recs):
        for b in recs[i + 1:]:
            if abs(_secs(a.clock, b.clock)) > p.max_skew + max(a.duration, b.duration):
                continue
            ea, eb = envs[a.path], envs[b.path]
            for frac in np.linspace(0.02, 0.98, 25):
                t_a = frac * a.duration
                seg = ea[int(t_a * R):int((t_a + 40) * R)]
                if len(seg) < 20 * R:
                    continue
                expect = _secs(a.clock, b.clock) + t_a
                lo, sl = _window(eb, expect, p.max_skew, len(seg))
                k, r, prom = audio.peak(audio.xcorr(sl, seg))
                if k >= 0 and r >= p.min_r + 0.1 and prom >= p.min_prom:
                    t_b = (lo + k) / R
                    votes[(a.path, b.path)].append((_secs(b.clock, a.clock) + t_b - t_a, r))   # delta(a)-delta(b)

    edges = []
    rec_paths = {r.path for r in recs}
    for (u, w), vv in votes.items():
        cons = _consensus(vv)
        if not cons:
            continue
        val, support, mr = cons
        if support >= 2 or (support == 1 and mr >= p.strong_r):
            rec_rec = u in rec_paths and w in rec_paths
            edges.append(dict(u=u, w=w, v=val, support=support, r=mr, total=len(vv), exact=rec_rec))
    res.edges = edges

    # maximum spanning tree from the reference camera: always extend along the best-supported edge
    import heapq
    adj = defaultdict(list)
    for e in edges:
        # recorder<->recorder matches are sample-accurate; camera votes carry the camera clock's
        # 1-second rounding, so the tree always prefers recorder links when it has them
        wgt = (1000 if e['exact'] else 0) + e['support'] + e['r']
        adj[e['u']].append((wgt, e['w'], -e['v']))   # delta(w) = delta(u) - v
        adj[e['w']].append((wgt, e['u'], e['v']))    # delta(u) = delta(w) + v
    deltas = {res.reference: 0.0}
    heap = [(-w, deltas[res.reference] + off, n) for w, n, off in adj[res.reference]]
    heapq.heapify(heap)
    while heap:
        _, d, node = heapq.heappop(heap)
        if node in deltas:
            continue
        deltas[node] = d
        for w, n, off in adj[node]:
            if n not in deltas:
                heapq.heappush(heap, (-w, d + off, n))
    for e in edges:   # report inconsistent loops
        if e['u'] in deltas and e['w'] in deltas:
            resid = deltas[e['u']] - deltas[e['w']] - e['v']
            if abs(resid) > 2.0:
                res.warnings.append(f"offsets disagree by {resid:+.1f}s between {_nm(e['u'])} and {_nm(e['w'])} "
                                    f"(usually a camera clock that drifted; clips there are still matched individually)")
    all_nodes = set(fam_clips) | {r.path for r in recs}
    linked = len(deltas)
    silent = {f for f, cl in fam_clips.items() if all(envs.get(c.path) is None for c in cl)}
    # a recording with no match of its own takes the clock correction of the nearest-in-time
    # recording from the same device (same recorder model / serial), e.g. the next take
    by_path = {r.path: r for r in recs}
    same_device = {}
    for n in sorted(all_nodes - set(deltas)):
        rec = by_path.get(n)
        dev = rec.extra.get('device') if rec else ''
        sibs = [r for r in recs if dev and r.path in deltas and r.extra.get('device') == dev]
        if sibs:
            sib = min(sibs, key=lambda r: abs(_secs(r.clock, rec.clock)))
            deltas[n] = deltas[sib.path]
            same_device[n] = sib.path
            res.warnings.append(f'{_nm(n)} shares no matchable sound with the rest: placed by the clock '
                                f'correction of {_nm(sib.path)} (same recorder)')
            continue
        why = 'has no audio' if n in silent else 'shares no matchable sound with the rest'
        res.warnings.append(f'{_nm(n)} {why}: placed by its own clock')
        deltas[n] = 0.0
    res.deltas = deltas
    log(f'  pass 1: {len(edges)} clock relations; {linked} of {len(all_nodes)} cameras/recorders linked by audio')

    # ---------- pass 2: every clip, narrow search ----------
    own = {}                                  # path -> (correction s, r, prom)
    for c in vids:
        env = envs.get(c.path)
        if env is None:
            continue
        s0, seg = _segment(env, p.seg_len)
        if seg is None:
            continue
        cstart = c.clock + dt.timedelta(seconds=deltas[c.family])
        best = None
        for rec in recs:
            rstart = rec.clock + dt.timedelta(seconds=deltas[rec.path])
            if not (rstart - dt.timedelta(seconds=p.search) < cstart + dt.timedelta(seconds=c.duration)
                    and cstart < rstart + dt.timedelta(seconds=rec.duration + p.search)):
                continue
            m = _match(envs[rec.path], rec.clock, c.clock, s0, seg,
                       deltas[c.family] - deltas[rec.path], p.search, p)
            if m and (best is None or m[0] > best[0]):
                r, prom, rec_t = m
                true_start = rstart + dt.timedelta(seconds=rec_t - s0)
                best = (r, prom, _secs(true_start, cstart))
        if best and best[0] >= p.min_r and best[1] >= p.min_prom:
            own[c.path] = (best[2], best[0], best[1])

    for fam, clips in fam_clips.items():
        pts = sorted((c.clock, own[c.path][0]) for c in clips
                     if c.path in own and own[c.path][1] >= p.clip_r)
        for c in clips:
            near = [x for t, x in pts if abs(_secs(t, c.clock)) <= p.curve_window] or [x for _, x in pts]
            curve = float(np.median(near)) if near else 0.0
            base = c.clock + dt.timedelta(seconds=deltas[fam])
            o = own.get(c.path)
            if o and ((o[1] >= p.clip_r and abs(o[0] - curve) <= p.clip_agree) or (o[1] >= p.strong_r and o[2] >= 0.25)):
                res.starts[c.path], res.how[c.path] = base + dt.timedelta(seconds=o[0]), 'audio'
            else:
                res.starts[c.path] = base + dt.timedelta(seconds=curve)
                res.how[c.path] = 'camera-curve' if pts or fam in {e['u'] for e in edges} | {e['w'] for e in edges} else 'clock'
        # a camera cannot overlap itself: nudge tiny overlaps
        prev_end = None
        for c in sorted(clips, key=lambda c: res.starts[c.path]):
            s = res.starts[c.path]
            if prev_end and s < prev_end and _secs(prev_end, s) < p.nudge:
                res.starts[c.path] = prev_end + dt.timedelta(milliseconds=40)
                res.how[c.path] += '+nudged'
            e = res.starts[c.path] + dt.timedelta(seconds=c.duration)
            prev_end = max(prev_end, e) if prev_end else e
    for rec in [m for m in media if m.kind == 'audio']:
        res.starts[rec.path] = rec.clock + dt.timedelta(seconds=deltas.get(rec.path, 0.0))
        res.how[rec.path] = ('recorder' if rec.path in {e['u'] for e in edges} | {e['w'] for e in edges}
                             else 'same-device' if rec.path in same_device else 'clock')
    return res
