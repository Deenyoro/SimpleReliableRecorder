"""Filesystem path helpers for SimpleReliableRecorder.

Centralizes where the executable lives, where bundled resources (ffmpeg, icon)
are found in both source and PyInstaller-frozen runs, and where writable data
(config, logs, recordings) goes. Mirrors the "config next to exe if writable,
else %APPDATA%" pattern from the Whisper/Scrivox project.
"""

import os
import sys

APP_NAME = "SimpleReliableRecorder"


def is_frozen():
    """True when running from a PyInstaller-built executable."""
    return getattr(sys, "frozen", False)


def exe_dir():
    """Directory containing the executable (frozen) or the project root (source)."""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    # recorder/paths.py -> project root is one level up from recorder/
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource_path(*parts):
    """Resolve a bundled resource (e.g. ffmpeg/ffmpeg.exe, assets/icon.ico).

    In a frozen onefile build, data is unpacked to sys._MEIPASS. In source runs
    it lives under the project root.
    """
    if is_frozen() and hasattr(sys, "_MEIPASS"):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def _is_writable(path):
    try:
        os.makedirs(path, exist_ok=True)
        test = os.path.join(path, ".write_test")
        with open(test, "w") as fh:
            fh.write("ok")
        os.remove(test)
        return True
    except Exception:
        return False


def data_dir():
    """Writable directory for config + logs.

    Prefer next to the exe (portable). Fall back to %APPDATA%\\SimpleReliableRecorder.
    """
    candidate = exe_dir()
    if _is_writable(candidate) and not is_frozen():
        # In source runs, keep data in the project dir for convenience.
        d = os.path.join(candidate, "_data")
        if _is_writable(d):
            return d
    # Frozen or non-writable: try next-to-exe portable folder first.
    if is_frozen():
        portable = os.path.join(candidate, "data")
        if _is_writable(portable):
            return portable
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(appdata, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def logs_dir():
    d = os.path.join(data_dir(), "logs")
    os.makedirs(d, exist_ok=True)
    return d


def default_recordings_dir():
    """Videos\\SimpleReliableRecorder (created on demand)."""
    userprofile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    videos = os.path.join(userprofile, "Videos")
    if not os.path.isdir(videos):
        videos = userprofile
    d = os.path.join(videos, APP_NAME)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def ffmpeg_path():
    """Path to the bundled ffmpeg.exe, falling back to a PATH lookup."""
    bundled = resource_path("ffmpeg", "ffmpeg.exe")
    if os.path.isfile(bundled):
        return bundled
    # Source-run convenience / fallback: use system ffmpeg if present.
    from shutil import which
    found = which("ffmpeg")
    return found or bundled  # return bundled path even if missing so caller can log it


def icon_path():
    p = resource_path("assets", "icon.ico")
    return p if os.path.isfile(p) else None
