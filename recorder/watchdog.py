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
   sound itself - so even a fully-hung GUI cannot hide a recording failure.
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
        tmp = path + ".tmp"
        while not self._stop.is_set():
            try:
                payload = self.status_fn()
                payload["wall_time"] = time.time()
                data = json.dumps(payload)
                # Atomic write (tmp + rename) is preferred, but antivirus or the
                # Downloads folder can briefly lock the target and make os.replace
                # raise PermissionError. Retry a couple times, then fall back to a
                # direct overwrite so wall_time never goes stale (a stale heartbeat
                # would make the watchdog falsely think the app has hung).
                wrote = False
                for _ in range(3):
                    try:
                        with open(tmp, "w", encoding="utf-8") as fh:
                            fh.write(data)
                        os.replace(tmp, path)
                        wrote = True
                        break
                    except Exception:
                        time.sleep(0.05)
                if not wrote:
                    try:
                        with open(path, "w", encoding="utf-8") as fh:
                            fh.write(data)
                        wrote = True
                    except Exception as e:
                        log.debug("heartbeat write failed: %s", e)
            except Exception as e:
                log.debug("heartbeat status failed: %s", e)
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
    # Optional trailing arg: the GUI's process create time, so the watcher can
    # tell a reused PID from the real GUI. Older arg counts still work.
    try:
        import psutil
        args.append(str(psutil.Process(gui_pid).create_time()))
    except Exception:
        pass
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
# The watcher process: decision logic + entry point
# --------------------------------------------------------------------------- #
# Seconds the GUI gets at startup before a missing heartbeat counts against it.
HB_GRACE_SECONDS = 4.0


class WatchdogState:
    """Per-session counters for the heartbeat decision logic."""

    def __init__(self):
        self.screen_stall_count = 0
        self.audio_stall_count = 0
        self.hb_missing_polls = 0
        self.hb_stale_polls = 0
        self.last_hb_raw = None
        self.last_change_mono = None


def evaluate_heartbeat(state, raw, hb, mono_now, stale, in_grace):
    """One decision step over the latest heartbeat read (polled at ~1s).

    `raw` is the heartbeat file's bytes (None if missing/unreadable), `hb` the
    parsed dict (None if missing/unparseable), `mono_now` a time.monotonic()
    stamp from THIS process. Returns (action, reason) with action one of
    "alert", "clear", "wait". Pure (no I/O, no clocks of its own) so the
    stall / staleness rules can be exercised directly.
    """
    if hb is None:
        # The GUI process exists but is not reporting. Tolerate transient
        # write races and slow starts; alert once it stays missing for
        # (stale + grace) consecutive polls past the startup grace window.
        state.hb_missing_polls += 1
        if not in_grace and state.hb_missing_polls >= stale + HB_GRACE_SECONDS:
            return "alert", "Recorder is running but not reporting (no heartbeat)."
        return "wait", None
    state.hb_missing_polls = 0

    # Staleness = the heartbeat CONTENT stopped changing, measured on OUR
    # monotonic clock. Comparing wall clocks across processes false-alarms on
    # NTP steps and sleep/resume; unchanged content cannot (every healthy
    # write bumps wall_time, so healthy bytes always differ).
    if raw != state.last_hb_raw:
        state.last_hb_raw = raw
        state.last_change_mono = mono_now

    if not hb.get("recording", False):
        # Not recording: nothing to watch.
        state.screen_stall_count = 0
        state.audio_stall_count = 0
        state.hb_stale_polls = 0
        return "clear", None

    if (state.last_change_mono is not None
            and mono_now - state.last_change_mono > stale):
        state.hb_stale_polls += 1
        if state.hb_stale_polls >= 2:
            # Static reason so the dedup holds and the beep fires only once.
            return "alert", ("App not responding - heartbeat not updating "
                             "while recording.")
        return "wait", None
    state.hb_stale_polls = 0

    # The GUI flags the first few seconds after pressing record as a startup
    # grace window. Never raise a subsystem-stopped alert during it: streams
    # and encoders are still spinning up and the first disk write may not have
    # landed yet. Defense in depth alongside the GUI's own grace handling.
    if hb.get("startup_grace", False):
        state.screen_stall_count = 0
        state.audio_stall_count = 0
        return "clear", None

    if not hb.get("audio_ok", True):
        # Require the failure to persist for two polls, same as screen, so a
        # single missed write can never raise a false alarm.
        state.audio_stall_count += 1
        if state.audio_stall_count >= 2:
            return "alert", ("AUDIO recording stopped (no audio written recently). "
                             + (hb.get("audio_detail") or ""))
        return "wait", None
    state.audio_stall_count = 0

    if hb.get("screen_enabled") and not hb.get("screen_alive", False):
        # The encoder process is gone. Require it to stay gone for a
        # couple of polls so a momentary auto-restart gap is not flagged.
        state.screen_stall_count += 1
        if state.screen_stall_count >= 2:
            return "alert", "SCREEN recording stopped (encoder process exited)."
        return "wait", None
    if hb.get("screen_enabled") and not hb.get("screen_progressing", True):
        # screen_progressing is computed in the GUI from ffmpeg's machine
        # readable -progress stream (out_time advancing) OR the output file
        # growing - both encoder agnostic. Only a genuine stall (neither
        # advancing) gets here, and we still require it to persist.
        state.screen_stall_count += 1
        if state.screen_stall_count >= 2:
            return "alert", "SCREEN recording stalled (no encoder progress)."
        return "wait", None
    state.screen_stall_count = 0
    return "clear", None


def watchdog_main(argv):
    """argv = [session_dir, gui_pid, stale_seconds, alert_sound,
    show_messagebox, gui_create_time?] - trailing args are optional."""
    if not argv:
        log.error("WATCHDOG: missing arguments (need at least session_dir); "
                  "exiting.")
        return 2
    session_dir = argv[0]
    try:
        gui_pid = int(argv[1]) if len(argv) > 1 else 0
        stale = float(argv[2]) if len(argv) > 2 else 6.0
        gui_create_time = float(argv[5]) if len(argv) > 5 else None
    except (TypeError, ValueError):
        log.error("WATCHDOG: bad arguments %r; exiting.", argv)
        return 2
    alert_sound = (argv[3] == "1") if len(argv) > 3 else True
    show_messagebox = (argv[4] == "1") if len(argv) > 4 else True

    log.info("WATCHDOG started: session=%s gui_pid=%s stale=%.1fs sound=%s box=%s",
             session_dir, gui_pid, stale, alert_sound, show_messagebox)

    hb_path = os.path.join(session_dir, HEARTBEAT_FILE)
    stop_path = os.path.join(session_dir, STOP_FILE)

    state = WatchdogState()
    last_alert_reason = None
    box_open = threading.Event()

    def gui_alive():
        if not gui_pid:
            return True
        try:
            import psutil
            if not psutil.pid_exists(gui_pid):
                return False
            if gui_create_time is not None:
                try:
                    # Guard against PID reuse: a different process wearing the
                    # GUI's old PID must still count as "GUI gone".
                    ct = psutil.Process(gui_pid).create_time()
                    if abs(ct - gui_create_time) > 1.0:
                        return False
                except Exception:
                    pass
            return True
        except Exception:
            # Fallback: assume alive (avoid false alarms without psutil).
            return True

    def raise_alert(reason, sync=False):
        nonlocal last_alert_reason
        log.error("WATCHDOG ALERT: %s", reason)
        # ALERT.json first - the GUI banner must never depend on box/sound.
        try:
            with open(os.path.join(session_dir, ALERT_FILE) + ".tmp", "w",
                      encoding="utf-8") as fh:
                json.dump({"reason": reason, "time": time.time()}, fh)
            os.replace(os.path.join(session_dir, ALERT_FILE) + ".tmp",
                       os.path.join(session_dir, ALERT_FILE))
        except Exception:
            pass
        if reason == last_alert_reason and not sync:
            return
        last_alert_reason = reason
        if sync:
            # Last alert before this process exits: sound and box must run on
            # THIS thread, otherwise process teardown destroys the box
            # mid-display and cuts the sound off.
            if alert_sound:
                alerts.beep(loop=False)
            if show_messagebox:
                alerts.message_box(
                    "SimpleReliableRecorder - RECORDING STOPPED",
                    reason + "\n\nRecording may have stopped. Check the app and "
                    "restart recording immediately.")
            elif alert_sound:
                time.sleep(2.5)  # let the async sound finish before exit
            return
        if alert_sound:
            alerts.beep(loop=False)
        if show_messagebox and not box_open.is_set():
            box_open.set()

            def _box():
                alerts.message_box(
                    "SimpleReliableRecorder - RECORDING STOPPED",
                    reason + "\n\nRecording may have stopped. Check the app and "
                    "restart recording immediately.")
                box_open.clear()
            threading.Thread(target=_box, daemon=True).start()

    grace_until = time.monotonic() + HB_GRACE_SECONDS  # first-heartbeat grace
    while True:
        if os.path.isfile(stop_path):
            log.info("WATCHDOG stop flag seen; exiting.")
            break
        if not gui_alive():
            # Re-check once in case it was a transient read before alerting.
            time.sleep(1.0)
            if gui_alive():
                continue
            raise_alert("The recorder application is no longer running "
                        "(process gone).", sync=True)
            break

        mono_now = time.monotonic()
        raw = None
        hb = None
        try:
            if os.path.isfile(hb_path):
                with open(hb_path, "rb") as fh:
                    raw = fh.read()
                hb = json.loads(raw.decode("utf-8"))
        except Exception:
            hb = None

        action, reason = evaluate_heartbeat(
            state, raw, hb, mono_now, stale, mono_now < grace_until)
        if action == "alert":
            raise_alert(reason)
        elif action == "clear":
            _clear_if_set(session_dir, last_alert_reason)
            last_alert_reason = None

        time.sleep(1.0)

    log.info("WATCHDOG exiting.")
    return 0


def _clear_if_set(session_dir, last_reason):
    if last_reason is not None:
        clear_alert(session_dir)
