"""Sync by timecode when it is there: LTC recorded as audio on a camera channel (Tentacle Sync and similar
boxes feed the camera's mic input) plus the recorders' broadcast-WAV timecode.

Many cameras keep their own free-running timecode in the file metadata even when a sync box is plugged in,
so that metadata timecode is not trusted. The LTC on the audio track is.

    detect(media)  -> reads LTC from each camera clip (any channel), stores it in m.extra
    place(media)   -> start time for every file, or None when there is not enough timecode to rely on
"""
from __future__ import annotations

import datetime as dt
import statistics
import subprocess
from concurrent.futures import ThreadPoolExecutor

import numpy as np

SR = 48000
SYNC = np.array([0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 1], np.int8)   # LTC sync word, bits 64-79


def _read(path: str, start: float, dur: float, channel: int) -> np.ndarray:
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-ss', f'{start:.3f}', '-t', f'{dur:.3f}', '-i', path,
                          '-af', f'pan=mono|c0=c{channel}', '-f', 's16le', '-ar', str(SR), '-'],
                         capture_output=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32)


def decode(x: np.ndarray) -> tuple[list[tuple[float, int, int, int, int]], float]:
    """LTC frames in a mono signal -> ([(seconds into x, h, m, s, f), ...], bits per second)."""
    if len(x) < SR // 4:
        return [], 0.0
    x = x - np.median(x)
    th = 0.2 * np.percentile(np.abs(x), 95)
    if th <= 0:
        return [], 0.0
    st = np.where(x > th, 1, np.where(x < -th, -1, 0))
    nz = np.nonzero(st)[0]
    if len(nz) < 100:
        return [], 0.0
    sg = st[nz]
    edges = nz[1:][sg[1:] != sg[:-1]]                 # sign changes (biphase-mark transitions)
    iv = np.diff(edges)
    if len(iv) < 200:
        return [], 0.0
    full = float(np.median(np.sort(iv)[len(iv) // 2:]))   # one bit cell
    bits, pos, i = [], [], 0
    while i < len(iv):
        if iv[i] > 0.75 * full:
            bits.append(0); pos.append(edges[i]); i += 1
        elif i + 1 < len(iv) and iv[i + 1] < 0.75 * full:
            bits.append(1); pos.append(edges[i]); i += 2    # a 1 is two half-cells
        else:
            i += 1
    b = np.array(bits, np.int8)
    out = []
    for k in range(64, len(b) - 16):
        if b[k] == 0 and b[k + 1] == 0 and np.array_equal(b[k:k + 16], SYNC):
            w = b[k - 64:k]
            v = lambda a, n: int(sum(int(w[a + j]) << j for j in range(n)))
            fr, se, mi, hr = v(0, 4) + 10 * v(8, 2), v(16, 4) + 10 * v(24, 3), v(32, 4) + 10 * v(40, 3), v(48, 4) + 10 * v(56, 2)
            if hr < 24 and mi < 60 and se < 60 and fr < 31:
                out.append((pos[k - 64] / SR, hr, mi, se, fr))
    return out, SR / full


def clip_ltc(path: str, duration: float, channels: int, video_fps: float) -> dict | None:
    """Find the channel carrying LTC and the clip's start in real seconds since midnight."""
    probe_at = min(1.0, max(0.0, duration / 3))
    for ch in range(max(channels, 1)):
        frames, bitrate = decode(_read(path, probe_at, 2.5, ch))
        if len(frames) < 3:
            continue
        nominal = round(bitrate / 80)
        if nominal not in (24, 25, 30):
            continue
        labels = [((h * 60 + m) * 60 + s) + f / nominal for _, h, m, s, f in frames]
        if max(np.diff(labels)) > 2.0 / nominal:        # frames must count up one by one
            continue
        # 23.976 / 29.97 non-drop: the labels run 0.1 % slow against the clock. The frame rate cannot be told
        # from the bit rate (1918 vs 1920 b/s), so measure the drift across the clip when it is long enough;
        # otherwise assume a camera shooting an NTSC rate (23.976/29.97/59.94) is fed NTSC timecode.
        ntsc_video = bool(video_fps) and abs(video_fps * 1.001 - round(video_fps * 1.001)) < 0.01 and abs(video_fps - round(video_fps)) > 0.01
        pull = 1.001 if ntsc_video and nominal in (24, 30) else 1.0
        t0, h, m, s, f = frames[0]
        if duration > 45:
            late = duration - 6
            frames2, _ = decode(_read(path, late, 2.5, ch))
            if len(frames2) >= 3:
                t1, h2, m2, s2, f2 = frames2[0]
                lab1 = ((h * 60 + m) * 60 + s) + f / nominal
                lab2 = ((h2 * 60 + m2) * 60 + s2) + f2 / nominal
                real = (late + t1) - (probe_at + t0)
                if real > 30 and lab2 > lab1:
                    ratio = real / (lab2 - lab1)
                    pull = 1.001 if abs(ratio - 1.001) < abs(ratio - 1.0) else 1.0
        label = ((h * 60 + m) * 60 + s) + f / nominal
        # `label` is read `into` seconds into the clip. Recorders disagree on whether their broadcast-WAV timecode is
        # in timecode-label seconds or wall-clock seconds, so keep both and let place() pick with the recorder audio.
        return dict(channel=ch, label=label, into=probe_at + t0, rate=nominal, ntsc=pull > 1.0)
    return None


def detect(media, workers: int = 4, log=print) -> int:
    vids = [m for m in media if m.kind == 'video' and m.has_audio]
    def one(m):
        return m, clip_ltc(m.path, m.duration, m.extra.get('channels') or 2, m.extra.get('fps') or 0)
    found = 0
    with ThreadPoolExecutor(workers) as ex:
        for m, r in ex.map(one, vids):
            if r:
                m.extra['ltc'] = r
                found += 1
    if found:
        log(f'  timecode on the camera audio: {found} of {len(vids)} clips (channel {statistics.mode(m.extra["ltc"]["channel"] + 1 for m in vids if "ltc" in m.extra)})')
    return found


def _on_day(clock: dt.datetime, secs: float) -> dt.datetime:
    """Seconds-since-midnight on the day closest to the file's own (possibly wrong) clock."""
    base = dt.datetime.combine(clock.date(), dt.time()) + dt.timedelta(seconds=secs)
    return min((base + dt.timedelta(days=d) for d in (-1, 0, 1)), key=lambda t: abs((t - clock).total_seconds()))


def ltc_start(m, mult: float) -> float:
    l = m.extra['ltc']
    return l['label'] * mult - l['into']


def place(media, log=print, mult: float = 1.0):
    """Start times from timecode, or None if the shoot does not have usable timecode.

    Clips without LTC follow their camera's clock, corrected by the median offset of that camera's
    timecoded clips (or of all timecoded clips when a camera has none)."""
    ltc = [m for m in media if 'ltc' in m.extra]
    recs = [m for m in media if m.kind == 'audio' and m.extra.get('tc_start')]
    if not ltc or not recs:
        return None
    starts, how = {}, {}
    for m in recs:
        starts[m.path], how[m.path] = _on_day(m.clock, m.extra['tc_start']), 'timecode (recorder)'
    for m in ltc:
        starts[m.path], how[m.path] = _on_day(m.clock, ltc_start(m, mult)), 'timecode'
    # sanity: timecoded clips should land inside the recorders' timecode span
    spans = [(starts[r.path], starts[r.path] + dt.timedelta(seconds=r.duration)) for r in recs]
    inside = sum(any(a - dt.timedelta(seconds=60) <= starts[m.path] <= b for a, b in spans) for m in ltc)
    if inside < max(1, len(ltc) // 2):
        log(f'  ! camera timecode and recorder timecode do not overlap ({inside}/{len(ltc)} clips)')
        return None
    offs = {}
    for m in ltc:
        offs.setdefault(m.family, []).append((starts[m.path] - m.clock).total_seconds())
    allo = [o for v in offs.values() for o in v]
    for m in media:
        if m.path in starts:
            continue
        o = offs.get(m.family) if m.kind == 'video' else None
        shift = statistics.median(o or allo)
        starts[m.path] = m.clock + dt.timedelta(seconds=shift)
        how[m.path] = 'clock (timecode-corrected)' if o else 'clock (corrected by other cameras)'
    return starts, how


def verify(media, starts, envelope, n: int = 8) -> list[float]:
    """Spot-check timecode placements against the recorder audio (camera channel without LTC, every recorder that
    covers the clip, +-90 s). Returns how far each matched clip would move by audio; ~0 means the timecode is right.
    Outdoor scratch audio correlates weakly, so a modest r is accepted and the caller looks for agreement."""
    recs = [m for m in media if m.kind == 'audio']
    picks = sorted((m for m in media if 'ltc' in m.extra and m.duration > 20), key=lambda m: -m.duration)[:n]
    out = []
    for m in picks:
        a0 = starts[m.path]
        cover = [r for r in recs if starts[r.path] <= a0 and a0 + dt.timedelta(seconds=m.duration) <= starts[r.path] + dt.timedelta(seconds=r.duration)]
        if not cover:
            continue
        ch = 1 - m.extra['ltc']['channel'] if (m.extra.get('channels') or 2) == 2 else None
        seg, cs = min(60.0, m.duration - 4), 2.0
        a = envelope(m.path, cs, seg, ch)
        if len(a) < 400:
            continue
        best = (0.0, 0.0)
        for rec in cover:
            off = (a0 - starts[rec.path]).total_seconds()
            s0 = max(0.0, off + cs - 90)
            b = envelope(rec.path, s0, seg + 180, None)
            if len(b) <= len(a):
                continue
            expect = (off + cs - s0) * 200
            for k in range(0, len(b) - len(a), 2):
                c = float(np.corrcoef(a, b[k:k + len(a)])[0, 1])
                if c > best[0]:
                    best = (c, (k - expect) / 200)
        if best[0] > 0.3:
            out.append(best[1])
    return out


def envelope(path: str, start: float, dur: float, channel: int | None) -> np.ndarray:
    """200/s speech-band log envelope of part of a file (one channel or the mix)."""
    af = 'highpass=f=250,lowpass=f=3500' + (f',pan=mono|c0=c{channel}' if channel is not None else '')
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-ss', f'{max(0.0, start):.3f}', '-t', f'{dur:.3f}', '-i', path,
                          '-af', af, '-ac', '1', '-ar', '8000', '-f', 'f32le', '-'], capture_output=True).stdout
    x = np.frombuffer(raw, np.float32)
    n = len(x) // 40
    if n == 0:
        return np.zeros(0, np.float32)
    e = np.log(np.sqrt((x[:n * 40].reshape(n, 40) ** 2).mean(1)) + 1e-5)
    return (e - e.mean()) / (e.std() + 1e-9)
