# SimpleReliableRecorder

A dead-simple, safety-first recorder. Pick your microphone and your system
playback, hit **RECORD**, and it writes them to disk continuously so a crash or
power loss costs at most a couple of seconds, never the whole take.

If recording ever stops it is impossible to miss: a flashing gold bar drops
across the bottom of the window, the taskbar flashes, an alert sound plays, and
an independent watchdog process raises an OS alert even if the app itself
hangs. Every alert channel (sound, gold banner, taskbar flash, watchdog alert)
and the background watchdog can be turned on or off in Settings.

## Features

- **Audio**: any number of microphones plus system-playback devices (WASAPI
  loopback on Windows, no virtual cable needed). Add more with the **+** buttons.
- **Save modes**:
  - Separate file per device (default, safest)
  - Separate channels in one file (mic = ch1, playback = ch2, ...)
  - Single mixed file
- **Per-device gain + live level meters** so you can balance every source
  before and during recording.
- **Screen recording** (optional, fully independent): pick a monitor from a
  dropdown (**Identify screens** shows a big number on each display). Encoders:
  NVIDIA NVENC, Intel Quick Sync, AMD AMF, Apple VideoToolbox, or CPU, with
  automatic fallback. Records silent video; combine with audio afterward.
- **Combine (after the fact, non-destructive)**: mux video and audio into one
  file, merge per-device WAVs into one multichannel WAV, mix to stereo, or
  convert every ticked recording to another format in one go. Several jobs
  queue up and run back to back. Your original separate tracks are always kept.
- **Transcription (optional, via [Scrivox](https://github.com/Deenyoro/Scrivox))**:
  when a Scrivox install is detected, the library grows a **Transcribe with
  Scrivox** button - tick recordings and get a transcript saved next to each
  one, either audio-only or with on-screen descriptions for takes that include
  video. See below.
- **Resilience**: in-process monitors plus a separate watchdog process and
  heartbeat files, with optional auto-restart on failure.
- **Immense logging**: rotating logs in the data folder plus a live in-app log.

## Platform notes

- **Windows**: system playback is captured via WASAPI loopback; everything
  works out of the box.
- **macOS**: microphone capture works natively, but capturing system playback
  (loopback) requires a virtual audio device such as BlackHole.
- **Linux**: screen capture uses X11. On Wayland sessions it requires
  XWayland; native Wayland capture (portals) is not supported.

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

## Transcription with Scrivox

[Scrivox](https://github.com/Deenyoro/Scrivox) is a standalone GPU
transcription suite (Whisper + speaker diarization + on-screen descriptions +
summaries). When both apps are present, SimpleReliableRecorder finds it
automatically - no setup:

- Portable: put the Scrivox folder (or `Scrivox.exe`) next to
  `SimpleReliableRecorder.exe`, or keep both app folders side by side.
- Installed: any installed Scrivox is found via the usual install locations,
  the registry, and PATH.

Tick recordings in the library and click **Transcribe with Scrivox...** (also
available on right-click). Choose audio-only or, for takes with video,
"describe what's on screen" as well; each transcript is saved next to its
recording as .txt / .md / .srt / .vtt / .json. Multi-track takes are mixed
automatically for transcription - your originals are never touched.

The transcription itself (model, language, speaker names, API keys,
translation, description detail) follows whatever you configured in Scrivox:
the **Open Scrivox** button in Settings (or in the transcribe dialog) opens it
to change them.

If Scrivox is not present, none of this UI exists - the app stays exactly as
lean as before. Unusual setups can point the optional `scrivox_path` key in
`config.json` at a specific `Scrivox.exe`.
