"""Find media in folders and read what each file says about itself (length, recording clock, audio)."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
from dataclasses import dataclass, field

VIDEO_EXT = {'.mp4', '.mov', '.mxf', '.m4v', '.mts', '.avi', '.mkv'}
AUDIO_EXT = {'.wav', '.mp3', '.m4a', '.aac', '.flac', '.aif', '.aiff'}
# recorders whose broadcast-WAV origination time is when the file was closed, not when it started
END_STAMPED = ('FR-AV2',)


@dataclass
class Media:
    path: str
    kind: str                      # 'video' or 'audio'
    duration: float                # seconds
    clock: dt.datetime             # local-time start according to the file's own clock
    clock_source: str              # which metadata the clock came from
    has_audio: bool
    family: str                    # camera grouping key (video) / own name (audio)
    extra: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


def scan(folders: list[str], recursive: bool = True) -> list[str]:
    """All video/audio files under the folders (skips hidden and macOS ._ files)."""
    out = []
    for folder in folders:
        folder = os.path.expanduser(folder)
        walker = os.walk(folder) if recursive else [(folder, [], os.listdir(folder))]
        for root, dirs, files in walker:
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for f in files:
                if f.startswith('.'):
                    continue
                ext = os.path.splitext(f)[1].lower()
                if ext in VIDEO_EXT or ext in AUDIO_EXT:
                    out.append(os.path.join(root, f))
    return sorted(set(out))


def family_of(path: str) -> str:
    """Group camera files by their name pattern with digits masked: A001_C012 -> 'A#_C#', C0461 -> 'C#'.

    Cameras name files with a fixed prefix plus a counter, so this separates cameras reliably
    without needing camera metadata."""
    stem = os.path.splitext(os.path.basename(path))[0]
    return re.sub(r'\d+', '#', stem)


def _local_tz():
    return dt.datetime.now().astimezone().tzinfo


def _rate(s: str) -> float:
    try:
        a, b = s.split('/')
        return float(a) / float(b) if float(b) else 0.0
    except (ValueError, AttributeError):
        return 0.0


def _tc_seconds(tc: str, fps: float) -> float | None:
    """'HH:MM:SS:FF' (or ;FF) -> seconds, counting frames at the nominal rate the way editors do."""
    m = re.match(r'^(\d+):(\d+):(\d+)[:;.](\d+)$', tc or '')
    if not m or fps <= 0:
        return None
    h, mi, se, fr = map(int, m.groups())
    nominal = round(fps)
    frames = ((h * 60 + mi) * 60 + se) * nominal + fr
    return frames / nominal * (nominal / fps)


def probe(path: str) -> Media:
    j = json.loads(subprocess.check_output(
        ['ffprobe', '-v', 'error', '-show_entries',
         'format=duration:format_tags:stream=codec_type,r_frame_rate,width,height,sample_rate,channels:stream_tags=timecode',
         '-of', 'json', path]))
    fmt = j.get('format', {})
    tags = {k.lower(): v for k, v in (fmt.get('tags') or {}).items()}
    duration = float(fmt.get('duration') or 0)
    sts = j.get('streams', [])
    streams = [s.get('codec_type') for s in sts]
    v = next((s for s in sts if s.get('codec_type') == 'video'), {})
    a = next((s for s in sts if s.get('codec_type') == 'audio'), {})
    fps = _rate(v.get('r_frame_rate', ''))
    tc = next((s.get('tags', {}).get('timecode') for s in sts if s.get('tags', {}).get('timecode')), None) or tags.get('timecode')
    sr = int(a.get('sample_rate') or 0)
    tc_start = _tc_seconds(tc, fps) if tc else None
    if tc_start is None and tags.get('time_reference') and sr:          # broadcast WAV: samples since midnight
        tc_start = int(tags['time_reference']) / sr
    extra = dict(fps=fps, width=int(v.get('width') or 0), height=int(v.get('height') or 0),
                 sample_rate=sr, channels=int(a.get('channels') or 0), tc_start=tc_start or 0.0)
    sn = re.search(r'SN:\s*(\S+)', tags.get('comment', ''))
    extra['device'] = ' '.join(x for x in (tags.get('encoded_by', '').strip(), sn.group(1) if sn else '') if x)
    ext = os.path.splitext(path)[1].lower()
    kind = 'video' if ext in VIDEO_EXT and 'video' in streams else 'audio'
    clock, source = None, ''
    ct = tags.get('creation_time', '')
    if 'T' in ct:                                       # cameras: ISO time in UTC
        try:
            t = dt.datetime.fromisoformat(ct.replace('Z', '+00:00'))
            clock = t.astimezone(_local_tz()).replace(tzinfo=None) if t.tzinfo else t
            source = 'creation_time'
        except ValueError:
            pass
    elif ct and tags.get('date'):                      # broadcast WAV: local date + time of day
        try:
            clock = dt.datetime.fromisoformat(f"{tags['date']}T{ct}")
            source = 'bwf_origination'
            if any(d in tags.get('encoded_by', '') for d in END_STAMPED):
                clock -= dt.timedelta(seconds=duration)
                source = 'bwf_origination (end of file)'
        except ValueError:
            pass
    if clock is None:                                   # fall back to the file's write time minus its length
        mtime = dt.datetime.fromtimestamp(os.path.getmtime(path))
        clock = mtime - dt.timedelta(seconds=duration)
        source = 'mtime'
    return Media(path=path, kind=kind, duration=duration, clock=clock, clock_source=source,
                 has_audio='audio' in streams,
                 family=family_of(path) if kind == 'video' else os.path.basename(path), extra=extra)


def probe_all(paths: list[str], workers: int = 8) -> list[Media]:
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(workers) as ex:
        return [m for m in ex.map(_safe_probe, paths) if m and m.duration > 0]


def _safe_probe(path):
    try:
        return probe(path)
    except Exception as e:  # unreadable file: report and skip
        print(f'  ! skipping {os.path.basename(path)}: {e}')
        return None
