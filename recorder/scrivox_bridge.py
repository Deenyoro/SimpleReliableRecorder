"""Optional Scrivox integration: transcribe library recordings.

Scrivox (https://github.com/Deenyoro/Scrivox) is a standalone GPU transcription
suite by the same author. When a Scrivox install is found near this app, the
recordings library grows a "Transcribe" action; when it is not found, nothing
Scrivox-related is shown anywhere - the integration is invisible unless both
apps are present.

Detection is fully automatic - no user setup needed. Order (first hit wins):
  1. "scrivox_path" in config.json - optional hand-set override, file or folder.
  2. Scrivox.exe in the same folder as our exe (both portables together).
  3. A Scrivox*/ folder next to our exe or next to OUR folder (Scrivox ships
     as a onedir folder: Scrivox, Scrivox-Full or Scrivox-Lite), including one
     extra nesting level for how archives usually extract
     (Scrivox-Full-v1.7.0-win64/Scrivox-Full/Scrivox.exe).
  4. Windows registry (installed instances: App Paths + Uninstall entries).
  5. Standard install locations (Program Files, %LocalAppData%\\Programs).
  6. Common portable-extract spots (Downloads, Desktop, user home).
  7. Scrivox on PATH.

Transcription runs Scrivox's headless CLI with --use-config, so the model,
language, diarization, API keys etc. are whatever the user configured in the
Scrivox GUI - SRR only decides the input, the output path/format, and whether
to add on-screen descriptions (--vision). Multi-track recordings are first
mixed to a temp stereo WAV (or muxed with the video for vision) with the
bundled ffmpeg; originals are never touched.
"""

import glob
import os
import shutil
import subprocess
import sys
import tempfile
import time

from . import combine, paths
from .ffmpeg_tools import CREATE_NO_WINDOW, _startupinfo
from .logging_setup import get_logger

log = get_logger("gui")

_EXE_NAME = "Scrivox.exe" if sys.platform == "win32" else "Scrivox"

# Transcription can be legitimately slow (model load/download, GPU queueing,
# vision LLM calls), so the floor is high; media length scales it further.
_MIN_TIMEOUT = 7200


def _registry_dirs():
    """Folders suggested by the Windows registry: App Paths entries and any
    installed program whose display name mentions Scrivox."""
    if sys.platform != "win32":
        return []
    dirs = []
    try:
        import winreg
    except ImportError:
        return dirs
    app_paths = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Scrivox.exe"
    uninstall_keys = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    )

    def _value(key, name):
        try:
            val, _ = winreg.QueryValueEx(key, name)
            # App Paths values are often quoted, and REG_EXPAND_SZ values
            # (%ProgramFiles%\...) are returned unexpanded.
            return os.path.expandvars(str(val or "").strip().strip('"'))
        except OSError:
            return ""

    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(root, app_paths) as k:
                val = _value(k, None)
                if val:
                    dirs.append(os.path.dirname(val))
        except OSError:
            pass
        for ukey in uninstall_keys:
            try:
                with winreg.OpenKey(root, ukey) as k:
                    for i in range(winreg.QueryInfoKey(k)[0]):
                        try:
                            with winreg.OpenKey(k, winreg.EnumKey(k, i)) as sk:
                                if "scrivox" not in _value(sk, "DisplayName").lower():
                                    continue
                                loc = _value(sk, "InstallLocation")
                                if loc:
                                    dirs.append(loc)
                        except OSError:
                            continue
            except OSError:
                pass
    return dirs


def _candidate_dirs():
    here = paths.exe_dir()
    dirs = [here]
    roots = [here]
    parent = os.path.dirname(here)
    if parent and parent != here:
        roots.append(parent)
    if sys.platform == "win32":
        for env in ("ProgramFiles", "ProgramFiles(x86)",):
            base = os.environ.get(env)
            if base:
                roots.append(base)
        lad = os.environ.get("LocalAppData")
        if lad:
            roots.append(os.path.join(lad, "Programs"))
        home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
        for sub in ("Downloads", "Desktop", ""):
            roots.append(os.path.join(home, sub) if sub else home)
    dirs += _registry_dirs()
    for root in roots:
        for d in sorted(glob.glob(os.path.join(root, "Scrivox*"))):
            dirs.append(d)
            # One nesting level deeper: extracting Scrivox-Full-v1.7.0-win64.7z
            # into a folder of the same name leaves Scrivox-Full/ inside it.
            dirs += sorted(glob.glob(os.path.join(d, "Scrivox*")))
    return dirs


# The full sweep (registry + a dozen globs) can take a while on machines with
# redirected profiles, and find_scrivox is called from the Tk thread on every
# library refresh - so the result is cached briefly. A cached hit re-validates
# with a cheap isfile so a removed Scrivox disappears immediately.
_CACHE_TTL = 30.0
_cache = {"exe": None, "override": None, "ts": -1e9}


def find_scrivox(override="", force=False):
    """Full path to Scrivox.exe, or None when Scrivox is not present.

    `override` is the optional hand-set path from config.json; it may point at
    the exe itself or at its folder. force=True (the library's Refresh button)
    bypasses the cache so a freshly extracted Scrivox is picked up on demand;
    otherwise a new install appears within _CACHE_TTL seconds.
    """
    now = time.monotonic()
    if not force and _cache["override"] == override \
            and now - _cache["ts"] < _CACHE_TTL:
        exe = _cache["exe"]
        if exe is None or os.path.isfile(exe):
            return exe
    exe = _find_scrivox_uncached(override)
    _cache.update(exe=exe, override=override, ts=now)
    return exe


def _find_scrivox_uncached(override=""):
    if override:
        p = os.path.expandvars(os.path.expanduser(override.strip()))
        if os.path.isfile(p):
            return p
        exe = os.path.join(p, _EXE_NAME)
        if os.path.isfile(exe):
            return exe
        log.warning("Configured scrivox_path not found: %s", override)
    for d in _candidate_dirs():
        exe = os.path.join(d, _EXE_NAME)
        if os.path.isfile(exe):
            return exe
    found = shutil.which("Scrivox")
    if found:
        return found
    # Dev convenience: a Scrivox source checkout next to this project.
    sibling = os.path.join(os.path.dirname(paths.exe_dir()), "scrivox", "main.py")
    if not paths.is_frozen() and os.path.isfile(sibling):
        return sibling
    return None


def _scrivox_cmd(exe, args):
    """Command list to run Scrivox with `args` (handles the dev .py case)."""
    if exe.lower().endswith(".py"):
        # In a frozen build sys.executable is THIS app, not python.
        py = sys.executable if not paths.is_frozen() \
            else (shutil.which("python") or "python")
        return [py, exe] + args
    return [exe] + args


def open_scrivox(exe):
    """Launch the Scrivox GUI (no args = GUI mode) for settings changes."""
    try:
        subprocess.Popen(_scrivox_cmd(exe, []), cwd=os.path.dirname(exe),
                         creationflags=(CREATE_NO_WINDOW if sys.platform == "win32" else 0))
        return True
    except Exception as e:
        log.error("Could not launch Scrivox: %s", e)
        return False


# Output formats offered by the transcribe dialog: label -> (--format, ext).
TRANSCRIBE_FORMATS = {
    "Plain text (.txt)": ("txt", "txt"),
    "Markdown (.md)": ("md", "md"),
    "Subtitles (.srt)": ("srt", "srt"),
    "Subtitles (.vtt)": ("vtt", "vtt"),
    "JSON (.json)": ("json", "json"),
}


def _kill_tree(proc):
    """Kill a timed-out Scrivox and its children (a plain kill would leave
    GPU workers alive, holding the temp input file open)."""
    try:
        import psutil
        children = psutil.Process(proc.pid).children(recursive=True)
    except Exception:
        children = []
    for c in children:
        try:
            c.kill()
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.communicate(timeout=10)
    except Exception:
        pass


def _unique(path):
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    for i in range(2, 100):
        cand = f"{stem}_{i}{ext}"
        if not os.path.exists(cand):
            return cand
    return path


def _prepare_input(entry, want_vision, tmp_dir):
    """Build the single input file Scrivox needs from a library entry.

    Returns (input_path, error). Multi-track audio is mixed to a temp stereo
    WAV; for vision the mixed audio is muxed with the video into a temp MKV so
    one file carries both. Single plain files are passed through untouched.
    """
    audio = [a for a in entry.get("audio", []) if a and os.path.isfile(a)]
    video = entry.get("video", "")
    video = video if video and os.path.isfile(video) else ""

    if want_vision and video:
        if not audio:
            return video, None  # backfilled entries may carry AV in one file
        tmp = os.path.join(tmp_dir, "srr_vision_input.mkv")
        ok, detail = combine.combine_av(video, audio, tmp, audio_mode="mix")
        if not ok:
            return None, f"could not prepare video+audio input: {detail}"
        return tmp, None

    if len(audio) == 1:
        return audio[0], None
    if audio:
        tmp = os.path.join(tmp_dir, "srr_audio_input.wav")
        ok, detail = combine.mix_audio_to_stereo(audio, tmp)
        if not ok:
            return None, f"could not mix the audio tracks: {detail}"
        return tmp, None
    if video:
        return video, None  # audio-less entry: let Scrivox read the video's sound
    return None, "this recording has no usable audio or video files"


def transcribe_entry(exe, entry, want_vision, fmt="txt", ext="txt",
                     on_status=None):
    """Transcribe one library entry with Scrivox. Blocking; run off the UI
    thread. Returns (ok, transcript_path_or_error_detail)."""
    def status(msg):
        if on_status:
            on_status(msg)

    out_dir = entry.get("out_dir") or ""
    if not os.path.isdir(out_dir):
        for p in entry.get("audio", []) + [entry.get("video", "")]:
            if p and os.path.isfile(p):
                out_dir = os.path.dirname(p)
                break
    if not os.path.isdir(out_dir):
        return False, "the recording's folder no longer exists"

    name = (entry.get("name") or "recording").strip() or "recording"
    out_path = _unique(os.path.join(out_dir, f"{name}_transcript.{ext}"))

    tmp_dir = tempfile.mkdtemp(prefix="srr_scrivox_")
    try:
        status("preparing audio...")
        input_path, err = _prepare_input(entry, want_vision, tmp_dir)
        if err:
            return False, err

        args = [input_path, "--use-config", "--format", fmt, "-o", out_path]
        # Explicit either way: the user's choice in OUR dialog must beat any
        # vision preference saved in the Scrivox GUI config.
        args.append("--vision" if want_vision else "--no-vision")
        cmd = _scrivox_cmd(exe, args)

        dur = combine._probe_media(input_path).get("duration") or 0
        timeout = max(_MIN_TIMEOUT, int(10 * dur))

        status("transcribing (this can take a while)...")
        log.info("scrivox: %s", " ".join(cmd))
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True,
                                    encoding="utf-8", errors="replace",
                                    cwd=os.path.dirname(exe),
                                    creationflags=CREATE_NO_WINDOW,
                                    startupinfo=_startupinfo())
        except Exception as e:
            return False, f"could not run Scrivox: {e}"
        try:
            out_s, err_s = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            return False, (f"Scrivox did not finish within {timeout} seconds "
                           "and was stopped")

        # The output file is the authoritative success signal: a windowed exe's
        # exit code / stderr can be unreliable across launch environments.
        if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            log.info("scrivox OK -> %s", out_path)
            return True, out_path
        tail = ((err_s or "") + "\n" + (out_s or "")).strip()[-800:]
        log.error("scrivox failed (rc=%s):\n%s", proc.returncode, tail)
        return False, tail or f"Scrivox exited with code {proc.returncode}"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
