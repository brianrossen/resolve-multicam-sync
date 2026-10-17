# resolve-multicam-sync

**Point it at a folder of footage. Get back a DaVinci Resolve timeline with every camera synced by audio, each on its own track.**

Weddings, events, interviews, concerts, documentaries: any shoot with several cameras and separate audio recorders, no timecode required. It does what PluralEyes does, at the scale of a full shoot day, and it builds the timeline for you.

See it on a real wedding day, and how to set it up with Claude Code: **[AI multicam sync for DaVinci Resolve](https://brianrossen.com/blog/multicam-sync/)**.

```
python3 -m resolve_multicam_sync "/path/to/Shoot Day"
```

## What it does

1. **Finds everything** in the folder (and subfolders): camera clips and audio recordings (lavs, recorders, board feeds, anything).
2. **Groups clips by camera** from their file names (`A001.MP4`, `C0461.MP4`, `DJI_0021.MP4` … each pattern is one camera).
3. **Uses timecode when you have it.** If a sync box (Tentacle Sync, Deity and similar) feeds timecode into a camera's audio input, the tool reads that LTC tone off the camera audio and lines it up with the recorders' broadcast-WAV timecode — frame-accurate, no audio matching needed. Cameras often keep their own free-running timecode in the file metadata even with a sync box attached, so that metadata is ignored; the tone on the audio is what counts. Clips without timecode (b-roll shot after the box was unplugged) follow their camera's clock, corrected by the timecoded clips. A few clips are spot-checked against the recorder audio. The timecode channel is never placed on the timeline. `--no-timecode` forces audio sync.
4. **Otherwise works out every clock error by audio.** It doesn't matter if a camera's clock is an hour off, a recorder is 28 minutes off and a lav is 71 seconds off. It matches sound between files, votes on each device's offset, and links them all together. Recorder-to-recorder matches are sample-accurate and are preferred.
5. **Places every clip.** Clips whose own scratch audio matches a recorder are locked by that match (typically within a frame or two). Clips with unusable or no audio (wind, music, drones) follow their camera's corrected clock. No camera is ever allowed to overlap itself, and bad matches are rejected automatically.
6. **Builds a new timeline** in the open Resolve project: one video track per camera, recorders packed onto audio tracks, clips color-coded, optional markers. Existing timelines are never touched.
7. **Closes the gaps (default):** stretches where no camera is rolling are removed and audio is trimmed to match, so a 10-hour day becomes only the moments you have picture for. Use `--keep-gaps` to keep real-time spacing instead.
8. **Optional camera scratch audio** (`--camera-audio`): leave it off (default), keep it for every camera, keep it only where no recorder was running (`auto`), or keep it for specific cameras or clips.
9. **Or export FCPXML** (`--fcpxml`): the same synced timeline as a file for **free DaVinci Resolve, Final Cut Pro or Premiere Pro** — no Resolve Studio or scripting setup needed.

Typical result on a real wedding day: ~800 clips from 6 cameras and 16 recordings, synced and laid out in minutes.

## Requirements

- To build straight into Resolve: **DaVinci Resolve Studio** (external scripting is a Studio feature), open, with a project loaded,
  and **Preferences → System → General → External scripting using: Local**.
- Using `--fcpxml` instead? Any editor that imports FCPXML works (Resolve free or Studio, Final Cut Pro, Premiere Pro via XML).
- **Python 3.9+** (numpy and Pillow go into the repo's own `.venv`, see below)
- **ffmpeg / ffprobe** on the PATH

Easiest setup: let Claude Code do it (see below). Manual setup on a Mac:

```
brew install python ffmpeg
git clone https://github.com/brianrossen/resolve-multicam-sync.git && cd resolve-multicam-sync
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m tests.test_solve          # should end with OK
```

The `.venv` folder keeps the tool's Python packages inside the repo, so it never touches your system Python
(Homebrew's Python refuses a plain `pip install`). In each new Terminal window, run `source .venv/bin/activate`
from the repo folder first; then the `python3 -m ...` commands below use it. On Windows the paths are
`.venv\Scripts\python` and `.venv\Scripts\activate`.

## Usage

```
python3 -m resolve_multicam_sync FOLDER [FOLDER ...] [options]

  --timeline NAME     new timeline name (default: folder name)
  --bin NAME          media pool bin to import into (default: timeline name)
  --keep-gaps         keep real-time gaps (by default, time with no camera rolling is removed)
  --camera-audio MODE none (default) | all | auto (only where no recorder was running) | camera labels, e.g. "C,B"
                      keep single clips' scratch audio with "camera_audio_clips" in --config
  --fcpxml FILE       write FCPXML instead of building in Resolve (import: Resolve File > Import > Timeline,
                      Final Cut File > Import > XML)
  --fps 23.976        FCPXML timeline frame rate (default 23.976)
  --resolution WxH    FCPXML timeline size (default: most common camera size)
  --config FILE.json  camera colors, time-of-day colors + markers, per-file colors
  --max-skew 2h       how wrong any clock may be (raise it if a camera is off by more than 2 hours)
  --no-timecode       ignore timecode and sync by audio only
  --skip TEXT         skip files whose name contains TEXT (repeatable), e.g. duplicate audio exports
  --dry-run           analyze and print the plan without touching Resolve
  --report out.json   save every file's synced start time and how it was placed
```

Examples:

```
python3 -m resolve_multicam_sync "~/Footage/Smith Wedding/Rehearsal Dinner"
python3 -m resolve_multicam_sync "/Volumes/SSD/Concert" --timeline "Concert - All Angles" --keep-gaps
python3 -m resolve_multicam_sync "~/Footage/Day 2" --config examples/colors.example.json
python3 -m resolve_multicam_sync "~/Footage/Interview" --camera-audio auto --fcpxml ~/Desktop/interview.fcpxml
```

**Personal defaults:** put the options you always want in `~/.resolve-multicam-sync.json` and they apply to every run
(command-line flags still win):

```json
{ "camera_audio": "auto", "close_gaps": true, "max_skew": "4h", "skip": ["_backup"],
  "colors": { "camera_colors_always": { "DJI": "Lime" } } }
```

`colors` holds color rules you always want; a job's own `--config` is layered on top of them.

Audio fingerprints are cached (`~/.cache/resolve-multicam-sync`), so re-runs are fast.

## Colors and markers

Without a config, each camera gets its own color. With `--config` you can color by **time of day** (getting ready, ceremony, dinner …), by camera, or per file, and drop a named marker at the start of each section. See [`examples/colors.example.json`](examples/colors.example.json).

**Automatic parts of the day:** add `"auto_day_parts": true` to a config (or to `colors` in your defaults file) and the
tool works out the schedule from your audio recordings' names and synced times: files named like *vows*, *first look* /
*FOB*, *ceremony* / *officiant*, *welcome* / *speeches* / *toasts*, *DJ*, *last dance* / *exit* mark those parts, and the time between them becomes
getting ready, portraits or dancing. Each part gets a color and a marker (getting ready Pink, first looks + ceremony Orange,
portraits Yellow, dinner + toasts Green, dancing Brown). Change them with
`"auto_day_parts": {"colors": {"dancing": "Chocolate"}, "keywords": {"ceremony": ["church"]}, "second_camera": {"dancing": "Tan"}}`
(`second_camera` colors every camera except the main one in that part; the main one is the camera with the most clips unless you set `"main_camera": "A"`). Time windows you write yourself still win.

Priority: per-file color > all-day camera color (`camera_colors_always`, e.g. the drone stays Lime all day) > time-of-day window > camera color.
A camera key matches its label (the file name without the numbers, as shown in the summary), so `"DJI"` means `DJI_0001.MP4` and not `DJI_20260101_0001_D.MP4`. (FCPXML has no clip colors, so colors apply when building straight into Resolve.)

## Color by what's in the shot (visual review)

The sync can't see your footage, but Claude Code can. After a sync run with `--report`:

```
python3 -m resolve_multicam_sync.review sheets report.json ~/Desktop/review      # contact sheets of every clip, in order
# look at the sheets (or ask Claude to) and write colors.txt:  "0-5 bride", "41 groom", "42-60 details", "A0123.MP4 Yellow"
python3 -m resolve_multicam_sync.review apply ~/Desktop/review/index.json colors.txt --timeline "Day 2"
```

Categories map to colors through `"content_colors"` in your defaults file:

```json
{ "content_colors": { "categories": { "bride": "Pink", "groom": "Navy", "bride_girls": "Purple", "details": "Teal",
                                      "venue": "Beige", "portraits": "Yellow", "dinner": "Green", "dancing": "Brown" },
                      "second_camera": { "dancing": "Tan" }, "main_camera": "A" } }
```

It only changes clip colors on that timeline; nothing moves. Contact sheets use Pillow (installed by `requirements.txt`).

## Use it with Claude Code

Give Claude Code this repo's link and say something like:

> Set up resolve-multicam-sync from <link> and sync everything in "/path/to/Shoot Day" into Resolve.

Claude reads [`CLAUDE.md`](CLAUDE.md), installs what's missing, runs a dry run, shows you the sync summary, then builds the timeline.

## Updating

Your copy only changes when you update it:

```
cd resolve-multicam-sync
git pull
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m tests.test_solve
```

Or tell Claude Code "update resolve-multicam-sync". Your `~/.resolve-multicam-sync.json` defaults live outside the
repo, so updates never overwrite them. Changes are listed in [CHANGELOG.md](CHANGELOG.md).

## How accurate is it?

`python3 -m tests.test_solve` simulates a 3-hour day with two cameras (one clock an hour off), two recorders with wrong clocks, noisy scratch audio and silent clips. Clips locked by audio land within ~2 ms of the truth; clips with no usable audio land within the camera clock's 1-second resolution after correction.

Always spot-check a moment with several angles (a clap, a kiss, a toast). If a camera never matched any recorder (no shared sound at all), the summary says so and that camera stays on its own clock.

## Limits

- Needs shared sound: at least one recorder or camera has to hear what the others hear.
- Clips under ~4 s, or with no audio track, are placed by their camera's corrected clock, not matched individually.
- Two camera bodies that use the same file-name pattern are treated as one camera; if they overlap, the extra clips go on a second track for that camera.
- Doesn't change existing timelines, grades or clips; it only adds a new timeline (and imports media into a bin).
- In an FCPXML import the first video track is an empty placeholder (the storyline); cameras start on track 2.

## License

MIT. See [LICENSE](LICENSE).
