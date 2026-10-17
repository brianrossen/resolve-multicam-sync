# Instructions for Claude

This repo syncs multi-camera footage by audio and builds a DaVinci Resolve timeline. When a user asks you to
"sync", "line up", "put all my clips on a timeline", or similar for a folder of footage, use it.

## Set up (once)

1. Check `python3 --version` (3.9+), `ffmpeg -version`, `ffprobe -version`. Install anything missing
   (macOS: `brew install python ffmpeg`; Windows: `winget install Python.Python.3.12 Gyan.FFmpeg`; Linux: package manager).
2. From the repo root, make the repo's own Python environment (never `pip install` into the system or Homebrew
   Python; Homebrew refuses it): `python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt`
   (Windows: `.venv\Scripts\python`). If `.venv` already exists, just reinstall requirements.
3. Run `.venv/bin/python -m tests.test_solve`; it must print `OK`.
   Every command below runs with `.venv/bin/python` (each shell call is fresh, so don't rely on `activate`).
4. To build straight into Resolve, it must be **DaVinci Resolve Studio**, open, with a project loaded, and
   **Preferences → System → General → External scripting using** set to **Local**. You cannot change that
   setting for the user — ask them to, and tell them to restart Resolve after changing it.
   If they use free Resolve, Final Cut or Premiere, use `--fcpxml <file>` instead and tell them how to import it.

## Run

1. Always start with a dry run so the user sees the sync summary before anything changes:
   `.venv/bin/python -m resolve_multicam_sync "<folder>" --dry-run --report /tmp/rms_report.json`
   Gaps are closed by default; add `--keep-gaps` if the user wants real-time spacing. Add `--timeline "<name>"`, `--skip "<text>"`, `--camera-audio <mode>` as the user asked
   ("keep the camera audio" = all; "only where there's no mic" = auto; "keep the B camera's audio" = that camera's label).
2. If the shoot had timecode (a sync box feeding the camera audio), the run says "synced by timecode" with an audio
   spot-check in ms. Report that; anything over ~200 ms is a warning worth checking. If the user expected timecode
   but the run synced by audio, the camera probably wasn't recording the LTC tone (check one clip's channels).
   Read the summary back in plain language: cameras found, how far each clock was off, how many clips locked
   by audio, any warnings (e.g. a camera with no audio link). Point out duplicates (two audio files with
   identical content) and suggest `--skip` for them.
3. Then run the same command without `--dry-run` to build the timeline. It creates a **new** timeline and
   never modifies existing ones.
4. Tell the user to spot-check one moment with several angles (a clap, a kiss, a toast).

## Update

When the user asks to update the tool: note `git rev-parse HEAD`, run `git pull`, reinstall requirements into
`.venv`, run the test, then summarize what changed from `CHANGELOG.md` (or `git log <old>..HEAD --oneline`),
especially anything that changes how they run it or what a config key means. Never edit
`~/.resolve-multicam-sync.json` during an update unless a changelog entry says a key was renamed; then offer to.

## Personal defaults

If the user says they always want something ("always keep camera audio where there's no mic"), write it to
`~/.resolve-multicam-sync.json` (keys: camera_audio, close_gaps, config, skip, max_skew, fps, resolution, colors) instead of
asking every time. The run prints which defaults it used.

## Colors

If the user wants clips colored by part of the day, set `"auto_day_parts"` (in `colors` of their defaults file, so it
applies to every run); the dry run prints the derived schedule under "Parts of the day" — read it back to the user.
If their recordings aren't named by event, or they want different windows, write a config like
`examples/colors.example.json` with explicit time windows and pass `--config`.
Time windows are clock times of the shoot day (local time). Recorder names and their synced start times
in the dry-run summary usually outline the schedule (first look, ceremony, dinner, reception).
Colors the user always wants (e.g. drone Lime all day) go in `"colors"` in the defaults file, not each job's config.

## Color by content (visual review)

If the user wants clips colored by what's in them ("bride pink, groom navy, details teal ...") — or their defaults
file has `"content_colors"` — do the visual pass after building:
1. Sync with `--report /tmp/rms_report.json`, then `.venv/bin/python -m resolve_multicam_sync.review sheets /tmp/rms_report.json <dir>`.
2. Read every `sheet_NN.jpg` (48 clips each, numbered in shooting order). Write `<dir>/colors.txt`, one rule per line:
   `0-5 bride`, `41 groom`, `42-60 details` — categories from `content_colors.categories` (or Resolve color names).
   Judge by content, not clock time; use the time only to break ties (e.g. dinner vs dancing).
3. `.venv/bin/python -m resolve_multicam_sync.review apply <dir>/index.json <dir>/colors.txt --timeline "<name>" --dry-run`,
   check the counts, then run it without `--dry-run`. If the user's categories are unknown, ask for them once and save
   them to `content_colors` in `~/.resolve-multicam-sync.json`.

## Troubleshooting

- "Could not reach DaVinci Resolve": Resolve isn't open, isn't Studio, or external scripting isn't set to Local.
- A camera shows "no audio link": none of its clips share sound with a recorder. Its clips stay on its own
  clock. Raise `--max-skew` if that camera's clock might be more than 2 hours off.
- Many clips "by camera offset": scratch audio too quiet/noisy or music-only; those still follow the camera's
  corrected clock, typically within half a second.
