"""Audio fingerprints (speech-band loudness envelopes) and fast normalized cross-correlation."""
from __future__ import annotations

import hashlib
import os
import subprocess

import numpy as np

SR = 8000          # decode rate
HOP = 40           # samples per envelope frame -> 200 frames per second
RATE = SR / HOP    # envelope frames per second
CACHE = os.path.expanduser(os.environ.get('RMS_CACHE', '~/.cache/resolve-multicam-sync'))


def _key(path: str) -> str:
    st = os.stat(path)
    return hashlib.sha1(f'{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}|v1'.encode()).hexdigest()


def envelope(path: str) -> np.ndarray | None:
    """Log-energy of the speech band (250-3500 Hz) at 200 frames/s. Wind rumble and hiss are filtered out,
    which is what lets quiet, noisy camera scratch audio still line up with a clean recorder. Cached."""
    os.makedirs(CACHE, exist_ok=True)
    cp = os.path.join(CACHE, _key(path) + '.npy')
    if os.path.exists(cp):
        e = np.load(cp)
        return e if e.size else None
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-map', '0:a:0', '-ac', '1', '-ar', str(SR),
                          '-af', 'highpass=f=250,lowpass=f=3500', '-f', 's16le', '-'],
                         capture_output=True).stdout
    y = np.frombuffer(raw, np.int16).astype(np.float32) / 32768
    n = len(y) // HOP
    e = np.log(np.square(y[:n * HOP]).reshape(n, HOP).mean(1) + 1e-8).astype(np.float32) if n else np.zeros(0, np.float32)
    np.save(cp, e)
    return e if e.size else None


def zn(x: np.ndarray) -> np.ndarray:
    return (x - x.mean()) / (x.std() + 1e-6)


def xcorr(long: np.ndarray, seg: np.ndarray) -> np.ndarray:
    """Normalized correlation of `seg` at every offset inside `long` (Pearson r, -1..1). FFT based."""
    n, L = len(seg), len(long)
    if n < 2 or L < n:
        return np.zeros(0)
    s = zn(seg)
    N = 1 << int(np.ceil(np.log2(L + n)))
    conv = np.fft.irfft(np.fft.rfft(long, N) * np.fft.rfft(s[::-1], N), N)[n - 1:L]
    cs = np.concatenate([[0.0], np.cumsum(long, dtype=np.float64)])
    cs2 = np.concatenate([[0.0], np.cumsum(long.astype(np.float64) ** 2)])
    m = (cs[n:] - cs[:-n]) / n
    sd = np.sqrt(np.maximum((cs2[n:] - cs2[:-n]) / n - m * m, 1e-9))
    return conv / n / sd


def peak(c: np.ndarray, guard_s: float = 1.0) -> tuple[int, float, float]:
    """(index, r, prominence): prominence = best r minus the best r found more than `guard_s` away."""
    if c.size == 0:
        return -1, 0.0, 0.0
    k = int(np.argmax(c))
    g = int(guard_s * RATE)
    rest = np.concatenate([c[:max(0, k - g)], c[k + g + 1:]])
    second = float(rest.max()) if rest.size else 0.0
    return k, float(c[k]), float(c[k]) - second
