"""Synthetic end-to-end check of the solver: a fake 'day' of sound, two cameras and two recorders with
badly wrong clocks, noisy scratch audio and a silent clip. Run: python3 -m tests.test_solve"""
import datetime as dt
import sys

import numpy as np

sys.path.insert(0, '.')
from resolve_multicam_sync import audio, solve
from resolve_multicam_sync.media import Media

R = audio.RATE
rng = np.random.default_rng(7)
DAY = 3 * 3600                                       # three hours of "real" sound
truth = np.convolve(rng.normal(0, 1, int(DAY * R)), np.ones(9) / 3, 'same').astype(np.float32)
T0 = dt.datetime(2030, 6, 1, 14, 0, 0)


def take(start, dur, noise):
    e = truth[int(start * R):int((start + dur) * R)].copy()
    return e + rng.normal(0, noise, e.size).astype(np.float32)


def run():
    media, envs, real = [], {}, {}
    clock_err = {'CAM_A#': 0.0, 'CAM_B#': 3605.3, 'rec1.wav': -71.2, 'rec2.wav': -1698.9}
    # two recorders covering most of the day
    for name, s, d in [('rec1.wav', 600, 4800), ('rec2.wav', 5000, 5400)]:
        m = Media(name, 'audio', d, T0 + dt.timedelta(seconds=s - clock_err[name]), 'test', True, name)
        media.append(m); envs[name] = take(s, d, 0.05); real[name] = s
    # two cameras, many short clips, one silent clip
    k = 0
    for fam, noise in [('CAM_A#', 0.6), ('CAM_B#', 0.9)]:
        s = 700.0 + (13 if fam == 'CAM_B#' else 0)
        while s < 10200:
            d = float(rng.uniform(4, 40)); k += 1
            name = f'{fam.replace("#", "")}{k:04d}.MP4'
            clock = T0 + dt.timedelta(seconds=round(s - clock_err[fam]))      # 1-second clock resolution
            m = Media(name, 'video', d, clock, 'test', True, fam)
            media.append(m); real[name] = s
            envs[name] = None if k % 17 == 0 else take(s, d, noise)          # some clips have no usable audio
            s += d + float(rng.uniform(5, 120))
    res = solve.solve(media, envs, solve.Params(max_skew=4000), log=lambda *_: None)
    # only relative timing matters (the whole day can sit anywhere), so remove the common shift
    diff = np.array([(res.starts[m.path] - T0).total_seconds() - real[m.path] for m in media])
    errs = np.abs(diff - np.median(diff))
    how = {m.path: res.how[m.path] for m in media}
    worst = sorted(zip(errs, [m.path for m in media]), reverse=True)[:5]
    print('worst:', [(n, round(float(e), 3), how[n]) for e, n in worst])
    audio_n = sum(v.startswith('audio') for v in how.values())
    print(f'{len(media)} files | audio-locked {audio_n} | median error {np.median(errs):.3f}s | max error {errs.max():.3f}s')
    print('warnings:', res.warnings)
    assert np.median(errs) < 0.05, 'median sync error too large'
    assert errs.max() < 1.2, 'some clip is badly misplaced'
    print('OK')


if __name__ == '__main__':
    run()
