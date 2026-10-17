# Changelog

Newest first. Anything that changes a command-line flag, a config key or the default behavior is marked **Changed**.

## 1.0.0 (2026-10-16)

First public release.

- Sync multi-camera footage and audio recorders by audio, or by LTC timecode recorded on camera audio + recorder BWF timecode.
- Builds a new DaVinci Resolve timeline (Studio, external scripting) or writes FCPXML (free Resolve, Final Cut Pro, Premiere Pro).
- One track per camera, recorders on audio tracks, gaps closed by default (`--keep-gaps` to keep real time).
- Clip colors by camera, time of day, automatic parts of the day from recording names, or by content (visual review).
- Personal defaults in `~/.resolve-multicam-sync.json`.
- Setup uses a private `.venv` inside the repo (`requirements.txt`).
