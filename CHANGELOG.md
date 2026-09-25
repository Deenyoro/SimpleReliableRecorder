# Changelog

All notable changes to SimpleReliableRecorder are listed here. Versions match
the git release tags (bare `X.Y.Z`, no `v` prefix).

## [0.0.20] - 2026-09-25

### Fixed
- Importing the app no longer loads the `keyboard` hotkey library; it loads on
  first use of global hotkeys. On macOS that library reads the keyboard layout
  as soon as it is imported and aborts the whole process when there is no login
  session, which is what made the macOS arm64 CI build crash before any test
  ran. `SRR_DISABLE_HOTKEYS=1` turns hotkeys off without loading it.

### CI
- The macOS jobs set `SRR_DISABLE_HOTKEYS=1`. All five jobs (Linux test and
  build, Windows, macOS arm64 and x64) now pass on the GitLab runners.

## [0.0.19] - 2026-09-25

No app changes; the Windows and macOS CI builds now pass.

### CI
- The Windows and macOS build jobs skip the Tk UI tests
  (`SRR_SKIP_GUI_TESTS=1`). Those shell runners run as a service with no
  interactive desktop, so Tk windows never map on Windows and Tk aborts the
  test process on macOS; the first real 0.0.18 pipeline failed there. The UI
  tests still run in the Linux job. The switch is checked before Tk starts.
- A Tk UI test now accepts newer Tk 8.6 reporting an unset `underline` as an
  empty string instead of `-1`.

## [0.0.18] - 2026-09-24

No app changes; this release adds GitLab CI/CD. The app works exactly as in
0.0.17.

### CI
- New GitLab pipeline (`.gitlab-ci.yml`) on the self-hosted runners, since
  CI/CD is moving off GitHub Actions. It runs on release tags (bare `X.Y.Z`
  or `vX.Y.Z`) and when started by hand; plain pushes and merge requests do
  not start it.
- A test job runs the unit tests on Python 3.12 first, and a tag build fails
  if `recorder.__version__` does not match the tag.
- Builds for Linux x64 (Ubuntu 22.04, so it runs on glibc 2.35 and newer),
  Windows x64 (portable zip and installer) and macOS Apple Silicon and Intel.
  Each build runs the tests again on its own Python before packaging.
- Every build bundles a static ffmpeg checked for the right architecture, no
  extra libraries and that it runs, with the same sources as the GitHub
  workflow. A new check fails the build if ffmpeg, the icon or the audio,
  tray or hotkey packages are missing from the app.
- The Windows and macOS runners install their build tools on first use from
  pinned, checksum-verified downloads (`ci/tools-windows.ps1`,
  `ci/tools-macos.sh`).
- Tag pipelines publish the release files to the GitLab package registry and
  create or update the GitLab Release, using this changelog's section for
  the tag as the notes. Republishing updates links and never deletes any.
- Windows arm64 and Linux arm64 are not built on GitLab (no arm64 runners).

### Docs
- README: new "Building / Releases (GitLab CI)" section explaining when the
  pipeline runs, what each job does and which files a release contains.

## [0.0.17] - 2026-09-24

Everything that changed since 0.0.16 (commit 918a73e).

### Fixed
- Mute and Volume clicks (and push-to-talk) made while Record is still
  starting, or during an automatic audio restart, now apply to the take
  instead of only changing the card. This was a privacy problem.
- Mute and Volume follow the card that is actually recorded: a muted
  duplicate card ("Already added above") no longer silences the whole take,
  and two different devices with the same name keep their own settings.
- Library takes whose first screen segment was deleted are kept when later
  screen-restart segments still exist; missing segments are dropped from the
  entry instead of lingering forever.
- Renaming a recording also moves its screen-restart segments in the library,
  so a later combine still joins every segment.
- The window no longer freezes: opening devices, starting ffmpeg, mid-take
  restarts, quitting while recording, and startup scans run in the background.
- Worker threads no longer call Tk directly (could crash or hang on Windows);
  results go through a queue on the UI thread, and that queue keeps flowing
  while an error dialog is open.
- Push-to-talk reacts within one capture block (queue drained every 10 ms
  while recording, 40 ms when idle), so first syllables are not clipped.
- Only one "Stop recording and quit?" question is shown at a time; extra X
  clicks during "Starting..." no longer queue several close attempts.
- The window is restored where it was, fully visible, on the right monitor
  (including secondary monitors left of the primary); 4K screens are no
  longer treated as two stacked monitors.
- The window can snap to half the screen at 125% and 150% scaling.
- The progress bar keeps moving for every queued combine/convert job, and the
  header and footer show the same progress text.
- The Recorded column shows when the take started (matching its name), not
  when Stop was pressed.
- Hotkeys the hotkey library cannot use are refused with a clear reason
  instead of being saved and blamed on "another app".
- Dialog access keys (Alt+letter) work with Caps Lock on.
- Recordings list, Saved strip and header fit snapped and minimum-size
  windows: buttons wrap or move to their own line instead of being cut off,
  and names end in "..." instead of being cut mid-letter.
- Length/Size no longer stay "..." for rows added while metadata was loading.

### Added
- Real percentage and time left while combining or converting
  ("Combining 1 of 3 - 42%, about 2 min left").
- Cancel stops the combine or convert that is running (not only queued ones),
  removes the unfinished output and never touches the original recordings.
- Splash screen as soon as the Windows exe is double-clicked (not shown for
  the watchdog child process; `SRR_NO_SPLASH=1` builds without it).
- "Saved - 2 tracks - 3:12 - 41 MB" strip after Stop with Open folder,
  Rename and Play.
- Recordings list as a real Windows list: multi-select, sortable columns
  (sort remembered), F2 rename, Del remove (files are never deleted),
  Enter/double-click opens the folder, right-click menu.
- Themed dialogs with verb buttons, Alt+letter access keys, error details,
  Copy and Open log.
- "Set key..." button to capture a hotkey, a Transcription tab in Settings,
  Restore defaults per tab, keyboard shortcuts Ctrl+L (activity log),
  Ctrl+, (Settings) and Ctrl+O (recordings folder).
- The window title shows "REC 00:12:04" while recording.
- Window size, position and maximized state, the activity-log drawer and the
  list sort order are remembered (new optional config keys; old config files
  load unchanged).
- Dark title bar on Windows 10/11.

### Changed
- Main window reworked around the record flow: large Record/Stop button with a
  status line, Sources | Recordings split, activity log as a drawer.
- Plain-language wording everywhere: friendly recording names and dates,
  device labels ("Mic: ...", "Sound from: ..."), Settings labels instead of
  config tokens (config.json keeps the same values), and recording-problem
  alerts and errors in everyday sentences (raw details still go to the log).
- Device choice, Remove and screen options are locked during a take; Mute and
  Volume stay live. "+ Add device" adds the next unused device and duplicates
  are flagged.
- Dark theme: dark dropdown lists, Segoe UI fonts, DPI-scaled widgets, and
  scrollbars only when content overflows. Record stands out again.
- Audio devices are enumerated once at startup instead of 3-5 times.
- Removed an unused import and a dead variable in `ui/widgets.py`.
- Internal tidy-ups: one shared splash helper (`ui/splash.py`), Record
  colours moved into the colour tokens, toggle-switch cleanup on destroy.
- Version in code (`recorder.__version__`) and the installer's default
  version now read 0.0.17 (previously an unused "1.0.0" and "0.0.0").

### Tests
- New stdlib unittest suite under `tests/`: crash-safe WAV writer, recordings
  library, plain-language helpers, combine progress, hotkeys, watchdog
  messages, app logic (mute/volume during start, duplicate cards) and Tk
  widget/app tests (skipped without a display).

### CI
- The release build runs the unit tests on every platform before building
  binaries.
