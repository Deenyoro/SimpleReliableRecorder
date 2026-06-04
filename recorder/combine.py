"""Post-recording combine/mux — always non-destructive (originals untouched).

Per the safety-first design, recording produces separate raw tracks. This module
optionally stitches them together *after the fact* using the bundled ffmpeg:

  * combine_av()              — screen video + audio -> one playable file.
  * merge_audio_to_channels() — N mono WAVs -> one N-channel WAV (Audacity-ready).
  * mix_audio_to_stereo()     — N WAVs -> one mixed stereo WAV.
"""

import os
import subprocess

from . import ffmpeg_tools
from .ffmpeg_tools import CREATE_NO_WINDOW, _startupinfo
from .logging_setup import get_logger

log = get_logger("screen")  # combine is ffmpeg work; share the screen log


def _run(cmd):
    log.info("combine: %s", " ".join(cmd))
    try:
        res = subprocess.run(cmd, capture_output=True, text=True,
                             creationflags=CREATE_NO_WINDOW,
                             startupinfo=_startupinfo())
        if res.returncode != 0:
            tail = (res.stderr or "")[-1500:]
            log.error("combine failed (rc=%s):\n%s", res.returncode, tail)
            return False, tail
        log.info("combine OK")
        return True, (res.stderr or "")[-400:]
    except Exception as e:
        log.exception("combine exception: %s", e)
        return False, str(e)


def combine_av(video_path, audio_paths, out_path, audio_mode="mix"):
    """Mux a video with one or more audio files into out_path.

    audio_mode:
      "mix"      -> all audio summed into a single stereo track (default).
      "tracks"   -> each audio kept as its own selectable track in the file.
    Video is stream-copied (no re-encode) for speed and quality.
    """
    if not os.path.isfile(video_path):
        return False, f"video not found: {video_path}"
    audio_paths = [a for a in audio_paths if a and os.path.isfile(a)]
    if not audio_paths:
        return False, "no audio inputs"

    ff = ffmpeg_tools.ffmpeg_exe()
    cmd = [ff, "-hide_banner", "-y", "-i", video_path]
    for a in audio_paths:
        cmd += ["-i", a]

    n = len(audio_paths)
    if audio_mode == "mix" and n > 1:
        inputs = "".join(f"[{i+1}:a]" for i in range(n))
        cmd += ["-filter_complex", f"{inputs}amix=inputs={n}:normalize=0[aout]",
                "-map", "0:v:0", "-map", "[aout]"]
        cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "256k", out_path]
    elif audio_mode == "tracks":
        cmd += ["-map", "0:v:0"]
        for i in range(n):
            cmd += ["-map", f"{i+1}:a:0"]
        cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "256k", out_path]
    else:  # single audio
        cmd += ["-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "256k", out_path]
    return _run(cmd)


def merge_audio_to_channels(audio_paths, out_path):
    """Merge N (mono) WAVs into one N-channel WAV. Great for Audacity editing."""
    audio_paths = [a for a in audio_paths if a and os.path.isfile(a)]
    if len(audio_paths) < 2:
        return False, "need at least two audio files to merge"
    ff = ffmpeg_tools.ffmpeg_exe()
    cmd = [ff, "-hide_banner", "-y"]
    for a in audio_paths:
        cmd += ["-i", a]
    n = len(audio_paths)
    inputs = "".join(f"[{i}:a]" for i in range(n))
    cmd += ["-filter_complex", f"{inputs}amerge=inputs={n}[aout]",
            "-map", "[aout]", out_path]
    return _run(cmd)


def mix_audio_to_stereo(audio_paths, out_path):
    """Sum N WAVs into a single stereo mix."""
    audio_paths = [a for a in audio_paths if a and os.path.isfile(a)]
    if not audio_paths:
        return False, "no audio inputs"
    ff = ffmpeg_tools.ffmpeg_exe()
    cmd = [ff, "-hide_banner", "-y"]
    for a in audio_paths:
        cmd += ["-i", a]
    n = len(audio_paths)
    if n == 1:
        cmd += ["-ac", "2", out_path]
    else:
        inputs = "".join(f"[{i}:a]" for i in range(n))
        cmd += ["-filter_complex",
                f"{inputs}amix=inputs={n}:normalize=0[aout]",
                "-map", "[aout]", "-ac", "2", out_path]
    return _run(cmd)


def concat_sessions(sessions, out_path, include_video=False):
    """Join several recording sessions end to end into one file.

    `sessions` is a list of dicts: {"audio": [wav,...], "video": path|""}.
    Within each session the audio tracks are mixed to stereo; sessions are then
    concatenated in order. If include_video is True and every session has a
    video, the videos are concatenated and the mixed audio muxed alongside.
    Re-encodes (the inputs have different start times / codecs), so this is a
    one-off convenience export; the originals are never touched.
    """
    sessions = [s for s in sessions if s and
                ([a for a in s.get("audio", []) if a and os.path.isfile(a)]
                 or (include_video and os.path.isfile(s.get("video", ""))))]
    if not sessions:
        return False, "no usable sessions selected"

    ff = ffmpeg_tools.ffmpeg_exe()
    cmd = [ff, "-hide_banner", "-y"]
    # Build the input list and remember each input's index.
    idx = 0
    seg_audio_idx = []   # list of (list-of-audio-input-indexes) per session
    seg_video_idx = []   # video input index per session (or None)
    do_video = include_video and all(os.path.isfile(s.get("video", ""))
                                     for s in sessions)
    for s in sessions:
        a_idx = []
        for a in s.get("audio", []):
            if a and os.path.isfile(a):
                cmd += ["-i", a]
                a_idx.append(idx); idx += 1
        seg_audio_idx.append(a_idx)
        if do_video:
            cmd += ["-i", s["video"]]
            seg_video_idx.append(idx); idx += 1
        else:
            seg_video_idx.append(None)

    filt = []
    seg_audio_labels = []
    for si, a_idx in enumerate(seg_audio_idx):
        if not a_idx:
            # No audio in this session: synthesize silence so the concat aligns.
            lbl = f"sa{si}"
            filt.append(f"anullsrc=channel_layout=stereo:sample_rate=48000[{lbl}]")
            seg_audio_labels.append(lbl)
            continue
        if len(a_idx) == 1:
            src = f"[{a_idx[0]}:a]"
            lbl = f"sa{si}"
            filt.append(f"{src}aformat=sample_rates=48000:channel_layouts=stereo[{lbl}]")
        else:
            ins = "".join(f"[{i}:a]" for i in a_idx)
            lbl = f"sa{si}"
            filt.append(f"{ins}amix=inputs={len(a_idx)}:normalize=0,"
                        f"aformat=sample_rates=48000:channel_layouts=stereo[{lbl}]")
        seg_audio_labels.append(lbl)

    if do_video:
        # Concatenate video+audio pairs together.
        vlabels = []
        for si in range(len(sessions)):
            vi = seg_video_idx[si]
            vlbl = f"sv{si}"
            filt.append(f"[{vi}:v]scale=trunc(iw/2)*2:trunc(ih/2)*2,"
                        f"setsar=1,format=yuv420p[{vlbl}]")
            vlabels.append(vlbl)
        pairs = "".join(f"[{vlabels[i]}][{seg_audio_labels[i]}]"
                        for i in range(len(sessions)))
        filt.append(f"{pairs}concat=n={len(sessions)}:v=1:a=1[vout][aout]")
        cmd += ["-filter_complex", ";".join(filt),
                "-map", "[vout]", "-map", "[aout]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "256k", out_path]
    else:
        ins = "".join(f"[{lbl}]" for lbl in seg_audio_labels)
        filt.append(f"{ins}concat=n={len(seg_audio_labels)}:v=0:a=1[aout]")
        cmd += ["-filter_complex", ";".join(filt),
                "-map", "[aout]", out_path]
    return _run(cmd)
