"""Post-recording combine/mux - always non-destructive (originals untouched).

Per the safety-first design, recording produces separate raw tracks. This module
optionally stitches them together *after the fact* using the bundled ffmpeg:

  * combine_av()              - screen video + audio -> one playable file.
  * merge_audio_to_channels() - N mono WAVs -> one N-channel WAV (Audacity-ready).
  * mix_audio_to_stereo()     - N WAVs -> one mixed stereo WAV.
  * convert()                 - re-encode one recording to another format.
  * concat_sessions()         - join several recordings end to end.

Requires ffmpeg >= 5.1 (amix's `normalize=0` option); the bundled build is 7.x.
Only ffmpeg is bundled (no ffprobe), so media info is read by parsing the
stderr of `ffmpeg -i <file>` - see _probe_media().
"""

import os
import re
import subprocess

from . import ffmpeg_tools
from .ffmpeg_tools import CREATE_NO_WINDOW, _startupinfo
from .logging_setup import get_logger

log = get_logger("screen")  # combine is ffmpeg work; share the screen log

# Floor for the computed per-operation timeout (seconds). Every operation gets
# max(_MIN_TIMEOUT, 4x total input duration) so nothing can hang forever.
_MIN_TIMEOUT = 900

_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_RES_RE = re.compile(r",\s*(\d{2,5})x(\d{2,5})")
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*fps")
_CHANNELS_RE = re.compile(r"(\d+)\s+channels")
_PART_RE = re.compile(r"^(?P<base>.+)_part(?P<num>\d+)$")


def _run(cmd, timeout=None, out_path=None):
    log.info("combine: %s", " ".join(cmd))
    try:
        # encoding pinned: ffmpeg echoes file paths as UTF-8; the default
        # locale codepage (cp1252) raises UnicodeDecodeError on non-ASCII
        # recording names and failed the whole merge.
        res = subprocess.run(cmd, capture_output=True, text=True,
                             encoding="utf-8", errors="replace",
                             timeout=timeout,
                             creationflags=CREATE_NO_WINDOW,
                             startupinfo=_startupinfo())
    except subprocess.TimeoutExpired:
        # subprocess.run kills the child before raising; drop the partial file.
        log.error("combine timed out after %ss, ffmpeg killed: %s",
                  timeout, out_path)
        if out_path:
            try:
                os.remove(out_path)
            except OSError:
                pass
        raise RuntimeError(
            f"ffmpeg did not finish within {timeout} seconds and was stopped. "
            "The incomplete output file was removed; the original recordings "
            "are untouched.")
    except Exception as e:
        log.exception("combine exception: %s", e)
        return False, str(e)
    if res.returncode != 0:
        tail = (res.stderr or "")[-1500:]
        log.error("combine failed (rc=%s):\n%s", res.returncode, tail)
        return False, tail
    log.info("combine OK")
    return True, (res.stderr or "")[-400:]


def _probe_media(path):
    """Read duration / resolution / fps / audio channels of a media file.

    No ffprobe is bundled, so this parses the stderr banner of
    `ffmpeg -i <file>` ("Duration: HH:MM:SS.cc", "Video: ... 1920x1080 ...
    30 fps", "Audio: ... stereo"). Missing values come back as None.
    """
    info = {"duration": None, "width": None, "height": None,
            "fps": None, "channels": None}
    try:
        res = subprocess.run([ffmpeg_tools.ffmpeg_exe(), "-hide_banner",
                              "-i", path],
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=30,
                             creationflags=CREATE_NO_WINDOW,
                             startupinfo=_startupinfo())
        text = res.stderr or ""
    except Exception as e:
        log.warning("probe failed for %s: %s", path, e)
        return info
    m = _DUR_RE.search(text)
    if m:
        info["duration"] = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                            + float(m.group(3)))
    for line in text.splitlines():
        if "Stream" not in line:
            continue
        if "Video:" in line and info["width"] is None:
            rm = _RES_RE.search(line)
            if rm:
                info["width"] = int(rm.group(1))
                info["height"] = int(rm.group(2))
            fm = _FPS_RE.search(line)
            if fm:
                info["fps"] = float(fm.group(1))
        elif "Audio:" in line and info["channels"] is None:
            info["channels"] = _parse_channels(line)
    return info


def _parse_channels(stream_line):
    """Channel count from an ffmpeg 'Audio:' stream line, or None."""
    seg = stream_line.split("Audio:", 1)[1]
    for part in seg.split(","):
        p = part.strip().lower()
        if p == "mono" or p.startswith("mono "):
            return 1
        if p == "stereo" or p.startswith("stereo"):
            return 2
        m = _CHANNELS_RE.match(p)
        if m:
            return int(m.group(1))
        m = re.match(r"(\d)\.(\d)\b", p)  # layouts like 5.1, 7.1(wide)
        if m:
            return int(m.group(1)) + int(m.group(2))
        if p.startswith("quad"):
            return 4
    return None


def _timeout_for(paths):
    """Generous run timeout: 4x the total input duration, floor _MIN_TIMEOUT."""
    total = 0.0
    for p in paths:
        d = _probe_media(p)["duration"]
        if d:
            total += d
    return max(_MIN_TIMEOUT, int(4 * total))


def _group_parts(paths):
    """Group a file list into logical tracks, folding 4 GiB rollover segments.

    safewav rolls long recordings over to `name_part2.wav`, `name_part3.wav`,
    ... next to `name.wav`. Those are *sequential pieces of one track*, not
    parallel tracks, so consumers must concatenate them end to end rather than
    mixing them on top of each other. Returns a list of groups; each group is
    the files of one logical track in play order. Group order follows the
    first appearance of each track in `paths`. Works for any extension (video
    rollover would group the same way if it ever existed).
    """
    groups, order = {}, []
    for p in paths:
        stem, ext = os.path.splitext(os.path.basename(p))
        m = _PART_RE.match(stem)
        if m and int(m.group("num")) >= 2:
            base, num = m.group("base"), int(m.group("num"))
        else:
            base, num = stem, 1
        key = os.path.normcase(os.path.join(os.path.dirname(p), base + ext))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((num, p))
    return [[p for _, p in sorted(groups[k])] for k in order]


def _concat_group_pads(idx_groups, filt, tag):
    """Emit concat filters joining each multi-part group end to end.

    `idx_groups` is a list of groups of ffmpeg input indexes. Returns one
    filtergraph audio pad per logical track ("[3:a]" for plain inputs, a
    concat label for rollover groups), appending filter lines to `filt`.
    """
    pads = []
    for gi, g in enumerate(idx_groups):
        if len(g) == 1:
            pads.append(f"[{g[0]}:a]")
        else:
            ins = "".join(f"[{i}:a]" for i in g)
            lbl = f"{tag}{gi}"
            filt.append(f"{ins}concat=n={len(g)}:v=0:a=1[{lbl}]")
            pads.append(f"[{lbl}]")
    return pads


def _equalize_pads(pads, groups, filt, tag):
    """Pad/trim each logical track to the longest one before amerge.

    amerge stops at its *shortest* input, which would truncate a track made
    of concatenated rollover parts when merged with a shorter one. Returns
    (pads, total_known_duration); pads are left untouched when any duration
    is unknown or there is nothing to align.
    """
    durs, total = [], 0.0
    for g in groups:
        gd = 0.0
        for p in g:
            d = _probe_media(p)["duration"]
            if d is None:
                gd = None
                break
            gd += d
        if gd is not None:
            total += gd
        durs.append(gd)
    if len(pads) < 2 or any(d is None for d in durs):
        return pads, total
    target = max(durs)
    out = []
    for i, pad in enumerate(pads):
        lbl = f"{tag}{i}"
        filt.append(f"{pad}apad,atrim=duration={target:.3f}[{lbl}]")
        out.append(f"[{lbl}]")
    return out, total


def _flatten_groups(groups, start=0):
    """Flatten part groups to (ordered file list, input-index groups)."""
    files, idx_groups = [], []
    i = start
    for g in groups:
        idx_groups.append(list(range(i, i + len(g))))
        files.extend(g)
        i += len(g)
    return files, idx_groups


def combine_av(video_path, audio_paths, out_path, audio_mode="mix"):
    """Mux a video with one or more audio files into out_path.

    audio_mode:
      "mix"      -> all audio summed into a single stereo track (default).
      "tracks"   -> each audio kept as its own selectable track in the file.
    Video is stream-copied (no re-encode) for speed and quality. Rollover
    parts (name_part2.wav, ...) are joined end to end into their base track.
    """
    if not os.path.isfile(video_path):
        return False, f"video not found: {video_path}"
    audio_paths = [a for a in audio_paths if a and os.path.isfile(a)]
    if not audio_paths:
        return False, "no audio inputs"

    groups = _group_parts(audio_paths)
    files, idx_groups = _flatten_groups(groups, start=1)  # input 0 = video

    ff = ffmpeg_tools.ffmpeg_exe()
    cmd = [ff, "-hide_banner", "-y", "-i", video_path]
    for a in files:
        cmd += ["-i", a]

    n = len(groups)
    filt = []
    pads = _concat_group_pads(idx_groups, filt, "ga")
    if audio_mode == "tracks":
        cmd += ["-map", "0:v:0"]
        if filt:
            cmd += ["-filter_complex", ";".join(filt)]
        for g, pad in zip(idx_groups, pads):
            cmd += ["-map", (f"{g[0]}:a:0" if len(g) == 1 else pad)]
        cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "256k", out_path]
    elif n > 1:  # mix
        filt.append(f"{''.join(pads)}amix=inputs={n}:normalize=0[aout]")
        cmd += ["-filter_complex", ";".join(filt),
                "-map", "0:v:0", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "256k", out_path]
    elif filt:  # single logical track made of rollover parts
        cmd += ["-filter_complex", ";".join(filt),
                "-map", "0:v:0", "-map", pads[0],
                "-c:v", "copy", "-c:a", "aac", "-b:a", "256k", out_path]
    else:  # single plain audio file
        cmd += ["-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "256k", out_path]
    return _run(cmd, timeout=_timeout_for([video_path] + files),
                out_path=out_path)


def merge_audio_to_channels(audio_paths, out_path):
    """Merge N (mono) WAVs into one N-channel WAV. Great for Audacity editing.

    Rollover parts of the same track are first joined end to end, so each
    output channel group is one complete logical track.
    """
    audio_paths = [a for a in audio_paths if a and os.path.isfile(a)]
    if len(audio_paths) < 2:
        return False, "need at least two audio files to merge"
    groups = _group_parts(audio_paths)
    if len(groups) < 2:
        return False, ("need at least two audio tracks to merge "
                       "(these files are rollover parts of a single track)")
    files, idx_groups = _flatten_groups(groups)

    ff = ffmpeg_tools.ffmpeg_exe()
    cmd = [ff, "-hide_banner", "-y"]
    for a in files:
        cmd += ["-i", a]
    filt = []
    pads = _concat_group_pads(idx_groups, filt, "gm")
    pads, total = _equalize_pads(pads, groups, filt, "gp")
    filt.append(f"{''.join(pads)}amerge=inputs={len(pads)}[aout]")
    cmd += ["-filter_complex", ";".join(filt), "-map", "[aout]", out_path]
    return _run(cmd, timeout=max(_MIN_TIMEOUT, int(4 * total)),
                out_path=out_path)


def mix_audio_to_stereo(audio_paths, out_path):
    """Sum N WAVs into a single stereo mix.

    Rollover parts of the same track are joined end to end (not overlaid),
    then the logical tracks are mixed together.
    """
    audio_paths = [a for a in audio_paths if a and os.path.isfile(a)]
    if not audio_paths:
        return False, "no audio inputs"
    groups = _group_parts(audio_paths)
    files, idx_groups = _flatten_groups(groups)

    ff = ffmpeg_tools.ffmpeg_exe()
    cmd = [ff, "-hide_banner", "-y"]
    for a in files:
        cmd += ["-i", a]
    filt = []
    pads = _concat_group_pads(idx_groups, filt, "gs")
    if len(pads) == 1 and not filt:
        cmd += ["-ac", "2", out_path]
    elif len(pads) == 1:
        cmd += ["-filter_complex", ";".join(filt),
                "-map", pads[0], "-ac", "2", out_path]
    else:
        filt.append(f"{''.join(pads)}amix=inputs={len(pads)}:normalize=0[aout]")
        cmd += ["-filter_complex", ";".join(filt),
                "-map", "[aout]", "-ac", "2", out_path]
    return _run(cmd, timeout=_timeout_for(files), out_path=out_path)


# Output formats offered by the Convert dialog. Maps a friendly label to
# (extension, has_video, audio_codec, extra ffmpeg args). Audio-only formats
# drop any video; video formats re-encode/copy as needed.
CONVERT_FORMATS = {
    "MP4 (H.264 + AAC)":      ("mp4", True,  "aac",      ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-movflags", "+faststart"]),
    "MKV (H.264 + AAC)":      ("mkv", True,  "aac",      ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]),
    "WebM (VP9 + Opus)":      ("webm", True, "libopus",  ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "32"]),
    "MOV (H.264 + AAC)":      ("mov", True,  "aac",      ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-movflags", "+faststart"]),
    "MP3 (audio only)":       ("mp3", False, "libmp3lame", ["-b:a", "256k"]),
    "AAC / M4A (audio only)": ("m4a", False, "aac",      ["-b:a", "256k", "-movflags", "+faststart"]),
    "FLAC (audio only)":      ("flac", False, "flac",    []),
    "WAV (audio only)":       ("wav", False, "pcm_s16le", []),
    "Opus (audio only)":      ("opus", False, "libopus", ["-b:a", "192k"]),
}

# How many channels each audio codec can carry in one stream. "tracks" mode on
# an audio-only format amerges every source into one multichannel stream, so
# the total must fit. Unlisted codecs (e.g. pcm_s16le) have no practical cap.
_CODEC_MAX_CHANNELS = {"libmp3lame": 2, "aac": 8, "libopus": 8, "flac": 8}
_CODEC_NAMES = {"libmp3lame": "MP3", "aac": "AAC", "libopus": "Opus",
                "flac": "FLAC"}


def convert(entry, out_path, fmt_label, audio_mode="mix"):
    """Convert a single recording (one library entry) to another format.

    entry: {"audio": [wav,...], "video": path|""}
    fmt_label: a key of CONVERT_FORMATS.
    audio_mode: "mix" (sum all audio into one stereo track) or
                "tracks" (keep each audio source as its own track in the file).
    For audio-only formats the video is ignored. Originals are never modified.
    Rollover parts (name_part2.wav, ...) are joined end to end into their base
    track. Raises ValueError if "tracks" mode would exceed the target codec's
    channel limit (e.g. MP3 holds at most 2 channels).
    """
    spec = CONVERT_FORMATS.get(fmt_label)
    if not spec:
        return False, f"unknown format: {fmt_label}"
    ext, has_video, acodec, vargs = spec

    audio = [a for a in entry.get("audio", []) if a and os.path.isfile(a)]
    video = entry.get("video", "")
    has_v = bool(video) and os.path.isfile(video)
    want_video = has_video and has_v
    if not audio and not want_video:
        return False, "nothing to convert (no audio, and no video for this format)"

    groups = _group_parts(audio)
    a_start = 1 if want_video else 0
    files, idx_groups = _flatten_groups(groups, start=a_start)
    n = len(groups)

    if (audio_mode == "tracks" and not want_video and n > 1
            and acodec in _CODEC_MAX_CHANNELS):
        # amerge packs every track into one stream; check the codec can hold it.
        total_ch = 0
        for g in groups:
            total_ch += _probe_media(g[0])["channels"] or 1
        cap = _CODEC_MAX_CHANNELS[acodec]
        if total_ch > cap:
            name = _CODEC_NAMES.get(acodec, acodec)
            raise ValueError(
                f"{name} can hold at most {cap} channels; this recording has "
                f"{total_ch} audio channels. Choose 'Mix to stereo' or a "
                "different format.")

    ff = ffmpeg_tools.ffmpeg_exe()
    cmd = [ff, "-hide_banner", "-y"]
    all_inputs = []
    if want_video:
        cmd += ["-i", video]
        all_inputs.append(video)
    for a in files:
        cmd += ["-i", a]
    all_inputs += files

    filt = []
    pads = _concat_group_pads(idx_groups, filt, "gc")
    if n > 1 and audio_mode != "tracks":
        filt.append(f"{''.join(pads)}amix=inputs={n}:normalize=0[aout]")

    if want_video:
        cmd += vargs
        if n == 0:
            cmd += ["-map", "0:v:0"]
        elif audio_mode == "tracks":
            cmd += ["-map", "0:v:0"]
            if any(len(g) > 1 for g in idx_groups):
                cmd += ["-filter_complex", ";".join(filt)]
            for g, pad in zip(idx_groups, pads):
                cmd += ["-map", (f"{g[0]}:a:0" if len(g) == 1 else pad)]
            cmd += ["-c:a", acodec, "-b:a", "256k"]
        else:  # mix
            if n > 1:
                cmd += ["-filter_complex", ";".join(filt),
                        "-map", "0:v:0", "-map", "[aout]"]
            elif filt:  # one logical track made of rollover parts
                cmd += ["-filter_complex", ";".join(filt),
                        "-map", "0:v:0", "-map", pads[0]]
            else:
                cmd += ["-map", "0:v:0", "-map", "1:a:0"]
            cmd += ["-c:a", acodec]
            if acodec not in ("flac", "pcm_s16le"):
                cmd += ["-b:a", "256k"]
    else:
        # Audio-only output.
        if n == 0:
            return False, "no audio to convert"
        if audio_mode == "tracks" and n > 1:
            # Merge into one multichannel stream so all sources are preserved.
            eq_pads, _ = _equalize_pads(pads, groups, filt, "ge")
            filt.append(f"{''.join(eq_pads)}amerge=inputs={n}[aout]")
            cmd += ["-filter_complex", ";".join(filt), "-map", "[aout]"]
        elif n > 1:
            cmd += ["-filter_complex", ";".join(filt), "-map", "[aout]"]
        elif filt:  # one logical track made of rollover parts
            cmd += ["-filter_complex", ";".join(filt), "-map", pads[0]]
        else:
            cmd += ["-map", f"{a_start}:a:0"]
        cmd += ["-c:a", acodec] + vargs

    cmd += [out_path]
    return _run(cmd, timeout=_timeout_for(all_inputs), out_path=out_path)


def concat_sessions(sessions, out_path, include_video=False):
    """Join several recording sessions end to end into one file.

    `sessions` is a list of dicts: {"audio": [wav,...], "video": path|""}.
    Within each session the audio tracks are mixed to stereo (rollover parts
    are first joined end to end); sessions are then concatenated in order. If
    include_video is True and every session has a video, the videos are
    concatenated and the mixed audio muxed alongside. Sessions recorded on
    different monitors are scaled/padded to a common canvas, each session's
    audio is padded/trimmed to its video's exact length so A/V stays aligned
    across the joins, and synthesized silence is bounded by the video duration
    so ffmpeg always terminates. Re-encodes (the inputs have different start
    times / codecs), so this is a one-off convenience export; the originals
    are never touched.
    """
    sessions = [s for s in sessions if s and
                ([a for a in s.get("audio", []) if a and os.path.isfile(a)]
                 or (include_video and os.path.isfile(s.get("video", ""))))]
    if not sessions:
        return False, "no usable sessions selected"

    do_video = include_video and all(os.path.isfile(s.get("video", ""))
                                     for s in sessions)

    # Probe everything up front: durations bound silence, drive the A/V
    # alignment and the run timeout; resolutions/fps pick the common canvas.
    seg_audio_files = []   # per session: list of part-groups (file paths)
    seg_video_info = []    # per session: probe dict or None
    total_dur = 0.0
    for s in sessions:
        files = [a for a in s.get("audio", []) if a and os.path.isfile(a)]
        groups = _group_parts(files)
        seg_audio_files.append(groups)
        for a in files:
            d = _probe_media(a)["duration"]
            if d:
                total_dur += d
        v = s.get("video", "")
        need_v_info = do_video or (not files and os.path.isfile(v))
        if need_v_info:
            info = _probe_media(v)
            if not info["duration"]:
                return False, f"could not read video duration: {v}"
            seg_video_info.append(info)
            total_dur += info["duration"]
        else:
            seg_video_info.append(None)

    if do_video:
        widths = [i["width"] for i in seg_video_info if i and i["width"]]
        heights = [i["height"] for i in seg_video_info if i and i["height"]]
        if not widths or not heights:
            return False, "could not read video resolution from the sessions"
        # Common canvas: the largest of each dimension, forced even (yuv420p).
        tw = (max(widths) // 2) * 2
        th = (max(heights) // 2) * 2
        fps = max([i["fps"] for i in seg_video_info if i and i["fps"]] or [0])
        if not fps:
            fps = 30

    ff = ffmpeg_tools.ffmpeg_exe()
    cmd = [ff, "-hide_banner", "-y"]
    idx = 0
    seg_audio_groups = []  # per session: list of groups of input indexes
    seg_video_idx = []     # video input index per session (or None)
    for si, s in enumerate(sessions):
        g_idx = []
        for g in seg_audio_files[si]:
            gi = []
            for a in g:
                cmd += ["-i", a]
                gi.append(idx); idx += 1
            g_idx.append(gi)
        seg_audio_groups.append(g_idx)
        if do_video:
            cmd += ["-i", s["video"]]
            seg_video_idx.append(idx); idx += 1
        else:
            seg_video_idx.append(None)

    filt = []
    seg_audio_labels = []
    for si, g_idx in enumerate(seg_audio_groups):
        lbl = f"sa{si}"
        vinfo = seg_video_info[si]
        # Keep each segment's audio exactly as long as its video so the A/V
        # offsets do not accumulate across the concatenated sessions.
        align = (f",apad,atrim=duration={vinfo['duration']:.3f}"
                 if do_video and vinfo and vinfo["duration"] else "")
        if not g_idx:
            # No audio in this session: synthesize silence bounded by the
            # video's duration (unbounded anullsrc would never end).
            if not (vinfo and vinfo["duration"]):
                return False, ("session has no audio and its video duration "
                               "is unknown; cannot synthesize silence")
            filt.append(f"anullsrc=channel_layout=stereo:sample_rate=48000,"
                        f"atrim=duration={vinfo['duration']:.3f},"
                        f"aformat=sample_rates=48000:channel_layouts=stereo"
                        f"[{lbl}]")
            seg_audio_labels.append(lbl)
            continue
        pads = _concat_group_pads(g_idx, filt, f"sp{si}_")
        if len(pads) == 1:
            filt.append(f"{pads[0]}aformat=sample_rates=48000:"
                        f"channel_layouts=stereo{align}[{lbl}]")
        else:
            filt.append(f"{''.join(pads)}amix=inputs={len(pads)}:normalize=0,"
                        f"aformat=sample_rates=48000:channel_layouts=stereo"
                        f"{align}[{lbl}]")
        seg_audio_labels.append(lbl)

    timeout = max(_MIN_TIMEOUT, int(4 * total_dur))
    if do_video:
        # Concatenate video+audio pairs together on a common canvas.
        vlabels = []
        for si in range(len(sessions)):
            vi = seg_video_idx[si]
            vlbl = f"sv{si}"
            # setpts first: real recordings (fragmented/restarted captures)
            # can start at a non-zero timestamp, and concat needs every
            # segment rebased to 0 or the output stalls after the first one.
            filt.append(
                f"[{vi}:v]setpts=PTS-STARTPTS,"
                f"scale={tw}:{th}:force_original_aspect_ratio=decrease,"
                f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps:g},"
                f"format=yuv420p[{vlbl}]")
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
    return _run(cmd, timeout=timeout, out_path=out_path)
