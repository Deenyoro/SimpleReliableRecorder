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
import threading
from contextlib import contextmanager

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


# --------------------------------------------------------------------------- #
# Progress: callers that want a percentage wrap the operation in
# report_progress(cb); cb(fraction 0..1) is then called from the worker
# thread as ffmpeg reports how far it has written (-progress pipe:1).
# --------------------------------------------------------------------------- #
_progress_local = threading.local()
_PROGRESS_RE = re.compile(r"^out_time_(?:us|ms)=(\d+)\s*$")


@contextmanager
def report_progress(callback):
    """Within this block, every ffmpeg run on this thread reports progress
    to callback(fraction) when its output length is known."""
    prev = getattr(_progress_local, "cb", None)
    _progress_local.cb = callback
    try:
        yield
    finally:
        _progress_local.cb = prev


# --------------------------------------------------------------------------- #
# Cancel: a job wrapped in cancellable(token) runs its ffmpeg through Popen
# and registers the process on the token, so token.cancel() from the UI
# thread stops it; the unfinished output file is then dropped the same way
# as after a timeout.
# --------------------------------------------------------------------------- #
CANCELLED = "Cancelled - the unfinished file was removed."


class CancelToken:
    """Thread-safe 'stop this job': kills the ffmpeg that is running for it
    (if any) and makes later runs of the same job return at once."""

    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self.cancelled = False

    def cancel(self):
        with self._lock:
            self.cancelled = True
            proc = self._proc
        if proc is not None:
            try:
                proc.kill()
            except OSError:
                log.debug("killing ffmpeg on cancel failed", exc_info=True)

    def _attach(self, proc):
        with self._lock:
            self._proc = proc
            return self.cancelled

    def _detach(self):
        with self._lock:
            self._proc = None


class _Cancelled(subprocess.TimeoutExpired):
    """The user pressed Cancel. A TimeoutExpired so the one cleanup path
    that drops the unfinished output file handles both."""

    def __init__(self, cmd):
        super().__init__(cmd, 0)


@contextmanager
def cancellable(token):
    """Within this block, every ffmpeg run on this thread can be stopped
    with token.cancel()."""
    prev = getattr(_progress_local, "token", None)
    _progress_local.token = token
    try:
        yield token
    finally:
        _progress_local.token = prev


def progress_seconds(line):
    """Seconds written so far from one ffmpeg -progress line, else None.
    (ffmpeg's out_time_ms is in microseconds too, despite the name.)"""
    m = _PROGRESS_RE.match(line.strip())
    return int(m.group(1)) / 1e6 if m else None


def _run_streaming(cmd, timeout, expected, cb, token=None):
    """subprocess.run() look-alike that also feeds ffmpeg's -progress
    output to cb (when given and the length is known) and can be stopped
    through `token`. Raises subprocess.TimeoutExpired like run() does, and
    _Cancelled when the token stopped it."""
    cmd = [cmd[0], "-progress", "pipe:1", "-nostats"] + list(cmd[1:])
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace",
                            creationflags=CREATE_NO_WINDOW,
                            startupinfo=_startupinfo())
    err = []
    reader = threading.Thread(target=lambda: err.append(proc.stderr.read()),
                              daemon=True)
    reader.start()
    if token is not None and token._attach(proc):
        proc.kill()  # cancelled between the check in _run and Popen
    killed = threading.Event()

    def kill():
        killed.set()
        proc.kill()
    timer = threading.Timer(timeout, kill) if timeout else None
    if timer:
        timer.daemon = True
        timer.start()
    last = -1.0
    try:
        for line in proc.stdout:
            if cb is None or not expected:
                continue
            secs = progress_seconds(line)
            if secs is None:
                if not line.startswith("progress=end"):
                    continue
                secs = expected
            frac = max(0.0, min(1.0, secs / expected))
            if frac - last >= 0.005 or frac >= 1.0:
                last = frac
                try:
                    cb(frac)
                except Exception:  # a UI hiccup must not stop the merge
                    log.debug("progress callback failed", exc_info=True)
        proc.wait()
    finally:
        if timer:
            timer.cancel()
        if token is not None:
            token._detach()
        reader.join(timeout=5)
        for pipe in (proc.stdout, proc.stderr):
            try:
                pipe.close()
            except OSError:
                log.debug("closing ffmpeg pipe failed", exc_info=True)
    if token is not None and token.cancelled:
        raise _Cancelled(cmd)
    if killed.is_set():
        raise subprocess.TimeoutExpired(cmd, timeout)
    return subprocess.CompletedProcess(cmd, proc.returncode, "",
                                       err[0] if err else "")


def _run(cmd, timeout=None, out_path=None, expected=None):
    cb = getattr(_progress_local, "cb", None)
    token = getattr(_progress_local, "token", None)
    if token is not None and token.cancelled:
        return False, CANCELLED
    log.info("combine: %s", " ".join(cmd))
    try:
        if token is not None or (cb is not None and expected
                                 and expected > 0):
            res = _run_streaming(cmd, timeout, float(expected or 0), cb,
                                 token)
        else:
            # encoding pinned: ffmpeg echoes file paths as UTF-8; the
            # default locale codepage (cp1252) raises UnicodeDecodeError on
            # non-ASCII recording names and failed the whole merge.
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace",
                                 timeout=timeout,
                                 creationflags=CREATE_NO_WINDOW,
                                 startupinfo=_startupinfo())
    except subprocess.TimeoutExpired as exc:
        # subprocess.run kills the child before raising (and Cancel kills
        # the streaming one); drop the partial file.
        cancelled = isinstance(exc, _Cancelled)
        if cancelled:
            log.info("combine cancelled by the user: %s", out_path)
        else:
            log.error("combine timed out after %ss, ffmpeg killed: %s",
                      timeout, out_path)
        if out_path:
            try:
                os.remove(out_path)
            except OSError:
                pass
        if cancelled:
            return False, CANCELLED
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


def _duration(path):
    return _probe_media(path)["duration"] or 0.0


def _groups_length(groups, durs=None):
    """Output length of logical tracks played side by side: the longest
    track, where each track is its rollover parts end to end."""
    durs = durs or {}
    best = 0.0
    for g in groups:
        best = max(best, sum(durs.get(p) or _duration(p) for p in g))
    return best


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
    durs = {p: _duration(p) for p in [video_path] + files}
    timeout = max(_MIN_TIMEOUT, int(4 * sum(durs.values())))
    expected = max(durs[video_path], _groups_length(groups, durs))
    return _run(cmd, timeout=timeout, out_path=out_path, expected=expected)


def combine_take(video_paths, audio_paths, out_path, audio_mode="mix"):
    """Mux a WHOLE take - which may be SEVERAL video segments - with its audio.

    A mid-recording screen auto-restart splits the screen capture into more
    than one file (name_screen.mkv, name_screen-restart-HHMMSS.mkv, ...) while
    the audio keeps running continuously. The old combine used only the single
    tracked video plus the full audio, so one segment was dropped and the
    sound drifted. This joins the segments end to end (in the given order) into
    one continuous picture and muxes the mixed audio, aligned to the joined
    length.

    `video_paths` must already be in chronological order. A single segment
    takes the fast stream-copy path (combine_av, no re-encode); several
    segments go through concat_sessions, which re-encodes onto one canvas.
    """
    video_paths = [v for v in video_paths if v and os.path.isfile(v)]
    if not video_paths:
        return False, "no video inputs"
    if len(video_paths) == 1:
        return combine_av(video_paths[0], audio_paths, out_path, audio_mode)
    return concat_sessions(
        [{"videos": video_paths, "audio": audio_paths}],
        out_path, include_video=True)

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
                out_path=out_path, expected=total)


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
    durs = {p: _duration(p) for p in files}
    return _run(cmd, timeout=max(_MIN_TIMEOUT, int(4 * sum(durs.values()))),
                out_path=out_path, expected=_groups_length(groups, durs))


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
    durs = {p: _duration(p) for p in all_inputs}
    expected = _groups_length(groups, durs)
    if want_video:
        expected = max(expected, durs.get(video) or 0.0)
    return _run(cmd, timeout=max(_MIN_TIMEOUT, int(4 * sum(durs.values()))),
                out_path=out_path, expected=expected)


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
    def _svids(sess):
        vs = sess.get("videos")
        if vs:
            return [v for v in vs if v and os.path.isfile(v)]
        v = sess.get("video", "")
        return [v] if v and os.path.isfile(v) else []

    sessions = [s for s in sessions if s and
                ([a for a in s.get("audio", []) if a and os.path.isfile(a)]
                 or (include_video and _svids(s)))]
    if not sessions:
        return False, "no usable sessions selected"

    # Every session must have >=1 video for a video concat; a session may have
    # SEVERAL (screen auto-restart split one take's capture into segments).
    do_video = include_video and all(_svids(s) for s in sessions)

    # Probe everything up front: durations bound silence, drive the A/V
    # alignment and the run timeout; resolutions/fps pick the common canvas.
    seg_audio_files = []   # per session: list of part-groups (file paths)
    seg_video_files = []   # per session: ordered list of segment paths
    seg_video_infos = []   # per session: list of probe dicts (parallel)
    seg_video_dur = []     # per session: summed video duration (or 0.0)
    total_dur = 0.0
    out_len = 0.0          # expected output length (drives the % shown)
    for s in sessions:
        files = [a for a in s.get("audio", []) if a and os.path.isfile(a)]
        groups = _group_parts(files)
        seg_audio_files.append(groups)
        adurs = {}
        for a in files:
            d = _probe_media(a)["duration"]
            adurs[a] = d or 0.0
            if d:
                total_dur += d
        vids = _svids(s)
        need_v_info = do_video or (not files and vids)
        infos, vdur = [], 0.0
        if need_v_info:
            for v in vids:
                info = _probe_media(v)
                if not info["duration"]:
                    return False, f"could not read video duration: {v}"
                infos.append(info)
                vdur += info["duration"]
                total_dur += info["duration"]
        seg_video_files.append(vids)
        seg_video_infos.append(infos)
        seg_video_dur.append(vdur)
        out_len += (vdur if (do_video and vdur) or not files
                    else _groups_length(groups, adurs))

    if do_video:
        allw = [i["width"] for infos in seg_video_infos for i in infos
                if i["width"]]
        allh = [i["height"] for infos in seg_video_infos for i in infos
                if i["height"]]
        if not allw or not allh:
            return False, "could not read video resolution from the sessions"
        # Common canvas: the largest of each dimension, forced even (yuv420p).
        tw = (max(allw) // 2) * 2
        th = (max(allh) // 2) * 2
        allfps = [i["fps"] for infos in seg_video_infos for i in infos
                  if i["fps"]]
        fps = max(allfps) if allfps else 30

    ff = ffmpeg_tools.ffmpeg_exe()
    cmd = [ff, "-hide_banner", "-y"]
    idx = 0
    seg_audio_groups = []  # per session: list of groups of input indexes
    seg_video_idxs = []    # per session: list of video input indexes
    for si, s in enumerate(sessions):
        g_idx = []
        for g in seg_audio_files[si]:
            gi = []
            for a in g:
                cmd += ["-i", a]
                gi.append(idx); idx += 1
            g_idx.append(gi)
        seg_audio_groups.append(g_idx)
        vidx = []
        if do_video:
            for v in seg_video_files[si]:
                cmd += ["-i", v]
                vidx.append(idx); idx += 1
        seg_video_idxs.append(vidx)

    filt = []
    seg_audio_labels = []
    for si, g_idx in enumerate(seg_audio_groups):
        lbl = f"sa{si}"
        vdur = seg_video_dur[si]
        # Keep each session's audio exactly as long as its (joined) video so
        # A/V offsets do not accumulate across the concatenated sessions.
        align = (f",apad,atrim=duration={vdur:.3f}"
                 if do_video and vdur else "")
        if not g_idx:
            # No audio in this session: synthesize silence bounded by the
            # video's duration (unbounded anullsrc would never end).
            if not vdur:
                return False, ("session has no audio and its video duration "
                               "is unknown; cannot synthesize silence")
            filt.append(f"anullsrc=channel_layout=stereo:sample_rate=48000,"
                        f"atrim=duration={vdur:.3f},"
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
        # Build one video label per session, first joining that session's own
        # segments (screen-restart split) end to end on the common canvas.
        vlabels = []
        for si in range(len(sessions)):
            parts = []
            for k, vi in enumerate(seg_video_idxs[si]):
                plbl = f"sv{si}_{k}"
                # setpts first: real captures can start at a non-zero
                # timestamp, and concat needs each part rebased to 0.
                filt.append(
                    f"[{vi}:v]setpts=PTS-STARTPTS,"
                    f"scale={tw}:{th}:force_original_aspect_ratio=decrease,"
                    f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps:g},"
                    f"format=yuv420p[{plbl}]")
                parts.append(plbl)
            vlbl = f"sv{si}"
            if len(parts) == 1:
                # rename via a passthrough so the outer concat sees [svN]
                filt.append(f"[{parts[0]}]null[{vlbl}]")
            else:
                filt.append("".join(f"[{p}]" for p in parts)
                            + f"concat=n={len(parts)}:v=1:a=0[{vlbl}]")
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
    return _run(cmd, timeout=timeout, out_path=out_path, expected=out_len)
