"""Fail if a frozen SimpleReliableRecorder build is missing something it needs.

    python ci/check_bundle.py dist/SimpleReliableRecorder.exe   # Windows
    python ci/check_bundle.py dist/SimpleReliableRecorder       # Linux / macOS

Reads the PyInstaller single-file archive with PyInstaller's own reader (run
it with the same Python/PyInstaller that built the binary) and checks that
the bundled ffmpeg, the icon and the audio / tray / hotkey packages (with
this platform's backends) are really inside. simplereliablerecorder.spec
bundles ffmpeg only "if os.path.isfile(...)", and build.py only warns when
it could not stage one, so a build without ffmpeg would otherwise succeed
and ship a recorder whose screen recording and combine silently fail.
"""

from __future__ import annotations

import sys

FFMPEG = "ffmpeg/ffmpeg.exe" if sys.platform == "win32" else "ffmpeg/ffmpeg"
# A real static ffmpeg is 60-200 MB; build.py itself rejects anything < 5 MB.
FFMPEG_MIN_BYTES = 20 * 1024 * 1024

REQUIRED_DATA = [FFMPEG, "assets/icon.ico"]
REQUIRED_MODULES = [
    "recorder.safewav", "recorder.audio", "recorder.watchdog", "ui.app",
    "tkinter", "numpy", "soundfile", "soundcard", "screeninfo", "psutil",
    "pystray", "PIL.Image", "keyboard",
]
if sys.platform == "win32":
    REQUIRED_MODULES += ["soundcard.mediafoundation", "pystray._win32"]
elif sys.platform == "darwin":
    REQUIRED_MODULES += ["soundcard.coreaudio", "pystray._darwin"]
else:
    REQUIRED_MODULES += ["soundcard.pulseaudio", "pystray._xorg"]


def check(toc: dict, modules: set[str] | list[str]) -> list[str]:
    """Return the required entries missing from an archive listing.

    `toc` maps outer CArchive entry names to PyInstaller's TOC tuples
    (offset, length, uncompressed_length, compression, typecode); `modules`
    are the dotted module names in the embedded PYZ. Entry names use the
    host OS separator (backslashes in a Windows build), so they are
    normalised to "/" before comparing.
    """
    norm = {n.replace("\\", "/"): v for n, v in toc.items()}
    mods = set(modules)
    missing = [m for m in REQUIRED_MODULES if m not in mods]
    missing += [d for d in REQUIRED_DATA if d not in norm]
    if FFMPEG in norm and norm[FFMPEG][2] < FFMPEG_MIN_BYTES:
        missing.append(f"{FFMPEG} (only {norm[FFMPEG][2]} bytes; not a full ffmpeg)")
    return missing


def read_archive(path: str) -> tuple[dict, set[str]]:
    """(outer TOC, PYZ module names) of a single-file PyInstaller build."""
    # Imported here so check() stays usable without PyInstaller.
    from PyInstaller.archive.readers import CArchiveReader

    outer = CArchiveReader(path)
    pyz_name = next((n for n in outer.toc if n.endswith(".pyz")), None)
    if pyz_name is None:
        raise SystemExit(f"FAIL: no PYZ archive inside {path}")
    return outer.toc, set(outer.open_embedded_archive(pyz_name).toc)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    path = argv[1]
    toc, modules = read_archive(path)
    missing = check(toc, modules)
    for m in REQUIRED_MODULES + REQUIRED_DATA:
        bad = any(x == m or x.startswith(m + " ") for x in missing)
        print(f"required  {m:<28} {'MISSING' if bad else 'ok'}")
    if missing:
        print(f"FAIL: {path} is missing: {', '.join(missing)}")
        return 1
    print(f"OK: {path} contains every required component")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
