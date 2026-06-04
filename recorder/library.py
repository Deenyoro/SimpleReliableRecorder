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


def prune(entries):
    """Return (kept, removed_count). Keeps an entry if at least one of its files
    still exists; drops files that no longer exist from kept entries."""
    kept, removed = [], 0
    for e in entries or []:
        audio = [a for a in e.get("audio", []) if _exists(a)]
        video = e.get("video", "") if _exists(e.get("video", "")) else ""
        if audio or video:
            ne = dict(e)
            ne["audio"] = audio
            ne["video"] = video
            kept.append(ne)
        else:
            removed += 1
            log.info("Recordings library: pruning '%s' (files no longer present)",
                     e.get("name"))
    return kept, removed


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
            audio, video = [], ""
            for f in sorted(os.listdir(sub)):
                full = os.path.join(sub, f)
                if not os.path.isfile(full):
                    continue
                low = f.lower()
                # Skip our own merged/combined exports - they are outputs.
                if "merged" in low or "video-with-audio" in low \
                        or "audio-mixed" in low or "audio-multitrack" in low \
                        or "combined" in low:
                    continue
                if low.endswith(_AUDIO_EXTS):
                    audio.append(full)
                elif low.endswith(_VIDEO_EXTS):
                    if not video:
                        video = full
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
