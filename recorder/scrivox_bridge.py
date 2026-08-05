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
mixed to a stereo WAV (or muxed with the video for vision) with the bundled
ffmpeg; the result is SAVED next to the recording as a normal combine export
(same artifact/naming as the Combine buttons) and reused by later runs via
find_precombined. Originals are never touched.
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


def _mux_av_export(entry, out_dir, name):
    """Mux the entry's video + mixed audio into a KEPT export next to the
    recording - the same artifact 'Make one video with sound' produces, named
    the same way, so the work is never thrown away and later transcriptions
    (and the user) reuse it. Returns (path, error)."""
    audio = [a for a in entry.get("audio", []) if a and os.path.isfile(a)]
    video = entry.get("video", "")
    vid_ext = os.path.splitext(video)[1].lstrip(".") or "mkv"
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    out = _unique(os.path.join(out_dir, f"{name}_merged_{stamp}.{vid_ext}"))
    ok, detail = combine.combine_av(video, audio, out, audio_mode="mix")
    if not ok:
        return None, f"could not prepare video+audio input: {detail}"
    log.info("Saved AV export (kept): %s", out)
    return out, None


def _prepare_input(entry, want_vision, out_dir, name, status):
    """Build the single input file Scrivox needs from a library entry.

    Returns (input_path, error). Multi-track audio is mixed, and for vision
    the mix is muxed with the video - both saved as normal combine exports
    next to the recording (NOT temp files), exactly as if the user had
    pressed the corresponding Combine button first. Single plain files are
    passed through untouched.
    """
    audio = [a for a in entry.get("audio", []) if a and os.path.isfile(a)]
    video = entry.get("video", "")
    video = video if video and os.path.isfile(video) else ""

    if want_vision and video:
        if not audio:
            return video, None  # backfilled entries may carry AV in one file
        status("combining video + audio (saved next to the recording)...")
        return _mux_av_export(entry, out_dir, name)

    if len(audio) == 1:
        return audio[0], None
    if audio:
        status("mixing the audio tracks (saved next to the recording)...")
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        out = _unique(os.path.join(out_dir, f"{name}_mixed_{stamp}.wav"))
        ok, detail = combine.mix_audio_to_stereo(audio, out)
        if not ok:
            return None, f"could not mix the audio tracks: {detail}"
        log.info("Saved audio mix export (kept): %s", out)
        return out, None
    if video:
        return video, None  # audio-less entry: let Scrivox read the video's sound
    return None, "this recording has no usable audio or video files"


def default_options():
    """The transcription options the dialog starts from. None everywhere means
    'use whatever is saved in the Scrivox GUI' (the --use-config behavior)."""
    return {
        "vision": False,          # add on-screen descriptions (video entries)
        "fmt": "txt", "ext": "txt",
        "input_mode": "mix",      # "mix": one transcript per recording;
                                  # "tracks": each audio track separately
        "merge": False,           # tracks mode: single combined text file
        "use_precombined": True,  # reuse up-to-date _merged_/_mixed_ exports
                                  # instead of re-muxing/re-mixing the tracks
        "vision_interval": None,  # seconds between screen descriptions
        "diarize": None,          # None=Scrivox setting, True/False=override
        "num_speakers": None,     # exact speaker count when diarize is True
        "model": None,            # whisper model override
        "language": None,         # language code override
        "summarize": None,        # None=Scrivox setting, True/False=override
    }


_PRECOMBINED_VIDEO_EXTS = (".mkv", ".mp4", ".mov")
_PRECOMBINED_AUDIO_EXTS = (".wav", ".flac", ".mp3", ".m4a")


def find_precombined(entry):
    """Combine-exports of this recording that are safe to transcribe directly.

    Returns {"av": path_or_None, "audio": path_or_None}: the newest
    '<name>_merged_*' video (video+sound in one file) and the newest
    '<name>_mixed_*' stereo audio in the entry's folder, but only when the
    export is at least as new as every source track - a stale export from
    before a track was replaced must not silently win over the real tracks.
    """
    found = {"av": None, "audio": None}
    out_dir = entry.get("out_dir") or ""
    name = (entry.get("name") or "").strip()
    if not name or not os.path.isdir(out_dir):
        return found

    sources = [p for p in list(entry.get("audio") or [])
               + [entry.get("video") or ""] if p and os.path.isfile(p)]
    try:
        newest_src = max(os.path.getmtime(p) for p in sources) if sources else 0
    except OSError:
        return found

    def newest(marker, exts):
        best, best_ts = None, -1.0
        try:
            names = os.listdir(out_dir)
        except OSError:
            return None
        for f in names:
            stem, ext = os.path.splitext(f)
            if ext.lower() not in exts:
                continue
            if not stem.lower().startswith(name.lower() + "_"):
                continue
            if marker not in stem.lower():
                continue
            full = os.path.join(out_dir, f)
            try:
                ts = os.path.getmtime(full)
            except OSError:
                continue
            if ts >= newest_src and ts > best_ts:
                best, best_ts = full, ts
        return best

    found["av"] = newest("_merged_", _PRECOMBINED_VIDEO_EXTS)
    found["audio"] = newest("_mixed_", _PRECOMBINED_AUDIO_EXTS)
    return found


def _option_args(opts):
    """CLI overrides for the 'More settings' choices. Only explicit choices
    emit flags; everything left on 'use Scrivox setting' rides on
    --use-config, so the Scrivox GUI stays the single source of defaults."""
    args = []
    if opts.get("model"):
        args += ["--model", opts["model"]]
    if opts.get("language"):
        args += ["--language", opts["language"]]
    diarize = opts.get("diarize")
    if diarize is True:
        args.append("--diarize")
        if opts.get("num_speakers"):
            args += ["--num-speakers", str(int(opts["num_speakers"]))]
    elif diarize is False:
        args.append("--no-diarize")
    summarize = opts.get("summarize")
    if summarize is True:
        args.append("--summarize")
    elif summarize is False:
        args.append("--no-summarize")
    return args


def _vision_args(opts):
    args = ["--vision"]
    if opts.get("vision_interval"):
        args += ["--vision-interval", str(opts["vision_interval"])]
    return args


def _run_scrivox(exe, input_path, out_path, args, status, what):
    """One blocking Scrivox run. Returns (ok, out_path_or_error_detail)."""
    cmd = _scrivox_cmd(exe, [input_path] + args + ["-o", out_path])
    dur = combine._probe_media(input_path).get("duration") or 0
    timeout = max(_MIN_TIMEOUT, int(10 * dur))
    status(f"transcribing {what} (this can take a while)...")
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


def _track_label(name, path):
    """Short label for one track file: 'keithPTOcall_mic-1.wav' -> 'mic-1'."""
    stem = os.path.splitext(os.path.basename(path))[0]
    if stem.lower().startswith(name.lower() + "_"):
        return stem[len(name) + 1:] or stem
    return stem


def transcribe_entry(exe, entry, opts, on_status=None):
    """Transcribe one library entry with Scrivox. Blocking; run off the UI
    thread. `opts` is a default_options()-shaped dict.
    Returns (ok, list_of_transcript_paths) or (False, error_detail)."""
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
    fmt = opts.get("fmt", "txt")
    ext = opts.get("ext", "txt")
    audio = [a for a in entry.get("audio", []) if a and os.path.isfile(a)]
    video = entry.get("video", "")
    video = video if video and os.path.isfile(video) else ""
    want_vision = bool(opts.get("vision")) and bool(video)
    per_track = opts.get("input_mode") == "tracks" and len(audio) > 1
    merge = per_track and bool(opts.get("merge"))

    # Explicit either way: the user's choice in OUR dialog must beat any
    # vision preference saved in the Scrivox GUI config.
    common = ["--use-config", "--format", fmt] + _option_args(opts)

    # An up-to-date combine-export made earlier IS the requested input - reuse
    # it instead of re-muxing/re-mixing (faster, and it honors a hand-tweaked
    # merge, e.g. one remade with different track levels).
    pre = (find_precombined(entry) if opts.get("use_precombined", True)
           else {"av": None, "audio": None})

    tmp_dir = tempfile.mkdtemp(prefix="srr_scrivox_")
    try:
        if not per_track:
            input_path = None
            if video and audio:
                # Audio + a separate screen video always become ONE combined
                # file - vision or not. The merged export is a deliverable in
                # its own right (identical to 'Make one video with sound'),
                # so transcribing IS also the combine step.
                if pre["av"]:
                    status("using your combined video...")
                    log.info("Using pre-combined AV export: %s", pre["av"])
                    input_path = pre["av"]
                else:
                    status("combining video + audio "
                           "(saved next to the recording)...")
                    input_path, err = _mux_av_export(entry, out_dir, name)
                    if err:
                        return False, err
            elif len(audio) > 1 and pre["audio"]:
                status("using your mixed audio...")
                log.info("Using pre-mixed audio export: %s", pre["audio"])
                input_path = pre["audio"]
            if input_path is None:
                input_path, err = _prepare_input(entry, want_vision, out_dir,
                                                 name, status)
                if err:
                    return False, err
            out_path = _unique(os.path.join(out_dir, f"{name}_transcript.{ext}"))
            args = common + (_vision_args(opts) if want_vision
                             else ["--no-vision"])
            ok, detail = _run_scrivox(exe, input_path, out_path, args,
                                      status, "audio")
            return (True, [detail]) if ok else (False, detail)

        # Per-track: one Scrivox run per audio file; with vision also one
        # run over the screen recording (muxed with the mixed audio so the
        # descriptions line up with real speech on the timeline).
        jobs = []  # (label, input_path, is_vision_run)
        if want_vision:
            if pre["av"]:
                status("using your combined video...")
                log.info("Using pre-combined AV export: %s", pre["av"])
                jobs.append(("screen", pre["av"], True))
            elif audio:
                status("combining video + audio (saved next to the recording)...")
                muxed, err = _mux_av_export(entry, out_dir, name)
                if err:
                    return False, err
                jobs.append(("screen", muxed, True))
            else:
                jobs.append(("screen", video, True))
        for a in audio:
            jobs.append((_track_label(name, a), a, False))

        outputs = []  # (label, path)
        for i, (label, inp, is_vision) in enumerate(jobs):
            if merge:
                part_out = os.path.join(tmp_dir, f"part_{i}.{ext}")
            else:
                part_out = _unique(os.path.join(
                    out_dir, f"{name}_{label}_transcript.{ext}"))
            args = common + (_vision_args(opts) if is_vision
                             else ["--no-vision"])
            ok, detail = _run_scrivox(exe, inp, part_out, args, status,
                                      f"{label} ({i + 1}/{len(jobs)})")
            if not ok:
                return False, f"{label}: {detail}"
            outputs.append((label, part_out))

        if not merge:
            return True, [p for _, p in outputs]

        final = _unique(os.path.join(out_dir, f"{name}_transcript.{ext}"))
        with open(final, "w", encoding="utf-8") as out:
            for i, (label, p) in enumerate(outputs):
                header = ("Screen + descriptions" if label == "screen"
                          else f"Track: {label}")
                if fmt == "md":
                    out.write(("\n\n" if i else "") + f"## {header}\n\n")
                else:
                    out.write(("\n\n" if i else "")
                              + "======== " + header + " ========\n\n")
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    out.write(fh.read().strip() + "\n")
        return True, [final]
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
