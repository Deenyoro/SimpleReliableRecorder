"""Screen recording subsystem, fully independent of the audio engine.

Runs ffmpeg in its own process (gdigrab on Windows, avfoundation on macOS,
x11grab on Linux). Because it's a separate process it can never block or stop
the audio threads, and OBS can capture the same screen at the same time. Records
silent video; audio is muxed in later by the Combine step. Supports NVENC / QSV /
AMF / VideoToolbox / CPU with automatic fallback.
"""

import os
import re
import subprocess
import sys
import threading
import time

from . import ffmpeg_tools
from .ffmpeg_tools import CREATE_NO_WINDOW, _startupinfo
from .logging_setup import get_logger

log = get_logger("screen")

# Keys from ffmpeg's machine-readable -progress output (key=value per line).
# out_time_us is the encoded timestamp in microseconds; it advances on every
# progress tick while encoding is healthy, for every encoder. frame is a useful
# secondary counter. Each block ends with "progress=continue" (or "end").
_PROG_RE = re.compile(r"^(frame|out_time_us|out_time_ms|total_size|progress)=(.+)$")


# --------------------------------------------------------------------------- #
# Monitor enumeration + identify overlay
# --------------------------------------------------------------------------- #
def list_monitors():
    """Return [{number, x, y, width, height, name, primary}] (1-based numbers)."""
    monitors = []
    try:
        from screeninfo import get_monitors
        for i, m in enumerate(get_monitors(), start=1):
            monitors.append({
                "number": i,
                "x": m.x, "y": m.y,
                "width": m.width, "height": m.height,
                "name": getattr(m, "name", f"Monitor {i}") or f"Monitor {i}",
                "primary": bool(getattr(m, "is_primary", False)),
            })
    except Exception as e:
        log.exception("screeninfo enumeration failed: %s", e)
    if not monitors:
        log.warning("No monitors enumerated; defaulting to a single screen entry.")
    log.info("Monitors: %s", monitors)
    return monitors


def show_identify_overlays(parent, duration_ms=4000):
    """Pop a large number on each monitor so the user can pick one.

    `parent` is the app's Tk root. Returns the monitor list; overlays auto-close.
    """
    import tkinter as tk
    mons = list_monitors()
    overlays = []
    for m in mons:
        try:
            top = tk.Toplevel(parent)
            top.overrideredirect(True)
            top.attributes("-topmost", True)
            try:
                top.attributes("-alpha", 0.85)
            except Exception:
                pass
            w, h = 360, 360
            cx = m["x"] + m["width"] // 2 - w // 2
            cy = m["y"] + m["height"] // 2 - h // 2
            top.geometry(f"{w}x{h}+{cx}+{cy}")
            top.configure(bg="#111111")
            label = tk.Label(top, text=str(m["number"]), fg="#FFD24A", bg="#111111",
                             font=("Segoe UI", 160, "bold"))
            label.pack(expand=True, fill="both")
            sub = tk.Label(
                top,
                text=f'{m["width"]}x{m["height"]}' + ("  (primary)" if m["primary"] else ""),
                fg="#CCCCCC", bg="#111111", font=("Segoe UI", 16))
            sub.pack(pady=(0, 18))
            overlays.append(top)
        except Exception as e:
            log.warning("Failed to show overlay on monitor %s: %s", m.get("number"), e)
    for top in overlays:
        top.after(duration_ms, top.destroy)
    return mons


# --------------------------------------------------------------------------- #
# Screen recorder
# --------------------------------------------------------------------------- #
class ScreenRecorder:
    def __init__(self, monitor, out_path, encoder_family="auto", codec="h264",
                 container="mkv", framerate=30, quality="balanced",
                 capture_method="gdigrab", on_error=None, available=None,
                 reliability="hybrid"):
        self.monitor = monitor  # dict from list_monitors()
        self.final_path = out_path
        self.out_path = out_path
        self.encoder_family = encoder_family
        self.codec = codec
        self.container = container
        self.framerate = int(framerate)
        self.quality = quality
        self.capture_method = capture_method
        self.on_error = on_error
        self.available = available or {"cpu": True}
        # standard | fragmented | hybrid  (MKV is already crash resilient)
        self.reliability = reliability
        self._is_mp4 = container.lower() == "mp4"
        # Hybrid mp4: capture to a fragmented temp, remux to a clean mp4 on stop.
        if self._is_mp4 and reliability == "hybrid":
            self._record_path = out_path + ".recording.mp4"
        else:
            self._record_path = out_path

        self.proc = None
        self.active_family = None
        self._stderr_thread = None
        self._progress_thread = None
        self._stderr_tail = []
        self.recording = False
        # Liveness from ffmpeg's -progress stream. out_time_us advances on every
        # 1s tick while encoding is healthy, for every encoder. _progress_time is
        # when we last received ANY progress block.
        self._frame = 0
        self._out_time_us = -1
        self._progress_time = 0.0

    # -- encoder chain (auto fallback) ------------------------------------ #
    def _encoder_chain(self):
        if self.encoder_family == "auto":
            order = [f for f in ("nvenc", "qsv", "amf", "videotoolbox")
                     if self.available.get(f)]
            order.append("cpu")
            return order
        chain = [self.encoder_family]
        if self.encoder_family != "cpu":
            chain.append("cpu")
        return chain

    # -- per-OS screen input ---------------------------------------------- #
    def _input_args(self):
        m = self.monitor
        if sys.platform == "win32" and self.capture_method == "ddagrab":
            out_idx = max(0, m["number"] - 1)
            return ["-filter_complex",
                    f"ddagrab=output_idx={out_idx}:framerate={self.framerate},"
                    f"hwdownload,format=bgra"]
        if sys.platform == "win32":  # gdigrab (compatible default)
            return [
                "-f", "gdigrab",
                "-framerate", str(self.framerate),
                "-offset_x", str(m["x"]),
                "-offset_y", str(m["y"]),
                "-video_size", f'{m["width"]}x{m["height"]}',
                "-i", "desktop",
            ]
        if sys.platform == "darwin":  # macOS avfoundation
            scr = max(0, m["number"] - 1)
            return [
                "-f", "avfoundation",
                "-framerate", str(self.framerate),
                "-capture_cursor", "1",
                "-i", f"{scr}:none",
            ]
        # Linux X11
        disp = os.environ.get("DISPLAY", ":0.0")
        return [
            "-f", "x11grab",
            "-framerate", str(self.framerate),
            "-video_size", f'{m["width"]}x{m["height"]}',
            "-i", f'{disp}+{m["x"]},{m["y"]}',
        ]

    def _build_cmd(self, family):
        enc = ffmpeg_tools.encoder_name(family, self.codec)
        cmd = [ffmpeg_tools.ffmpeg_exe(), "-hide_banner", "-y"]
        # Machine-readable progress on stdout, on a fixed 1s timer. This is the
        # authoritative liveness signal and works for EVERY encoder (NVENC, QSV,
        # AMF, VideoToolbox, CPU) - unlike the human "frame=" stats on stderr,
        # which hardware encoders emit rarely or not at all. -nostats silences
        # the unreliable stderr stats so stderr carries only real errors.
        cmd += ["-progress", "pipe:1", "-stats_period", "1", "-nostats"]
        cmd += self._input_args()
        cmd += ["-c:v", enc]
        cmd += ffmpeg_tools.quality_flags(family, self.quality)
        cmd += ["-pix_fmt", "yuv420p", "-r", str(self.framerate)]

        # Crash safety. MKV is inherently resilient (recoverable on crash). For
        # MP4, fragmented/hybrid write self-contained fragments so a kill or power
        # loss still leaves a playable file up to the last fragment.
        if self._is_mp4 and self.reliability in ("fragmented", "hybrid"):
            cmd += ["-movflags", "+frag_keyframe+empty_moov+default_base_moof",
                    "-frag_duration", "1000000"]  # ~1s fragments
        cmd += ["-flush_packets", "1"]
        cmd += [self._record_path]
        return cmd

    def _spawn(self, family):
        cmd = self._build_cmd(family)
        log.info("Screen capture attempt (encoder=%s): %s", family, " ".join(cmd))
        try:
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW,
                startupinfo=_startupinfo())
        except Exception as e:
            log.exception("Failed to launch ffmpeg for %s: %s", family, e)
            return False

        self._stderr_tail = []
        self._out_time_us = -1
        self._progress_time = 0.0
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, name="ffmpeg-stderr", daemon=True)
        self._stderr_thread.start()
        self._progress_thread = threading.Thread(
            target=self._drain_progress, name="ffmpeg-progress", daemon=True)
        self._progress_thread.start()

        time.sleep(1.3)
        if self.proc.poll() is not None:
            tail = "".join(self._stderr_tail[-15:])
            log.warning("Encoder %s exited early (rc=%s):\n%s",
                        family, self.proc.returncode, tail)
            return False
        return True

    def _drain_stderr(self):
        # With -nostats, stderr now carries only the banner, stream info, and
        # real warnings/errors. Keep a tail for diagnostics on failure.
        try:
            for line in iter(self.proc.stderr.readline, b""):
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip()
                self._stderr_tail.append(text + "\n")
                if len(self._stderr_tail) > 200:
                    self._stderr_tail = self._stderr_tail[-200:]
                low = text.lower()
                if "error" in low or "failed" in low or "unable" in low:
                    log.error("ffmpeg: %s", text)
                else:
                    log.debug("ffmpeg: %s", text)
        except Exception:
            pass

    def _drain_progress(self):
        # Parse ffmpeg's machine-readable -progress stream from stdout. Every
        # block is several key=value lines terminated by "progress=continue".
        # Receiving ANY block means ffmpeg is alive and encoding; out_time_us
        # advancing confirms real forward progress. Works for all encoders.
        try:
            for line in iter(self.proc.stdout.readline, b""):
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                m = _PROG_RE.match(text)
                if not m:
                    continue
                key, val = m.group(1), m.group(2).strip()
                if key == "frame":
                    try:
                        self._frame = int(val)
                    except Exception:
                        pass
                elif key == "out_time_us":
                    try:
                        self._out_time_us = int(val)
                    except Exception:
                        pass
                elif key == "progress":
                    # End of a progress block - stamp the time we got an update.
                    self._progress_time = time.time()
        except Exception:
            pass

    def start(self):
        os.makedirs(os.path.dirname(self._record_path) or ".", exist_ok=True)
        chain = self._encoder_chain()
        log.info("Screen recorder encoder chain: %s -> %s", self.encoder_family, chain)
        for family in chain:
            if self._spawn(family):
                self.active_family = family
                self.recording = True
                log.info("Screen recording started (encoder=%s) -> %s",
                         family, self._record_path)
                return family
            # On the first failure on Windows, drop ddagrab to gdigrab.
            if sys.platform == "win32" and self.capture_method == "ddagrab":
                log.warning("ddagrab failed; switching to gdigrab.")
                self.capture_method = "gdigrab"
        msg = "All screen encoders failed: " + "".join(self._stderr_tail[-10:])
        if self.on_error:
            self.on_error("screen", msg)
        raise RuntimeError(msg)

    def get_status(self):
        alive = self.proc is not None and self.proc.poll() is None
        size = 0
        try:
            if os.path.isfile(self._record_path):
                size = os.path.getsize(self._record_path)
        except Exception:
            pass
        # Seconds since ffmpeg last emitted a -progress block (-1 = none yet).
        progress_age = ((time.time() - self._progress_time)
                        if self._progress_time else -1.0)
        return {
            "recording": self.recording,
            "alive": alive,
            "size": size,
            "frame": self._frame,
            "out_time_us": self._out_time_us,
            "progress_age": progress_age,
            "encoder": self.active_family,
            "path": self._record_path,
        }

    def stop(self):
        if not self.proc:
            return self.out_path
        self.recording = False
        log.info("Stopping screen recording...")
        try:
            if self.proc.poll() is None:
                try:
                    self.proc.stdin.write(b"q")
                    self.proc.stdin.flush()
                except Exception:
                    pass
                try:
                    self.proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    log.warning("ffmpeg did not exit on 'q'; terminating.")
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
        except Exception as e:
            log.exception("Error stopping screen recorder: %s", e)

        # Hybrid mp4: remux the crash-safe fragmented capture into a clean,
        # maximally compatible standard mp4. Keep the fragmented file if remux
        # fails so nothing is ever lost.
        self.out_path = self._record_path
        if (self._is_mp4 and self.reliability == "hybrid"
                and self._record_path != self.final_path
                and os.path.isfile(self._record_path)
                and os.path.getsize(self._record_path) > 0):
            if self._remux_to_final():
                self.out_path = self.final_path

        sz = os.path.getsize(self.out_path) if os.path.isfile(self.out_path) else 0
        log.info("Screen recording stopped -> %s (%d bytes)", self.out_path, sz)
        return self.out_path

    def _remux_to_final(self):
        """Copy fragmented capture to a standard mp4 (no re-encode)."""
        cmd = [ffmpeg_tools.ffmpeg_exe(), "-hide_banner", "-y",
               "-i", self._record_path, "-c", "copy",
               "-movflags", "+faststart", self.final_path]
        log.info("Hybrid remux: %s", " ".join(cmd))
        try:
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 creationflags=CREATE_NO_WINDOW,
                                 startupinfo=_startupinfo(), timeout=120)
            if res.returncode == 0 and os.path.isfile(self.final_path):
                try:
                    os.remove(self._record_path)
                except Exception:
                    pass
                log.info("Hybrid remux complete -> %s", self.final_path)
                return True
            log.error("Hybrid remux failed (rc=%s): %s", res.returncode,
                      (res.stderr or "")[-500:])
        except Exception as e:
            log.exception("Hybrid remux exception: %s", e)
        return False
