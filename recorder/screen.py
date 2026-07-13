"""Screen recording subsystem, fully independent of the audio engine.

Runs ffmpeg in its own process (gdigrab on Windows, avfoundation on macOS,
x11grab on Linux). Because it's a separate process it can never block or stop
the audio threads, and OBS can capture the same screen at the same time. Records
silent video; audio is muxed in later by the Combine step. Supports NVENC / QSV /
AMF / VideoToolbox / CPU with automatic fallback.
"""

import os
import re
import signal
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
            # Numeric avfoundation indices count cameras first (0 is usually
            # the FaceTime camera), which would silently record the webcam.
            # ffmpeg also matches -i by device NAME, so use the screen's name.
            scr = max(0, m["number"] - 1)
            return [
                "-f", "avfoundation",
                "-framerate", str(self.framerate),
                "-capture_cursor", "1",
                "-i", f"{self._avfoundation_screen(scr)}:none",
            ]
        # Linux X11
        disp = os.environ.get("DISPLAY", ":0.0")
        return [
            "-f", "x11grab",
            "-framerate", str(self.framerate),
            "-video_size", f'{m["width"]}x{m["height"]}',
            "-i", f'{disp}+{m["x"]},{m["y"]}',
        ]

    @staticmethod
    def _avfoundation_screen(scr_idx):
        """Resolve the avfoundation device for screen scr_idx (0-based).

        Prefer mapping via `-list_devices true` so the exact device name is
        used; fall back to the conventional "Capture screen N" name, which
        ffmpeg also matches.
        """
        name = f"Capture screen {scr_idx}"
        try:
            rc, out = ffmpeg_tools.run_capture(
                ["-f", "avfoundation", "-list_devices", "true", "-i", ""],
                timeout=10)
            screens = re.findall(r"\[\d+\]\s+(Capture screen \d+)", out)
            if screens and scr_idx < len(screens):
                name = screens[scr_idx]
        except Exception as e:
            log.debug("avfoundation device listing failed: %s", e)
        return name

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
        log.info("Screen capture attempt (encoder=%s, capture=%s): %s",
                 family, self.capture_method, " ".join(cmd))
        flags = CREATE_NO_WINDOW
        if sys.platform == "win32":
            # Own process group so stop() can send CTRL_BREAK_EVENT to ffmpeg
            # without hitting our own process.
            flags |= subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, creationflags=flags,
                startupinfo=_startupinfo())
        except Exception as e:
            log.exception("Failed to launch ffmpeg for %s: %s", family, e)
            return False

        self._stderr_tail = []
        self._out_time_us = -1
        # Until the first -progress block lands, age is measured from spawn so
        # an ffmpeg that never produces a frame reads as stalled, not healthy.
        self._progress_time = time.time()
        # Bind each drain thread to ITS process and tail so a fallback respawn
        # never has the old attempt's threads writing into the new attempt.
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, args=(self.proc, self._stderr_tail),
            name="ffmpeg-stderr", daemon=True)
        self._stderr_thread.start()
        self._progress_thread = threading.Thread(
            target=self._drain_progress, args=(self.proc,),
            name="ffmpeg-progress", daemon=True)
        self._progress_thread.start()

        # Confirm the process survives startup. Poll instead of one long sleep
        # so a fast failure doesn't freeze the caller for the full window.
        deadline = time.monotonic() + 1.3
        while self.proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        if self.proc.poll() is not None:
            tail = "".join(self._stderr_tail[-15:])
            log.warning("Encoder %s exited early (rc=%s):\n%s",
                        family, self.proc.returncode, tail)
            self._close_pipes(self.proc)
            return False
        return True

    @staticmethod
    def _close_pipes(proc):
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if pipe:
                    pipe.close()
            except Exception:
                pass

    def _drain_stderr(self, proc, tail):
        # With -nostats, stderr now carries only the banner, stream info, and
        # real warnings/errors. Keep a tail for diagnostics on failure.
        try:
            for line in iter(proc.stderr.readline, b""):
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip()
                tail.append(text + "\n")
                if len(tail) > 200:
                    del tail[:-200]
                low = text.lower()
                if "error" in low or "failed" in low or "unable" in low:
                    log.error("ffmpeg: %s", text)
                else:
                    log.debug("ffmpeg: %s", text)
        except Exception:
            pass

    def _drain_progress(self, proc):
        # Parse ffmpeg's machine-readable -progress stream from stdout. Every
        # block is several key=value lines terminated by "progress=continue".
        # Receiving ANY block means ffmpeg is alive and encoding; out_time_us
        # advancing confirms real forward progress. Works for all encoders.
        try:
            for line in iter(proc.stdout.readline, b""):
                if not line or proc is not self.proc:
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
        # Capture methods to try per encoder. A ddagrab failure must not burn
        # the encoder attempt: retry the SAME encoder with gdigrab before
        # advancing the chain (otherwise a cpu-only chain dies entirely when
        # ddagrab is the broken part).
        methods = [self.capture_method]
        if sys.platform == "win32" and self.capture_method == "ddagrab":
            methods.append("gdigrab")
        for family in chain:
            for method in list(methods):
                self.capture_method = method
                if self._spawn(family):
                    self.active_family = family
                    self.recording = True
                    log.info("Screen recording started (encoder=%s, capture=%s) -> %s",
                             family, method, self._record_path)
                    return family
                if len(methods) > 1 and method == methods[0]:
                    log.warning("%s failed for %s; retrying with %s.",
                                method, family, methods[1])
                    # Don't keep offering the failed method to later encoders.
                    methods.remove(method)
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
        # Seconds since ffmpeg last emitted a -progress block (measured from
        # spawn until the first block arrives, so a stream that never produces
        # a frame keeps aging). -1 ONLY when there is no live process.
        progress_age = ((time.time() - self._progress_time)
                        if (alive and self._progress_time) else -1.0)
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
                    # Escalate gracefully: CTRL_BREAK -> terminate -> kill.
                    if sys.platform == "win32":
                        log.warning("ffmpeg did not exit on 'q'; sending CTRL_BREAK.")
                        try:
                            self.proc.send_signal(signal.CTRL_BREAK_EVENT)
                            self.proc.wait(timeout=4)
                        except Exception:
                            pass
                    if self.proc.poll() is None:
                        log.warning("ffmpeg still running; terminating.")
                        self.proc.terminate()
                        try:
                            self.proc.wait(timeout=4)
                        except subprocess.TimeoutExpired:
                            self.proc.kill()
            # Always wait the process out so the remux below never races the
            # dying process's open handle on the output file.
            try:
                self.proc.wait(timeout=5)
            except Exception:
                pass
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
                                 encoding="utf-8", errors="replace",
                                 creationflags=CREATE_NO_WINDOW,
                                 startupinfo=_startupinfo(), timeout=120)
            if res.returncode == 0 and os.path.isfile(self.final_path):
                # Sanity-check the result before destroying the crash-safe
                # original: a truncated remux can succeed with rc=0.
                src_size = (os.path.getsize(self._record_path)
                            if os.path.isfile(self._record_path) else 0)
                final_size = os.path.getsize(self.final_path)
                if final_size > 0 and final_size >= 0.6 * src_size:
                    try:
                        os.remove(self._record_path)
                    except Exception:
                        pass
                    log.info("Hybrid remux complete -> %s", self.final_path)
                    return True
                log.warning("Hybrid remux output looks suspect (%d bytes vs %d "
                            "source); keeping the fragmented original.",
                            final_size, src_size)
                return False
            log.error("Hybrid remux failed (rc=%s): %s", res.returncode,
                      (res.stderr or "")[-500:])
        except Exception as e:
            log.exception("Hybrid remux exception: %s", e)
        return False
