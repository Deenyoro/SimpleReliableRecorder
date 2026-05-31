# SimpleReliableRecorder

A dead-simple, safety-first recorder. Pick your microphone and your system
playback, hit **RECORD**, and it writes them to disk continuously so a crash or
power loss costs at most a couple of seconds, never the whole take.

If recording ever stops it is impossible to miss: a flashing gold bar drops
across the bottom of the window, the taskbar flashes, an alert sound plays, and
an independent watchdog process raises an OS message box even if the app itself
hangs. Every alert channel (sound, gold banner, taskbar flash, watchdog popup)
and the background watchdog can be turned on or off in Settings.

## Features

- **Audio**: any number of microphones plus system-playback devices via WASAPI
  loopback (no virtual cable needed). Add more with the **+** buttons.
- **Save modes**:
  - Separate file per device (default, safest)
  - Separate channels in one file (mic = ch1, playback = ch2, ...)
  - Single mixed file
- **Per-device gain + live OBS-style level meters** so you can balance every
  source before and during recording.
- **Screen recording** (optional, fully independent): pick a monitor from a
  dropdown (**Identify screens** shows a big number on each display). Encoders:
  NVIDIA NVENC, Intel Quick Sync, AMD AMF, Apple VideoToolbox, or CPU, with
  automatic fallback. Records silent video; combine with audio afterward.
- **Combine (after the fact, non-destructive)**: mux video and audio into one
  file, merge per-device WAVs into one multichannel WAV, or mix to stereo. Your
  original separate tracks are always kept.
- **Resilience**: in-process monitors plus a separate watchdog process and
  heartbeat files, with optional auto-restart on failure.
- **Immense logging**: rotating logs in the data folder plus a live in-app log.

## Download

Prebuilt binaries are attached to each release for Windows, macOS, and Linux
(x64 and arm64). On Windows you can grab either the portable single EXE or the
installer.

## Run from source

```powershell
pip install -r requirements.txt
python main.py
```

## Build the single executable

```powershell
python build.py --clean
# -> dist\SimpleReliableRecorder.exe  (ffmpeg bundled inside)
```

`build.py` stages an ffmpeg binary from `FFMPEG_EXE`, `./ffmpeg/`, or your PATH.
Use a full build (gyan.dev "full"/"git" or BtbN) so NVENC/QSV/AMF are present.

## Where files go

- Recordings: `Videos\SimpleReliableRecorder\<timestamp>\` (changeable, or "ask
  every time").
- Config and logs: next to the executable if that folder is writable, otherwise
  `%APPDATA%\SimpleReliableRecorder\` (Windows) or the platform user data dir.

## Quick start

1. The default mic and system playback are added automatically on first run.
2. Optional: enable **Record a screen**, click **Identify screens**, pick the monitor.
3. Hit **RECORD**. Watch the green status lights. Hit **STOP** when done.
4. Optional: use the **Combine** buttons to stitch tracks together.
