"""Recordings library: a persistent queue of finished takes.

Lets you record repeatedly without closing the app, then combine any selection of
past recordings into a single file later. Entries are stored in the config (so
they survive restarts). On load we prune entries whose files were moved or
deleted, and drop individual files that no longer exist, so the queue always
reflects what is actually on disk.

Each entry is a dict:
    {
      "id":      unique id string,
      "name":    display name (the session folder name),
      "out_dir": session folder,
      "audio":   [wav paths that still exist],
      "video":   video path or "",
      "created": ISO timestamp,
    }
"""

import glob
import os

from .logging_setup import get_logger

log = get_logger("gui")

_AUDIO_EXTS = (".wav",)
_VIDEO_EXTS = (".mkv", ".mp4", ".mov")


def _exists(p):
    try:
        return bool(p) and os.path.isfile(p)
    except Exception:
        return False


def make_entry(entry_id, name, out_dir, audio, video, created):
    return {
        "id": str(entry_id),
        "name": name,
        "out_dir": out_dir,
        "audio": [a for a in (audio or []) if a],
        "video": video or "",
        "created": created,
    }


def _volume_offline(path):
    """True when `path` is missing because its volume/parent looks unreachable
    (NAS share down, USB drive unplugged) rather than because the files were
    deleted. Only treat files as truly gone when their parent folder exists."""
    try:
        if not path:
            return False
        if os.path.isdir(path):
            return False  # the folder is reachable; the files really are gone
        drive, _ = os.path.splitdrive(path)
        if drive and not os.path.isdir(drive + os.sep):
            return True  # whole drive / UNC share is missing -> offline
        parent = os.path.dirname(path.rstrip("\\/"))
        if parent and not os.path.isdir(parent):
            return True  # the configured save folder itself is unreachable
        return False
    except Exception:
        return True  # can't tell -> err on the side of keeping the entry


def prune(entries):
    """Return (kept, removed_count). Keeps an entry if at least one of its files
    still exists; drops files that no longer exist from kept entries. Entries
    whose volume looks offline (unplugged USB, unreachable NAS) are kept
    untouched so a temporarily missing drive never wipes the library."""
    kept, removed = [], 0
    for e in entries or []:
        audio = [a for a in e.get("audio", []) if _exists(a)]
        video = e.get("video", "") if _exists(e.get("video", "")) else ""
        if audio or video:
            ne = dict(e)
            ne["audio"] = audio
            ne["video"] = video
            kept.append(ne)
            continue
        probe = (e.get("out_dir")
                 or os.path.dirname((e.get("audio") or [""])[0]
                                    or e.get("video", "")))
        if _volume_offline(probe):
            kept.append(dict(e))  # drive is away; leave the entry as-is
            log.info("Recordings library: keeping '%s' (volume offline: %s)",
                     e.get("name"), probe)
        else:
            removed += 1
            log.info("Recordings library: pruning '%s' (files no longer present)",
                     e.get("name"))
    return kept, removed


# Our combine/convert outputs are named {prefix}_<kind>_{stamp}, so the kind
# token always appears underscore-delimited in the stem. Matching the
# underscores keeps user files like "remixed.wav" from being mistaken for
# exports.
_EXPORT_MARKERS = ("_merged_", "_multitrack_", "_mixed_", "_converted_")
# Fixed output names used by older releases; matched as whole, delimited words.
_EXPORT_LEGACY = ("video-with-audio", "audio-mixed", "audio-multitrack",
                  "combined")
_TOKEN_DELIMS = "_-. "


def _is_export_file(filename):
    """True if `filename` looks like one of our own combine/convert outputs."""
    stem = os.path.splitext(filename)[0].lower()
    if any(m in stem for m in _EXPORT_MARKERS):
        return True
    for tok in _EXPORT_LEGACY:
        i = stem.find(tok)
        while i != -1:
            j = i + len(tok)
            if ((i == 0 or stem[i - 1] in _TOKEN_DELIMS)
                    and (j == len(stem) or stem[j] in _TOKEN_DELIMS)):
                return True
            i = stem.find(tok, i + 1)
    return False


def scan_folder(root, existing_dirs=None):
    """Discover recording sessions already on disk under `root`.

    Looks for SRR_* session subfolders (the layout the app creates) that contain
    audio and/or video, and returns library entries for any not already present
    (by out_dir). Combined/merged exports are skipped as inputs. This is what
    back-fills recordings made before the library existed.
    """
    found = []
    existing_dirs = set(existing_dirs or [])
    try:
        if not root or not os.path.isdir(root):
            return found
        for sub in sorted(glob.glob(os.path.join(root, "SRR_*"))):
            if not os.path.isdir(sub) or sub in existing_dirs:
                continue
            audio, videos = [], []
            for f in sorted(os.listdir(sub)):
                full = os.path.join(sub, f)
                if not os.path.isfile(full):
                    continue
                low = f.lower()
                # Skip our own merged/combined exports - they are outputs.
                if _is_export_file(f):
                    continue
                if low.endswith(_AUDIO_EXTS):
                    audio.append(full)
                elif low.endswith(_VIDEO_EXTS):
                    videos.append(full)
            video = _pick_video(videos)
            if not audio and not video:
                continue
            name = os.path.basename(sub)
            try:
                created = _mtime_str(sub)
            except Exception:
                created = ""
            found.append(make_entry(
                entry_id="scan-" + name, name=name, out_dir=sub,
                audio=audio, video=video, created=created))
    except Exception as e:
        log.warning("Recordings scan failed for %s: %s", root, e)
    return found


def _pick_video(videos):
    """Choose the session's main video: prefer files from the original run
    over auto-restart fragments, then the largest file."""
    if not videos:
        return ""

    def key(p):
        name = os.path.basename(p).lower()
        try:
            size = os.path.getsize(p)
        except OSError:
            size = 0
        return (1 if "restart" in name else 0, -size)

    return sorted(videos, key=key)[0]


def _mtime_str(path):
    import datetime
    ts = os.path.getmtime(path)
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def summarize(entry):
    """Short human description, e.g. '2 audio + 1 video'."""
    parts = []
    n = len(entry.get("audio", []))
    if n:
        parts.append(f"{n} audio")
    if entry.get("video"):
        parts.append("1 video")
    return " + ".join(parts) if parts else "empty"
