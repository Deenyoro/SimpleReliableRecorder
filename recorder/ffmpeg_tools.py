"""Helpers for locating and driving the bundled ffmpeg.

Handles: console-less subprocess spawning on Windows, encoder probing (NVENC /
Intel QSV / AMD AMF / Apple VideoToolbox / CPU x264), and mapping a chosen
encoder family + codec to the concrete ffmpeg encoder name plus quality flags.
"""

import os
import subprocess
import sys

from . import paths
from .logging_setup import get_logger

log = get_logger("screen")

# Hide the console window when launching ffmpeg from a windowed app.
if sys.platform == "win32":
    CREATE_NO_WINDOW = 0x08000000
else:
    CREATE_NO_WINDOW = 0


def _startupinfo():
    if sys.platform != "win32":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return si


def ffmpeg_exe():
    p = paths.ffmpeg_path()
    if not os.path.isfile(p):
        log.error("ffmpeg not found at %s; screen recording will not work.", p)
    return p


def run_capture(args, timeout=20):
    """Run ffmpeg with extra args and capture combined output (for probing)."""
    cmd = [ffmpeg_exe(), "-hide_banner"] + args
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW, startupinfo=_startupinfo())
        return res.returncode, (res.stdout or "") + (res.stderr or "")
    except Exception as e:
        log.exception("ffmpeg run failed: %s", e)
        return 1, str(e)


# Map (family, codec) -> ffmpeg encoder name
_ENC = {
    ("nvenc", "h264"): "h264_nvenc",
    ("nvenc", "hevc"): "hevc_nvenc",
    ("qsv", "h264"): "h264_qsv",
    ("qsv", "hevc"): "hevc_qsv",
    ("amf", "h264"): "h264_amf",
    ("amf", "hevc"): "hevc_amf",
    ("videotoolbox", "h264"): "h264_videotoolbox",
    ("videotoolbox", "hevc"): "hevc_videotoolbox",
    ("cpu", "h264"): "libx264",
    ("cpu", "hevc"): "libx265",
}

FAMILY_LABELS = {
    "auto": "Auto (best available)",
    "nvenc": "NVIDIA NVENC",
    "qsv": "Intel Quick Sync",
    "amf": "AMD AMF",
    "videotoolbox": "Apple VideoToolbox",
    "cpu": "CPU (x264/x265)",
}


def encoder_name(family, codec):
    return _ENC.get((family, codec), "libx264")


def probe_encoders():
    """Return dict {family: available_bool} by parsing `ffmpeg -encoders`.

    CPU is always available. HW availability here means the encoder is built in;
    whether the hardware is actually present is confirmed at record time via the
    auto fallback chain.
    """
    avail = {"cpu": True, "nvenc": False, "qsv": False, "amf": False,
             "videotoolbox": False}
    rc, out = run_capture(["-encoders"])
    text = out.lower()
    if "h264_nvenc" in text:
        avail["nvenc"] = True
    if "h264_qsv" in text:
        avail["qsv"] = True
    if "h264_amf" in text:
        avail["amf"] = True
    if "h264_videotoolbox" in text:
        avail["videotoolbox"] = True
    log.info("Encoder probe: %s", avail)
    return avail


def quality_flags(family, quality):
    """Return a list of ffmpeg flags for the given encoder family + quality."""
    # cq/crf values: lower = higher quality.
    q = {"high": 18, "balanced": 23, "small": 28}.get(quality, 23)
    if family == "nvenc":
        return ["-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", str(q), "-b:v", "0"]
    if family == "qsv":
        return ["-global_quality", str(q), "-preset", "medium"]
    if family == "amf":
        return ["-rc", "cqp", "-qp_i", str(q), "-qp_p", str(q), "-quality", "balanced"]
    if family == "videotoolbox":
        # videotoolbox quality is 0..100 (higher = better).
        vt_q = {"high": 70, "balanced": 55, "small": 40}.get(quality, 55)
        return ["-q:v", str(vt_q), "-realtime", "1"]
    # cpu
    return ["-preset", "veryfast", "-crf", str(q)]
