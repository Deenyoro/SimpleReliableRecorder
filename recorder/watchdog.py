"""Resilience layer.

Two cooperating pieces:

1. HeartbeatWriter (in the GUI process): every ~1s writes a derived status
   snapshot to <session>/heartbeat.json. Deltas (e.g. seconds-since-last-write)
   are computed in-process so the separate watcher can compare them without
   sharing a monotonic clock.

2. The separate watcher process: launched as
   `SimpleReliableRecorder.exe --watchdog <session_dir> <gui_pid> <stale> <sound>`.
   It tails heartbeat.json and, if recording should be happening but a subsystem
   has gone quiet (or the whole GUI has hung), it writes <session>/ALERT.json
   (which the GUI polls to raise the gold banner) AND shows an OS message box +
   sound itself — so even a fully-hung GUI cannot hide a recording failure.
"""

import json
import os
import subprocess
import sys
import threading
import time

from . import alerts, paths
from .ffmpeg_tools import CREATE_NO_WINDOW, _startupinfo
from .logging_setup import get_logger

log = get_logger("watchdog")

HEARTBEAT_FILE = "heartbeat.json"
ALERT_FILE = "ALERT.json"
STOP_FILE = "watchdog.stop"


# --------------------------------------------------------------------------- #
# GUI-side heartbeat writer
# --------------------------------------------------------------------------- #
class HeartbeatWriter:
    def __init__(self, session_dir, status_fn, interval=1.0):
        self.session_dir = session_dir
        self.status_fn = status_fn  # callable -> dict
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        os.makedirs(self.session_dir, exist_ok=True)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="heartbeat",
                                        daemon=True)
        self._thread.start()
        log.info("Heartbeat writer started -> %s",
                 os.path.join(self.session_dir, HEARTBEAT_FILE))

    def _loop(self):
        path = os.path.join(self.session_dir, HEARTBEAT_FILE)
        while not self._stop.is_set():
            try:
                payload = self.status_fn()
                payload["wall_time"] = time.time()
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh)
                os.replace(tmp, path)
            except Exception as e:
                log.debug("heartbeat write failed: %s", e)
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        log.info("Heartbeat writer stopped.")


def write_stop_flag(session_dir):
    try:
        with open(os.path.join(session_dir, STOP_FILE), "w") as fh:
            fh.write("stop")
    except Exception:
        pass


def read_alert(session_dir):
    path = os.path.join(session_dir, ALERT_FILE)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def clear_alert(session_dir):
    try:
        os.remove(os.path.join(session_dir, ALERT_FILE))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Spawning the separate watcher
# --------------------------------------------------------------------------- #
def spawn_watchdog(session_dir, gui_pid, stale_seconds=6, alert_sound=True,
                   show_messagebox=True):
    """Launch the watcher as a child of the same executable. Returns Popen."""
    os.makedirs(session_dir, exist_ok=True)
    # remove any stale stop flag
    try:
        os.remove(os.path.join(session_dir, STOP_FILE))
    except Exception:
        pass

    args = ["--watchdog", session_dir, str(gui_pid), str(stale_seconds),
            "1" if alert_sound else "0", "1" if show_messagebox else "0"]
    if paths.is_frozen():
        cmd = [sys.executable] + args
    else:
        main_py = os.path.join(paths.exe_dir(), "main.py")
        cmd = [sys.executable, main_py] + args
    try:
        proc = subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW,
                                startupinfo=_startupinfo())
        log.info("Spawned watchdog process pid=%s: %s", proc.pid, " ".join(cmd))
        return proc
    except Exception as e:
        log.exception("Failed to spawn watchdog: %s", e)
        return None


# --------------------------------------------------------------------------- #
# The watcher process entry point
# --------------------------------------------------------------------------- #
def watchdog_main(argv):
    """argv = [session_dir, gui_pid, stale_seconds, alert_sound, show_messagebox]"""
    session_dir = argv[0]
    gui_pid = int(argv[1]) if len(argv) > 1 else 0
    stale = float(argv[2]) if len(argv) > 2 else 6.0
    alert_sound = (argv[3] == "1") if len(argv) > 3 else True
    show_messagebox = (argv[4] == "1") if len(argv) > 4 else True

    log.info("WATCHDOG started: session=%s gui_pid=%s stale=%.1fs sound=%s box=%s",
             session_dir, gui_pid, stale, alert_sound, show_messagebox)

    hb_path = os.path.join(session_dir, HEARTBEAT_FILE)
    stop_path = os.path.join(session_dir, STOP_FILE)

    prev_screen_size = -1
    screen_stall_count = 0
    last_alert_reason = None
    box_open = threading.Event()

    def gui_alive():
        if not gui_pid:
            return True
        try:
            import psutil
            return psutil.pid_exists(gui_pid)
        except Exception:
            # Fallback: assume alive (avoid false alarms without psutil).
            return True

    def raise_alert(reason):
        nonlocal last_alert_reason
        log.error("WATCHDOG ALERT: %s", reason)
        try:
            with open(os.path.join(session_dir, ALERT_FILE) + ".tmp", "w",
                      encoding="utf-8") as fh:
                json.dump({"reason": reason, "time": time.time()}, fh)
            os.replace(os.path.join(session_dir, ALERT_FILE) + ".tmp",
                       os.path.join(session_dir, ALERT_FILE))
        except Exception:
            pass
        if reason == last_alert_reason:
            return
        last_alert_reason = reason
        if alert_sound:
            alerts.beep(loop=False)
        if show_messagebox and not box_open.is_set():
            box_open.set()

            def _box():
                alerts.message_box(
                    "SimpleReliableRecorder — RECORDING STOPPED",
                    reason + "\n\nRecording may have stopped. Check the app and "
                    "restart recording immediately.")
                box_open.clear()
            threading.Thread(target=_box, daemon=True).start()

    grace_until = time.time() + 4  # give the GUI a moment to write the first heartbeat
    while True:
        if os.path.isfile(stop_path):
            log.info("WATCHDOG stop flag seen; exiting.")
            break
        if not gui_alive():
            raise_alert("The recorder application is no longer running (process gone).")
            time.sleep(1.0)
            # keep watching in case it's a transient read
            if not gui_alive():
                break
            continue

        now = time.time()
        hb = None
        try:
            if os.path.isfile(hb_path):
                with open(hb_path, "r", encoding="utf-8") as fh:
                    hb = json.load(fh)
        except Exception:
            hb = None

        if hb is None:
            if now > grace_until:
                # No heartbeat at all after grace: only alert if GUI claims to record.
                pass
            time.sleep(1.0)
            continue

        recording = hb.get("recording", False)
        wall = hb.get("wall_time", 0)
        # The GUI flags the first few seconds after pressing record as a startup
        # grace window. Never raise a subsystem-stopped alert during it: streams
        # and encoders are still spinning up and the first disk write may not have
        # landed yet. Defense in depth alongside the GUI's own grace handling.
        warming_up = bool(hb.get("startup_grace", False))

        if recording:
            if now - wall > stale:
                raise_alert(f"App not responding — no heartbeat for "
                            f"{now - wall:.0f}s while recording.")
            elif warming_up:
                # Still spinning up; clear any prior alert and wait.
                _clear_if_set(session_dir, last_alert_reason)
                last_alert_reason = None
            elif not hb.get("audio_ok", True):
                raise_alert("AUDIO recording stopped (no audio written recently). "
                            + (hb.get("audio_detail") or ""))
            elif hb.get("screen_enabled") and not hb.get("screen_alive", False):
                raise_alert("SCREEN recording stopped (encoder process exited).")
            elif hb.get("screen_enabled"):
                size = hb.get("screen_size", 0)
                if size == prev_screen_size and size >= 0:
                    screen_stall_count += 1
                else:
                    screen_stall_count = 0
                prev_screen_size = size
                if screen_stall_count >= max(3, int(stale)):
                    raise_alert("SCREEN recording stalled (file not growing).")
                else:
                    _clear_if_set(session_dir, last_alert_reason)
                    last_alert_reason = None
            else:
                _clear_if_set(session_dir, last_alert_reason)
                last_alert_reason = None
        else:
            # Not recording: clear any stale alert.
            _clear_if_set(session_dir, last_alert_reason)
            last_alert_reason = None
            prev_screen_size = -1
            screen_stall_count = 0

        time.sleep(1.0)

    log.info("WATCHDOG exiting.")


def _clear_if_set(session_dir, last_reason):
    if last_reason is not None:
        clear_alert(session_dir)
