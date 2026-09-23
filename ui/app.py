"""Main GUI for SimpleReliableRecorder.

Simple by design: pick devices, balance levels, hit Record. Everything else
(crash-safe writing, resilience, the gold alert, screen capture, combine) hangs
off that core flow.

Threading rule: Tk is only touched from the Tk thread. Slow work (opening
devices, ffmpeg, finalizing, folder scans) runs on worker threads that hand
results back through _safe_after().
"""

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, ttk
from tkinter import font as tkfont

from recorder import (
    alerts,
    combine,
    ffmpeg_tools,
    hotkeys,
    library,
    paths,
    scrivox_bridge,
    tray,
    watchdog,
)
from recorder import screen as screenmod
from recorder.audio import (
    AudioRecorder,
    CaptureSource,
    LevelMonitor,
    default_devices,
    list_devices,
    resolve_selection,
)
from recorder.config import DEFAULTS, ConfigManager
from recorder.logging_setup import get_logger, install_inapp_handler
from recorder.screen import ScreenRecorder, list_monitors
from ui import ux
from ui.widgets import (
    COLORS,
    FONT,
    DeviceRow,
    GoldBanner,
    ScrollFrame,
    SegmentedControl,
    StatusLight,
    ToggleSwitch,
    Tooltip,
    apply_dark_theme,
    set_dark_titlebar,
    ui_scale,
    work_area,
)

log = get_logger("gui")

APP_TITLE = "Simple Reliable Recorder"

# Record button colours per state: (normal bg, hover bg, outline). Idle
# Record gets its own lighter surface and a red outline so the primary
# action stands out from Settings and the toolbar buttons.
_REC_LOOK = {
    "idle": ("#353b46", "#404755", COLORS["red"]),
    "recording": (COLORS["red"], "#f36f6c", COLORS["red"]),
    "busy": (COLORS["panel3"], COLORS["panel3"], COLORS["panel"]),
}


class _TreeSelVar:
    """BooleanVar-like view of one Treeview row's selection, so code (and
    the screenshot harness) that ticked rows through row["var"] keeps
    working with the real multi-select list."""

    def __init__(self, tree, iid):
        self.tree, self.iid = tree, iid

    def get(self):
        try:
            return self.iid in self.tree.selection()
        except tk.TclError:
            return False

    def set(self, on):
        try:
            if on:
                self.tree.selection_add(self.iid)
            else:
                self.tree.selection_remove(self.iid)
        except tk.TclError:
            pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        apply_dark_theme(self)
        self._s = ui_scale(self)
        self.cfg = ConfigManager()
        self._place_window()
        ip = paths.icon_path()
        if ip:
            try:
                self.iconbitmap(ip)
            except tk.TclError:
                pass
        # Build normal + recording (red) window icons. Swapping the window icon
        # is what makes the TASKBAR button turn red while recording on Windows.
        self._icon_normal = None
        self._icon_recording = None
        self._build_window_icons()

        # One enumeration for the whole startup pass (restoring rows and the
        # default mic/playback used to re-enumerate 3-5 times before the
        # window could appear).
        self.inputs, self.outputs = list_devices()
        self.all_devices = self.inputs + self.outputs
        # The ffmpeg encoder probe spawns a process (up to 20 s on a slow or
        # AV-scanned machine). Only screen recording needs it, so it runs in
        # the background and the window paints immediately.
        self.encoders = {"cpu": True}
        self._encoders_ready = threading.Event()
        threading.Thread(target=self._probe_encoders, name="encoder-probe",
                         daemon=True).start()

        # Recordings library: prune entries whose files were moved/deleted, keep
        # the rest so the user can combine past takes without reopening the app.
        # Scanning the save folder for older takes happens in the background
        # (see _startup_background) so a big or network folder can't delay the
        # first paint.
        self._library, _pruned = library.prune(self.cfg.get("recordings") or [])
        if _pruned:
            self.cfg.set("recordings", self._library)
        self._lib_rows = []
        self._lib_iids = {}
        self._lib_meta = {}
        self._lib_meta_busy = False
        sort = str(self.cfg.get("library_sort") or "-created")
        self._lib_sort = (sort.lstrip("-") or "created", sort.startswith("-"))
        self._lib_seq = len(self._library)

        # recording state
        self.audio_rec = None
        self.screen_rec = None
        self.heartbeat = None
        self.wd_proc = None
        self.session_dir = None
        self.recording = False
        self.alerting = False
        self._record_start_mono = 0.0
        self._take_id = 0
        self._restart_inflight = {}
        # CaptureSource lists handed to a worker that is still opening the
        # devices (async start / mid-take restart). Mute and Volume changes
        # made in that window are written straight into them, so the take
        # never runs with a stale mute - that is a privacy promise.
        self._pending_sources = []
        self.last_outputs = {}
        self._restart_cooldown = {}
        self._restart_counts = {}
        self._log_queue = queue.Queue()
        self._log_unseen = 0
        self._device_rows = []
        self.level_monitor = None
        self._save_job = None
        self._combine_busy = False
        # FIFO of pending combine/convert jobs: firing several operations (or
        # converting several ticked recordings) runs them one after another
        # instead of refusing everything after the first.
        self._combine_queue = []
        self._combine_results = []
        self._combine_total = 0
        self._combine_frac = None      # 0..1 once ffmpeg reports progress
        self._combine_t0 = 0.0
        # Output paths promised to queued/running jobs but not on disk yet, so
        # _unique_path can't hand the same name to two queued jobs.
        self._pending_out_paths = set()
        # Worker threads hand their UI callbacks to the Tk thread through this
        # queue (see _safe_after / _pump_ui_calls).
        self._ui_calls = queue.Queue()
        self._transcribe_busy = False
        # Optional Scrivox integration: when no Scrivox install is found, every
        # Scrivox-related control stays hidden (users without it never see it).
        # Detection (registry, PATH, folder sweep) runs in the background.
        self._scrivox_exe = None
        self._scrivox_checked = False
        self._closing = False
        self._quitting = False
        self._close_asking = False     # quit question on screen (latch)
        self._close_retry_job = None   # pending on_close retry while starting
        # Re-entrancy latches: dialogs inside start_recording pump the Tk event
        # loop, so a queued second click / tray / hotkey event could re-enter.
        self._starting = False
        self._finalizing = False
        self._toggle_ts = 0.0
        # Alert bookkeeping (dedup + rate limiting so retry loops can't strobe).
        self._last_alert_reason = ""
        self._last_alert_fx = 0.0
        self._last_wd_reason = ""
        self._last_wd_time = 0.0
        self._poll_err_ts = {}
        self._hotkey_job = None
        self._hotkey_ok = True
        self._hotkey_status_lbl = None
        self._settings_tab = 0
        self._settings_lockables = []
        self._settings_rec_note = None
        self._strip_job = None
        self._last_take_secs = 0.0
        self._normal_geom = None

        self.settings_win = None
        self.tray = None
        self.hotkeys = None
        # _save_settings is a no-op until restore finishes: the var traces fire
        # during UI construction (e.g. _refresh_monitor_list writes monitor_var)
        # while _device_rows is still empty, and a save at that moment would
        # wipe the persisted device selections, gains, and mutes on every
        # launch.
        self._ui_ready = False
        self._unresolved_sources = []
        self._make_vars()
        self._build_ui()
        install_inapp_handler(self._enqueue_log)
        self._restore_from_config()
        self._ui_ready = True
        self._refresh_monitor()
        self._setup_tray()
        self._setup_hotkeys()
        self._install_shortcuts()
        self._poll()
        self._pump_ui_calls()
        self._meter_loop()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind("<Configure>", self._track_geometry, add="+")
        self.after(50, lambda: set_dark_titlebar(self))
        self.after(150, self._startup_background)
        log.info("GUI ready. %d devices.", len(self.all_devices))

    def _place_window(self):
        """Restore the last window size/position when it still fits the
        monitor it was on (secondary monitors included); otherwise size
        proportionally on the primary monitor and start maximized."""
        s = self._s
        g = ux.parse_geometry(self.cfg.get("window_geometry"))
        (left, top, right, bottom), _found = work_area(self, g[2:] + g[:2]
                                                       if g else None)
        pl, pt, pr, pb = work_area(self)[0]  # primary monitor
        pw, ph = pr - pl, pb - pt
        # Small enough for a snapped half of a 1920 screen at 150%, big
        # enough that nothing important is hidden (panes scroll below this).
        self.minsize(min(int(820 * s), pw - 40), min(int(560 * s), ph - 40))
        geom = ux.sane_geometry(self.cfg.get("window_geometry"), pw, ph,
                                bounds=(left, top, right, bottom))
        if geom:
            self.geometry(geom)
            zoom = bool(self.cfg.get("window_zoomed"))
        else:
            w = max(min(pw - 40, int(1000 * s)), int(pw * 0.66))
            h = max(min(ph - 40, int(700 * s)), int(ph * 0.85))
            w, h = min(w, pw), min(h, ph)
            x = pl + max(0, (pw - w) // 2)
            y = pt + max(0, (ph - h) // 3)
            self.geometry(f"{w}x{h}+{x}+{y}")
            zoom = True
        if zoom:
            # Maximize after the window is actually mapped - calling zoomed
            # during __init__ is unreliable on multi-monitor / high-DPI Windows.
            self.after(60, self._maximize)

    def _track_geometry(self, event):
        """Remember the un-maximized size/position so it can be restored."""
        if event.widget is not self:
            return
        try:
            if self.state() == "normal":
                self._normal_geom = self.geometry()
        except tk.TclError:
            pass

    def _probe_encoders(self):
        try:
            enc = ffmpeg_tools.probe_encoders() or {"cpu": True}
        except Exception as e:
            log.warning("Encoder probe failed (%s); using CPU encoding.", e)
            enc = {"cpu": True}
        self.encoders = enc
        self._encoders_ready.set()
        log.info("Video encoders available: %s", enc)

    def _startup_background(self):
        """Slow, disk-bound startup work off the Tk thread: stale session
        cleanup, Scrivox detection and the save-folder scan that back-fills
        older recordings into the library."""
        known = {e.get("out_dir") for e in self._library}
        folder = self.cfg.resolved_save_folder()
        override = self.cfg.get("scrivox_path")

        def work():
            self._cleanup_stale_sessions()
            try:
                exe = scrivox_bridge.find_scrivox(override)
            except Exception as e:
                log.warning("Scrivox detection failed: %s", e)
                exe = None
            try:
                found = library.scan_folder(folder, existing_dirs=known)
            except Exception as e:
                log.warning("Library scan skipped: %s", e)
                found = []
            self._safe_after(lambda: self._apply_scan(found, exe))
        threading.Thread(target=work, name="startup-scan", daemon=True).start()

    def _apply_scan(self, found, exe, announce=False):
        self._scrivox_checked = True
        if not self._transcribe_busy:
            self._scrivox_exe = exe
            if exe:
                log.info("Scrivox detected: %s", exe)
        if found:
            known = {e.get("out_dir") for e in self._library}
            fresh = [e for e in found if e.get("out_dir") not in known]
            if fresh:
                self._library.extend(fresh)
                # Keep the list chronological so back-filled old recordings
                # don't show up above yesterday's takes.
                self._library.sort(key=lambda e: e.get("created") or "")
                self.cfg.set("recordings", self._library)
                log.info("Imported %d existing recording(s) into the library.",
                         len(fresh))
        self._refresh_library()
        if announce:
            n = len(found or [])
            self._set_status_note("Found " + ux.plural(n, "new recording")
                                  if n else "The list is up to date.")

    def _cleanup_stale_sessions(self):
        """Best-effort removal of session dirs left by dead instances (the
        dir name embeds the pid; see start_recording)."""
        import glob as _glob
        import shutil as _shutil
        try:
            import psutil
        except ImportError:
            return
        base = paths.data_dir()
        for d in _glob.glob(os.path.join(base, "session-*")) + [
                os.path.join(base, "session")]:  # legacy shared dir
            if not os.path.isdir(d):
                continue
            pid_part = os.path.basename(d).rpartition("-")[2]
            try:
                if pid_part.isdigit() and psutil.pid_exists(int(pid_part)) \
                        and int(pid_part) != os.getpid():
                    continue  # that instance is alive - leave its dir alone
                _shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass

    # ------------------------------------------------------- tray + hotkeys #
    def _safe_after(self, fn):
        """Schedule fn on the Tk thread, ignoring it if we're shutting down.

        Worker threads (recorder start/stop, combine, tray, hotkeys) never
        call into Tk themselves - a Tk call from another thread can crash or
        deadlock on Windows. They drop the callback into a queue that the Tk
        thread drains every few milliseconds (_pump_ui_calls)."""
        if getattr(self, "_closing", False):
            return
        if threading.current_thread() is threading.main_thread():
            try:
                self.after(0, fn)
                return
            except tk.TclError:
                log.debug("after() failed; queueing the callback instead")
        self._ui_calls.put(fn)

    def _pump_ui_calls(self):
        """Tk thread: run callbacks queued by worker threads. Each one is
        guarded on its own so one failure can't starve the rest."""
        if getattr(self, "_closing", False):
            return
        try:
            for _ in range(50):
                fn = self._ui_calls.get_nowait()
                try:
                    fn()
                except Exception as e:
                    self._poll_err("uicall", e)
        except queue.Empty:
            pass
        if not getattr(self, "_closing", False):
            self.after(40, self._pump_ui_calls)

    def report_callback_exception(self, exc, val, tb):
        """Tk swallows callback exceptions into stderr - which is None in a
        windowed build. Log them, and if a recording is running surface the
        failure loudly instead of letting a broken button look like success."""
        try:
            log.error("UI callback error", exc_info=(exc, val, tb))
        except Exception:
            pass
        if self.recording and not getattr(self, "_closing", False):
            try:
                self._raise_gold_alert(f"Internal UI error: {val}")
            except Exception:
                pass

    def _setup_tray(self):
        if not self.tray_var.get():
            return
        self.tray = tray.TrayIcon(
            on_show=lambda: self._safe_after(self._show_window),
            on_toggle_record=lambda: self._safe_after(self._toggle_record),
            on_quit=lambda: self._safe_after(self.on_close),
            is_recording=lambda: self.recording)
        self.tray.start()

    def _apply_tray_setting(self):
        """Start or stop the tray icon as soon as the setting changes."""
        want = bool(self.tray_var.get())
        if want and self.tray is None:
            self._setup_tray()
            if self.tray is not None and self.recording:
                self.tray.set_recording(True)
        elif not want and self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                log.debug("tray stop failed", exc_info=True)
            self.tray = None

    def _build_window_icons(self):
        """Create the normal and recording (red) window/taskbar icons via PIL."""
        try:
            from PIL import Image, ImageDraw, ImageTk
        except Exception as e:
            log.warning("Window icon images unavailable: %s", e)
            return

        def make(recording):
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            if recording:
                d.ellipse([4, 4, 60, 60], fill=(229, 32, 32, 255),
                          outline=(255, 255, 255, 255), width=3)
                d.ellipse([24, 24, 40, 40], fill=(255, 255, 255, 255))
            else:
                d.ellipse([6, 6, 58, 58], fill=(40, 44, 52, 255),
                          outline=(255, 193, 7, 255), width=5)
                d.ellipse([24, 24, 40, 40], fill=(255, 193, 7, 255))
            return ImageTk.PhotoImage(img)

        try:
            self._icon_normal = make(False)
            self._icon_recording = make(True)
            self.iconphoto(True, self._icon_normal)
        except Exception as e:
            log.warning("Could not set window icon: %s", e)

    def _set_taskbar_recording(self, recording):
        """Swap the window/taskbar icon to the red recording variant."""
        icon = self._icon_recording if recording else self._icon_normal
        if icon is not None:
            try:
                self.iconphoto(True, icon)
            except Exception:
                log.debug("taskbar icon swap failed", exc_info=True)

    def _maximize(self):
        # Prefer the native maximized state; fall back to filling the work area
        # if 'zoomed' is unavailable on this platform/runner.
        try:
            self.state("zoomed")
            return
        except Exception:
            pass
        try:
            self.attributes("-zoomed", True)  # some X11/Tk builds
        except Exception:
            pass

    def _show_window(self):
        try:
            self.deiconify()
            self.state("normal")
            self.lift()
            self.focus_force()
        except Exception:
            pass

    def _setup_hotkeys(self):
        self.hotkeys = hotkeys.HotkeyManager(
            on_mute_change=lambda target, muted:
            self._safe_after(lambda: self._apply_hotkey_mute(target, muted)))
        self._reconfigure_hotkeys()

    def _request_hotkey_reconfig(self, delay=700):
        """Coalesce hotkey setting changes so typing 'f8' doesn't bind 'f'."""
        if self._hotkey_job:
            try:
                self.after_cancel(self._hotkey_job)
            except Exception:
                pass
        self._hotkey_job = self.after(delay, self._reconfigure_hotkeys)

    def _reconfigure_hotkeys(self):
        self._hotkey_job = None
        if not self.hotkeys:
            return
        ok = self.hotkeys.configure(
            enabled=self.ptt_enabled_var.get(),
            hotkey=self.ptt_hotkey_var.get().strip(),
            mode=self.ptt_mode_var.get(),
            target=self.ptt_target_var.get(),
            # Seed toggle mode with the real current mute state so the first
            # press actually flips it instead of being a no-op.
            initial_state=lambda: self._hotkey_target_muted(
                self.ptt_target_var.get()))
        enabled = bool(self.ptt_enabled_var.get())
        key = self.ptt_hotkey_var.get().strip()
        self._hotkey_ok = bool(ok) or not enabled or not key
        if not self._hotkey_ok:
            log.warning("Hotkey '%s' could not be registered - "
                        "push-to-talk is INACTIVE.", key)
            if self.settings_win is None:
                # Never silent: say it where the user is looking.
                self._show_strip(
                    "warn", "Hotkey is off", self._hotkey_problem(key),
                    [("Change key...", lambda: self._open_settings(tab=3))])
        self._update_hotkey_status()

    @staticmethod
    def _hotkey_problem(key):
        if not hotkeys.is_valid_hotkey(key):
            return (f"'{key}' isn't a key name the hotkey can use. Click "
                    "'Set key...' and pick another key.")
        return (f"Couldn't use '{key}' - another app may own it. Try "
                "another key.")

    def _update_hotkey_status(self):
        lbl = self._hotkey_status_lbl
        if lbl is None:
            return
        try:
            if not lbl.winfo_exists():
                self._hotkey_status_lbl = None
                return
            key = self.ptt_hotkey_var.get().strip()
            if not self.ptt_enabled_var.get():
                lbl.configure(text="", style="PanelMuted.TLabel")
            elif not key:
                lbl.configure(text="Choose a key to turn the hotkey on.",
                              style="PanelWarn.TLabel")
            elif not self._hotkey_ok:
                lbl.configure(text=self._hotkey_problem(key),
                              style="PanelError.TLabel")
            else:
                lbl.configure(text=f"Active: {key}",
                              style="PanelMuted.TLabel")
            if lbl.cget("text"):
                lbl.grid()
            else:
                lbl.grid_remove()
        except tk.TclError:
            pass

    def _hotkey_target_muted(self, target):
        """True when every device the hotkey targets is currently muted."""
        any_target = False
        for row in self._device_rows:
            d = row.get_selection()
            if not d:
                continue
            key = f'{d["name"]}|{d["kind"]}'
            is_target = (target == key) if target else (d["kind"] == "input")
            if is_target:
                any_target = True
                if not row.is_muted():
                    return False
        return any_target

    def _apply_hotkey_mute(self, target, muted):
        """Mute/unmute the hotkey's target device(s). Empty target = all mics.

        Hotkey mutes are a transient overlay: in ptt/ptm modes they never
        overwrite a mute the user set by hand (releasing the key only unmutes
        rows the hotkey muted), and _save_settings does not persist them - so
        a recording made next week can't silently start with muted mics
        because PTT was tried once. TOGGLE mode is different: each press is an
        explicit user action, so unmute applies to every target row - without
        this, a hand-muted device could never be unmuted by its own hotkey.
        """
        toggle_mode = self.ptt_mode_var.get() == "toggle"
        for row in self._device_rows:
            d = row.get_selection()
            if not d:
                continue
            key = f'{d["name"]}|{d["kind"]}'
            is_target = (target == key) if target else (d["kind"] == "input")
            if not is_target:
                continue
            if muted:
                if not row.is_muted():
                    row._hotkey_muted = True
                    row.set_muted(True, notify=True)
            else:
                if getattr(row, "_hotkey_muted", False) or (
                        toggle_mode and row.is_muted()):
                    row._hotkey_muted = False
                    row.set_muted(False, notify=True)

    def _populate_ptt_devices(self):
        """Fill the push-to-talk device dropdown. Maps a friendly label to the
        '<name>|<kind>' key stored in config; blank = all microphones."""
        self._ptt_keymap = {"All microphones": ""}
        values = ["All microphones"]
        for label, d in ux.device_labels(self.all_devices):
            key = f'{d["name"]}|{d["kind"]}'
            self._ptt_keymap[label] = key
            values.append(label)
        self.ptt_device_combo["values"] = values
        cur = self.ptt_target_var.get()
        match = next((lbl for lbl, k in self._ptt_keymap.items() if k == cur), None)
        self.ptt_device_combo.set(match or "All microphones")

    def _on_ptt_device_pick(self):
        label = self.ptt_device_combo.get()
        self.ptt_target_var.set(self._ptt_keymap.get(label, ""))

    def _make_vars(self):
        """Create every Tk variable up front so both the main window and the
        Settings window can bind to the same state. Any change autosaves."""
        cfg = self.cfg
        self.live_levels_var = tk.BooleanVar(value=cfg.get("live_levels"))
        self.output_mode = tk.StringVar(value=cfg.get("audio_output_mode"))
        self.subtype = tk.StringVar(value=cfg.get("audio_subtype"))
        self.screen_enabled = tk.BooleanVar(value=cfg.get("screen_enabled"))
        self.monitor_var = tk.StringVar(value=str(cfg.get("screen_monitor")))
        self.encoder_var = tk.StringVar(value=cfg.get("screen_encoder"))
        self.container_var = tk.StringVar(value=cfg.get("screen_container"))
        self.codec_var = tk.StringVar(value=cfg.get("screen_codec"))
        self.fps_var = tk.IntVar(value=int(cfg.get("screen_framerate")))
        self.quality_var = tk.StringVar(value=cfg.get("screen_quality"))
        self.reliability_var = tk.StringVar(value=cfg.get("screen_reliability"))
        self.folder_var = tk.StringVar(value=cfg.resolved_save_folder())
        self.ask_var = tk.BooleanVar(value=cfg.get("ask_every_time"))
        self.on_stop_var = tk.StringVar(value=cfg.get("on_stop_action"))
        self.autorestart_var = tk.BooleanVar(value=cfg.get("auto_restart"))
        self.watchdog_var = tk.BooleanVar(value=cfg.get("watchdog_enabled"))
        self.sound_var = tk.BooleanVar(value=cfg.get("alert_sound"))
        self.banner_var = tk.BooleanVar(value=cfg.get("alert_banner"))
        self.taskbar_var = tk.BooleanVar(value=cfg.get("alert_taskbar_flash"))
        self.msgbox_var = tk.BooleanVar(value=cfg.get("alert_messagebox"))
        self.tray_var = tk.BooleanVar(value=cfg.get("tray_enabled"))
        self.ptt_enabled_var = tk.BooleanVar(value=cfg.get("ptt_enabled"))
        self.ptt_hotkey_var = tk.StringVar(value=cfg.get("ptt_hotkey"))
        self.ptt_target_var = tk.StringVar(value=cfg.get("ptt_target"))
        self.ptt_mode_var = tk.StringVar(value=cfg.get("ptt_mode"))
        self.scrivox_path_var = tk.StringVar(value=cfg.get("scrivox_path"))
        self.log_open_var = tk.BooleanVar(value=bool(cfg.get("log_open")))
        for v in (self.live_levels_var, self.output_mode, self.subtype,
                  self.screen_enabled, self.monitor_var, self.encoder_var,
                  self.container_var, self.codec_var, self.fps_var,
                  self.quality_var, self.reliability_var, self.folder_var,
                  self.ask_var, self.on_stop_var, self.autorestart_var,
                  self.watchdog_var, self.sound_var, self.banner_var,
                  self.taskbar_var, self.msgbox_var, self.tray_var,
                  self.ptt_enabled_var, self.ptt_hotkey_var,
                  self.ptt_target_var, self.ptt_mode_var,
                  self.scrivox_path_var):
            v.trace_add("write", lambda *a: self._save_settings())
        self.live_levels_var.trace_add("write", lambda *a: self._refresh_monitor())
        # The tray starts/stops live instead of "takes effect next launch".
        self.tray_var.trace_add(
            "write", lambda *a: self.after_idle(self._apply_tray_setting))
        # Debounced: rebinding on every keystroke of the hotkey field would
        # briefly register single-character global hotkeys and (in ptt mode)
        # mute the mics the moment the user types the first letter.
        for v in (self.ptt_enabled_var, self.ptt_hotkey_var,
                  self.ptt_target_var, self.ptt_mode_var):
            v.trace_add("write", lambda *a: self._request_hotkey_reconfig())
        for v in (self.screen_enabled, self.output_mode):
            v.trace_add("write", lambda *a: self._restore_status())
        self.screen_enabled.trace_add("write", lambda *a: self._idle_lights())

    def _build_ui(self):
        """Command bar on top (Record, timer, status, where files go), a
        notice strip for results, then Sources | Recordings side by side and
        a collapsible activity log. Grid everywhere so it reflows."""
        outer = ttk.Frame(self, style="TFrame", padding=(16, 12, 16, 12))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)
        self._outer = outer

        bar = ttk.Frame(outer, style="Bar.TFrame", padding=(16, 12))
        bar.grid(row=0, column=0, sticky="ew")
        self._build_record(bar)

        self._build_strip(outer)  # row 1, shown on demand

        paned = ttk.Panedwindow(outer, orient="horizontal")
        paned.grid(row=2, column=0, sticky="nsew", pady=(12, 0))
        self._paned = paned
        left = ttk.Frame(paned, style="TFrame")
        right = ttk.Frame(paned, style="TFrame")
        paned.add(left, weight=4)
        paned.add(right, weight=5)

        # Sources pane: heading + a scroll area that only scrolls when needed.
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        lhead = ttk.Frame(left, style="TFrame")
        lhead.grid(row=0, column=0, sticky="ew", padx=(0, 12))
        ttk.Label(lhead, text="Sources", style="Section.TLabel").pack(
            side="left")
        self.dev_refresh_btn = ttk.Button(lhead, text="Refresh devices",
                                          style="Toolbar.TButton",
                                          command=self._refresh_devices)
        self.dev_refresh_btn.pack(side="right")
        Tooltip(self.dev_refresh_btn,
                "Look again for microphones and speakers you plugged in "
                "or unplugged.")
        left_scroll = ScrollFrame(left)
        left_scroll.grid(row=1, column=0, sticky="nsew", pady=(8, 0),
                         padx=(0, 12))
        self._build_devices(left_scroll.body)
        self._build_screen(left_scroll.body)

        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)
        self._build_library(right)

        logf = ttk.Frame(outer, style="TFrame")
        logf.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self._build_log(logf)

        self.banner = GoldBanner(self, on_ack=self._dismiss_alert,
                                 on_restart=self._restart_recording)
        self.banner.pack_before = outer
        self.after(80, self._init_sash)

    def _init_sash(self):
        """Keep the Sources | Recordings split sensible at every window
        size: proportional, with a floor for each side, and biased toward
        the list in narrow (snapped) windows. Once the user drags the sash,
        their proportion is kept instead."""
        self._sash_ratio = None
        self._paned.bind("<Configure>", self._place_sash, add="+")
        self._paned.bind("<ButtonRelease-1>", self._remember_sash, add="+")
        self._place_sash()

    def _sash_target(self, w):
        s = self._s
        left_min, right_min = int(330 * s), int(380 * s)
        if self._sash_ratio is not None:
            pos = int(w * self._sash_ratio)
        else:
            # Device cards read well from ~400 px; the rest goes to the list.
            pos = min(int(w * 0.42), int(540 * s))
            pos = max(pos, min(int(420 * s), w - int(560 * s)))
        pos = min(pos, w - right_min)
        return max(min(left_min, w // 2), pos)

    def _place_sash(self, _event=None):
        try:
            w = self._paned.winfo_width()
            if w <= 50:
                return
            pos = self._sash_target(w)
            if abs(self._paned.sashpos(0) - pos) > 1:
                self._paned.sashpos(0, pos)
        except tk.TclError:
            pass

    def _remember_sash(self, _event=None):
        try:
            w = self._paned.winfo_width()
            if w > 50:
                self._sash_ratio = self._paned.sashpos(0) / float(w)
        except tk.TclError:
            pass

    def _install_shortcuts(self):
        """Keyboard access: F9 everywhere, Ctrl+, settings, Ctrl+O folder,
        Ctrl+L activity log."""
        self.bind_all("<F9>", self._on_f9)
        self.bind("<Control-comma>", lambda e: self._open_settings())
        self.bind("<Control-o>", lambda e: self._open_folder(
            self.cfg.resolved_save_folder()))
        self.bind("<Control-l>", lambda e: self._toggle_log())

    def _on_f9(self, _event=None):
        # A modal dialog (rename, confirm...) owns the keyboard: F9 must not
        # start or stop a take behind it. Settings is not modal.
        grab = self.grab_current()
        if grab is not None and grab is not self.settings_win:
            return "break"
        self._toggle_record()
        return "break"

    def _section(self, parent, title, pady=(0, 12)):
        """A titled card: small heading, then a panel with 12 px padding."""
        box = ttk.Frame(parent, style="TFrame")
        box.pack(fill="x", pady=pady)
        if title:
            ttk.Label(box, text=title, style="Header.TLabel").pack(
                anchor="w", pady=(0, 6))
        inner = ttk.Frame(box, style="Panel.TFrame", padding=12)
        inner.pack(fill="x")
        return inner

    def _build_devices(self, parent):
        box = ttk.Frame(parent, style="TFrame")
        box.pack(fill="x")
        ttk.Label(box, text="Microphone and system sound",
                  style="Header.TLabel").pack(anchor="w", pady=(0, 6))
        self.rows_frame = ttk.Frame(box, style="TFrame")
        self.rows_frame.pack(fill="x")
        btns = ttk.Frame(box, style="TFrame")
        btns.pack(fill="x", pady=(4, 0))
        self.add_dev_btn = ttk.Button(btns, text="+ Add device",
                                      style="Toolbar.TButton",
                                      command=lambda: self._add_row())

        Tooltip(self.add_dev_btn, "Adds the next device that isn't in the "
                                  "list yet - pick another from its menu.")
        self.add_mic_btn = ttk.Button(btns, text="+ Default mic",
                                      style="Toolbar.TButton",
                                      command=self._add_default_mic)

        self.add_play_btn = ttk.Button(btns, text="+ System sound",
                                       style="Toolbar.TButton",
                                       command=self._add_system_playback)

        Tooltip(self.add_play_btn, "Records what you hear through your "
                                   "speakers or headphones (the other side "
                                   "of a call, videos, ...).")
        self._flow(btns, [self.add_dev_btn, self.add_mic_btn,
                          self.add_play_btn])
        btns2 = ttk.Frame(box, style="TFrame")
        btns2.pack(fill="x", pady=(10, 0))
        meters_toggle = ToggleSwitch(btns2, self.live_levels_var,
                                     text="Show sound levels before recording")
        meters_toggle.pack(side="left")
        Tooltip(meters_toggle, "Keeps the level meters moving while you're "
                               "not recording, so you can check a mic works.")
        self._wrap_label(box, "Tip: speak normally and watch the meter - "
                         "green to yellow is good, red means too loud.",
                         style="Muted.TLabel").pack(fill="x", pady=(8, 0))

    def _build_screen(self, parent):
        box = ttk.Frame(parent, style="TFrame")
        box.pack(fill="x", pady=(16, 0))
        ttk.Label(box, text="Screen (optional)", style="Header.TLabel").pack(
            anchor="w", pady=(0, 6))
        inner = ttk.Frame(box, style="Card.TFrame", padding=(12, 10))
        inner.pack(fill="x")
        self.screen_toggle = ToggleSwitch(inner, self.screen_enabled,
                                          text="Record the screen too",
                                          command=self._toggle_screen)
        self.screen_toggle.pack(anchor="w")

        # Collapsible options panel: only visible when the toggle is on.
        self.screen_opts = ttk.Frame(inner, style="Card.TFrame")
        row = ttk.Frame(self.screen_opts, style="Card.TFrame")
        row.pack(fill="x", pady=(10, 0))
        ttk.Label(row, text="Which screen:", style="Card.TLabel").pack(
            side="left")
        self.monitor_combo = ttk.Combobox(row, textvariable=self.monitor_var,
                                          width=22, state="readonly")
        self.monitor_combo.pack(side="left", padx=8, fill="x", expand=True)
        self.ident_btn = ttk.Button(row, text="Identify screens",
                                    style="Toolbar.TButton",
                                    command=self._identify_screens)
        self.ident_btn.pack(side="left")
        Tooltip(self.ident_btn, "Shows a big number on each screen so you "
                                "can tell which is which.")
        self._refresh_monitor_list()
        ttk.Label(self.screen_opts,
                  text="Video quality and file type are in Settings.",
                  style="CardMuted.TLabel").pack(anchor="w", pady=(8, 0))
        self._toggle_screen()

    def _toggle_screen(self):
        if self.screen_enabled.get():
            self.screen_opts.pack(fill="x")
        else:
            self.screen_opts.pack_forget()

    def _flow(self, frame, items, gap=8, visible=None, gaps=None):
        """Lay buttons out left to right and wrap onto the next line when
        the pane is too narrow (small windows, 150% scaling). `visible(w)`
        hides items; `gaps` maps an item to extra space before it. Returns
        the relayout function so callers can re-run it after a change."""
        gaps = gaps or {}

        def relayout(_e=None):
            shown = [w for w in items if visible is None or visible(w)]
            for w in items:
                if w not in shown:
                    w.place_forget()
            width = frame.winfo_width()
            if width <= 1:
                width = sum(w.winfo_reqwidth() + gap for w in shown)
            x = y = line_h = 0
            for w in shown:
                rw, rh = w.winfo_reqwidth(), w.winfo_reqheight()
                extra = gaps.get(w, 0) if x else 0
                if x and x + extra + rw > width:
                    x, y, line_h, extra = 0, y + line_h + gap, 0, 0
                x += extra
                w.place(x=x, y=y)
                x += rw + gap
                line_h = max(line_h, rh)
            if int(frame.cget("height")) != y + line_h:
                frame.configure(height=y + line_h)
        relayout()
        frame.bind("<Configure>", relayout, add="+")
        return relayout

    def _wrap_label(self, parent, text, style="Muted.TLabel", width=440):
        """A label that wraps to whatever width its container gives it
        (a fixed pixel wraplength clipped text at 150% and left big gaps
        on wide windows)."""
        lbl = ttk.Label(parent, text=text, style=style, justify="left",
                        wraplength=int(width * self._s))
        lbl.bind("<Configure>", lambda e: lbl.configure(
            wraplength=max(120, e.width - 4)))
        return lbl

    def _choice_combo(self, parent, key, var, values=None, width=28):
        """Readonly combobox that shows plain-language labels but reads and
        writes the unchanged config tokens in `var`."""
        pairs = [(v, lbl) for v, lbl in ux.CHOICES[key]
                 if values is None or v in values]
        disp = tk.StringVar(value=ux.choice_label(key, var.get()))
        cb = ttk.Combobox(parent, textvariable=disp, state="readonly",
                          values=[lbl for _, lbl in pairs], width=width)
        cb.bind("<<ComboboxSelected>>",
                lambda e: var.set(ux.choice_value(key, disp.get())))
        trace = var.trace_add(
            "write", lambda *a: disp.set(ux.choice_label(key, var.get())))

        def _untrace(e):
            if e.widget is cb:
                try:
                    var.trace_remove("write", trace)
                except tk.TclError:
                    pass
        cb.bind("<Destroy>", _untrace, add="+")
        cb._srr_disp = disp  # keep the display var alive with the widget
        return cb

    def _open_settings(self, tab=None):
        if self.settings_win is not None and self.settings_win.winfo_exists():
            self.settings_win.deiconify()
            self.settings_win.lift()
            self.settings_win.focus_force()
            if tab is not None:
                try:
                    self._settings_nb.select(tab)
                except tk.TclError:
                    pass
            return
        s = self._s
        win = tk.Toplevel(self)
        win.withdraw()
        self.settings_win = win
        win.title("Settings - " + APP_TITLE)
        win.configure(bg=COLORS["bg"])
        try:
            ip = paths.icon_path()
            if ip:
                win.iconbitmap(ip)
        except tk.TclError:
            pass
        win.transient(self)
        self._settings_lockables = []

        bottom = ttk.Frame(win, style="TFrame")
        bottom.pack(side="bottom", fill="x", padx=16, pady=(8, 16))
        ttk.Button(bottom, text="Open logs folder",
                   command=lambda: self._open_path(paths.logs_dir())).pack(
            side="left")
        ttk.Button(bottom, text="Restore defaults",
                   command=lambda: _restore_tab()).pack(side="left", padx=8)
        ttk.Button(bottom, text="Close", style="Accent.TButton",
                   command=lambda: _on_settings_close()).pack(side="right")

        self._settings_rec_note = ttk.Label(
            win, style="Warn.TLabel", wraplength=int(560 * s),
            text="A recording is running. Changes here apply to the next "
                 "recording; the greyed-out options can't change mid-take.")

        # Tabs instead of one tall scroll: each concern fits on screen and the
        # safety-critical alerts page is findable by name.
        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=16, pady=(16, 0))
        self._settings_nb = nb
        pages, bodies = {}, []
        for name in ("Recording", "Saving", "Safety & alerts",
                     "Hotkey & tray", "Transcription"):
            pg = ScrollFrame(nb)
            nb.add(pg, text=name)
            pg.body.configure(padding=(16, 16, 16, 8))
            pages[name] = pg.body
            bodies.append(pg.body)

        def form_row(parent, r, label, widget_fn):
            ttk.Label(parent, text=label, style="Panel.TLabel").grid(
                row=r, column=0, sticky="w", padx=(0, 12), pady=4)
            w = widget_fn(parent)
            w.grid(row=r, column=1, sticky="ew", pady=4)
            return w

        # --- Recording: audio files ---
        a = self._section(pages["Recording"], "Audio files")
        seg = SegmentedControl(a, self.output_mode, [
            ("separate", "A separate file for each device (safest, default)"),
            ("channels", "One file, each device on its own channel"),
            ("mixed", "One mixed file"),
        ], wraplength=460)
        seg.pack(fill="x")
        self._settings_lockables.append(seg)
        af = ttk.Frame(a, style="Panel.TFrame")
        af.pack(fill="x", pady=(10, 0))
        af.columnconfigure(1, weight=1)
        sub = form_row(af, 0, "Sample format", lambda p: self._choice_combo(
            p, "audio_subtype", self.subtype))
        self._settings_lockables.append(sub)
        ttk.Label(a, text="Audio is saved to disk every ~2 seconds, so a "
                  "crash loses almost nothing.", style="PanelMuted.TLabel",
                  wraplength=int(460 * s), justify="left").pack(
            anchor="w", pady=(6, 0))

        # --- Recording: screen video ---
        v = self._section(pages["Recording"], "Screen video")
        vf = ttk.Frame(v, style="Panel.TFrame")
        vf.pack(fill="x")
        vf.columnconfigure(1, weight=1)
        enc_values = ["auto"] + [f for f in ("nvenc", "qsv", "amf",
                                             "videotoolbox")
                                 if self.encoders.get(f)] + ["cpu"]
        rows = [
            ("Video encoder", lambda p: self._choice_combo(
                p, "screen_encoder", self.encoder_var, values=enc_values)),
            ("File type", lambda p: self._choice_combo(
                p, "screen_container", self.container_var)),
            ("Codec", lambda p: self._choice_combo(
                p, "screen_codec", self.codec_var)),
            ("Frames per second", lambda p: ttk.Spinbox(
                p, from_=5, to=60, width=6, textvariable=self.fps_var)),
            ("Quality", lambda p: self._choice_combo(
                p, "screen_quality", self.quality_var)),
            ("Crash safety (MP4)", lambda p: self._choice_combo(
                p, "screen_reliability", self.reliability_var)),
        ]
        for i, (label, fn) in enumerate(rows):
            w = form_row(vf, i, label, fn)
            if label == "Frames per second":
                w.grid_configure(sticky="w")
            self._settings_lockables.append(w)
        ttk.Label(v, text="MKV files are always crash-safe. For MP4, "
                  "'Maximum' records in crash-safe pieces and turns them "
                  "into a normal MP4 when you stop.",
                  style="PanelMuted.TLabel", wraplength=int(460 * s),
                  justify="left").pack(anchor="w", pady=(8, 0))

        # --- Saving ---
        o = self._section(pages["Saving"], "Where recordings are saved")
        f = ttk.Frame(o, style="Panel.TFrame")
        f.pack(fill="x")
        ttk.Entry(f, textvariable=self.folder_var, width=30).pack(
            side="left", fill="x", expand=True)
        ttk.Button(f, text="Browse...",
                   command=lambda: self._browse_folder(parent=win)).pack(
            side="left", padx=(8, 0))
        ToggleSwitch(o, self.ask_var,
                     text="Ask where to save each recording").pack(
            anchor="w", pady=(10, 0))
        st = self._section(pages["Saving"],
                           "After recording screen and audio together")
        sf = ttk.Frame(st, style="Panel.TFrame")
        sf.pack(fill="x")
        sf.columnconfigure(1, weight=1)
        form_row(sf, 0, "Make one video with sound?",
                 lambda p: self._choice_combo(p, "on_stop_action",
                                              self.on_stop_var, width=24))
        ttk.Label(st, text="Your separate tracks are always kept.",
                  style="PanelMuted.TLabel").pack(anchor="w", pady=(6, 0))

        # --- System tray ---
        tsec = self._section(pages["Hotkey & tray"], "System tray")
        ToggleSwitch(tsec, self.tray_var,
                     text="Show a tray icon (turns red while recording)").pack(
            anchor="w")

        # --- Push to talk / push to mute ---
        psec = self._section(pages["Hotkey & tray"],
                             "Push-to-talk / push-to-mute hotkey")
        ToggleSwitch(psec, self.ptt_enabled_var,
                     text="Use a keyboard shortcut to mute or unmute").pack(
            anchor="w")
        pf = ttk.Frame(psec, style="Panel.TFrame")
        pf.pack(fill="x", pady=(10, 0))
        pf.columnconfigure(1, weight=1)

        def key_field(p):
            fr = ttk.Frame(p, style="Panel.TFrame")
            ttk.Entry(fr, textvariable=self.ptt_hotkey_var, width=14).pack(
                side="left")
            ttk.Button(fr, text="Set key...", style="Toolbar.TButton",
                       command=lambda: self._capture_hotkey(win)).pack(
                side="left", padx=(8, 0))
            return fr
        kf = form_row(pf, 0, "Key", key_field)
        kf.grid_configure(sticky="w")
        self._hotkey_status_lbl = ttk.Label(pf, text="",
                                            style="PanelMuted.TLabel")
        self._hotkey_status_lbl.grid(row=1, column=1, sticky="w",
                                     pady=(0, 4))
        form_row(pf, 2, "What it does", lambda p: self._choice_combo(
            p, "ptt_mode", self.ptt_mode_var, width=28))

        def dev_field(p):
            self.ptt_device_combo = ttk.Combobox(p, width=30, state="readonly")
            return self.ptt_device_combo
        form_row(pf, 3, "Device", dev_field)
        self._populate_ptt_devices()
        self.ptt_device_combo.bind("<<ComboboxSelected>>",
                                   lambda e: self._on_ptt_device_pick())
        ttk.Label(psec, text="Works even when this window is in the "
                  "background. 'All microphones' mutes every mic you record.",
                  style="PanelMuted.TLabel", wraplength=int(460 * s),
                  justify="left").pack(anchor="w", pady=(8, 0))
        self._update_hotkey_status()

        # --- Scrivox transcription (always shown, so the path can be set
        # even when auto-detection finds nothing) ---
        if not self._transcribe_busy:
            self._scrivox_exe = scrivox_bridge.find_scrivox(
                self.cfg.get("scrivox_path"))
            self._scrivox_checked = True
        xsec = self._section(pages["Transcription"], "Scrivox")
        scrivox_status = ttk.Label(xsec, style="PanelMuted.TLabel",
                                   wraplength=int(460 * s), justify="left")
        scrivox_status.pack(anchor="w")
        xpath = ttk.Frame(xsec, style="Panel.TFrame")
        xpath.pack(fill="x", pady=(8, 0))
        ttk.Label(xpath, text="Scrivox location", style="Panel.TLabel").pack(
            side="left")
        ttk.Entry(xpath, textvariable=self.scrivox_path_var, width=28).pack(
            side="left", padx=8, fill="x", expand=True)
        xrow = ttk.Frame(xsec, style="Panel.TFrame")
        xrow.pack(fill="x", pady=(8, 0))
        open_btn = ttk.Button(xrow, text="Open Scrivox",
                              command=lambda: scrivox_bridge.open_scrivox(
                                  self._scrivox_exe))
        scrivox_info = ttk.Label(
            xsec,
            text="Transcription options (model, language, speakers, "
            "API keys, screen-description detail) are configured "
            "inside Scrivox and used automatically here.",
            style="PanelMuted.TLabel", wraplength=int(460 * s), justify="left")

        def _scrivox_update_status():
            # Until Scrivox is actually found, the ONLY Scrivox UI anywhere
            # is this location setting - no dead buttons, no explainer text.
            if self._scrivox_exe:
                scrivox_status.config(
                    text=f"Scrivox found:  {self._scrivox_exe}")
                if not open_btn.winfo_manager():
                    open_btn.pack(side="left", padx=(8, 0))
                if not scrivox_info.winfo_manager():
                    scrivox_info.pack(anchor="w", pady=(8, 0))
            else:
                scrivox_status.config(
                    text="Scrivox isn't installed or wasn't found. Leave the "
                         "location blank to find it automatically, or point "
                         "it at Scrivox.exe (or its folder).")
                open_btn.pack_forget()
                scrivox_info.pack_forget()

        def _scrivox_redetect():
            # The traced var already saved the config; force skips the cache
            # so the new path (or a freshly installed Scrivox) applies now.
            if not self._transcribe_busy:
                self._scrivox_exe = scrivox_bridge.find_scrivox(
                    self.cfg.get("scrivox_path"), force=True)
            _scrivox_update_status()
            self._refresh_library()

        def _scrivox_browse():
            cur = self.scrivox_path_var.get().strip()
            start = (cur if os.path.isdir(cur) else os.path.dirname(cur)) \
                if cur else os.environ.get("ProgramFiles", "")
            p = filedialog.askopenfilename(
                parent=win, title="Locate Scrivox.exe",
                initialdir=start or None,
                filetypes=[("Scrivox", "Scrivox.exe"),
                           ("Programs", "*.exe"), ("All files", "*.*")])
            if p:
                self.scrivox_path_var.set(p)
                _scrivox_redetect()

        ttk.Button(xrow, text="Browse...", command=_scrivox_browse).pack(
            side="left")
        ttk.Button(xrow, text="Check again", command=_scrivox_redetect).pack(
            side="left", padx=(8, 0))
        _scrivox_update_status()

        # --- Resilience ---
        rsec = self._section(pages["Safety & alerts"],
                             "If something stops working")
        ToggleSwitch(rsec, self.autorestart_var,
                     text="Restart a stopped device or screen capture "
                          "automatically").pack(anchor="w", pady=3)
        ToggleSwitch(rsec, self.watchdog_var,
                     text="Run a background watchdog").pack(anchor="w", pady=3)
        ttk.Label(rsec, text="The watchdog is a separate helper that warns "
                  "you even if this window freezes.",
                  style="PanelMuted.TLabel", wraplength=int(460 * s),
                  justify="left").pack(anchor="w", pady=(4, 0))

        # --- Alerts ---
        al = self._section(pages["Safety & alerts"],
                           "Warn me if recording stops")
        for text, var in (("Play a sound", self.sound_var),
                          ("Show a flashing gold bar in this window",
                           self.banner_var),
                          ("Flash the taskbar button", self.taskbar_var),
                          ("Pop up a message from the watchdog",
                           self.msgbox_var)):
            ToggleSwitch(al, var, text=text).pack(anchor="w", pady=3)

        tab_defaults = {
            0: [(self.output_mode, "audio_output_mode"),
                (self.subtype, "audio_subtype"),
                (self.encoder_var, "screen_encoder"),
                (self.container_var, "screen_container"),
                (self.codec_var, "screen_codec"),
                (self.fps_var, "screen_framerate"),
                (self.quality_var, "screen_quality"),
                (self.reliability_var, "screen_reliability")],
            1: [(self.ask_var, "ask_every_time"),
                (self.on_stop_var, "on_stop_action")],
            2: [(self.autorestart_var, "auto_restart"),
                (self.watchdog_var, "watchdog_enabled"),
                (self.sound_var, "alert_sound"),
                (self.banner_var, "alert_banner"),
                (self.taskbar_var, "alert_taskbar_flash"),
                (self.msgbox_var, "alert_messagebox")],
            3: [(self.tray_var, "tray_enabled"),
                (self.ptt_enabled_var, "ptt_enabled"),
                (self.ptt_hotkey_var, "ptt_hotkey"),
                (self.ptt_mode_var, "ptt_mode"),
                (self.ptt_target_var, "ptt_target")],
            4: [(self.scrivox_path_var, "scrivox_path")],
        }

        def _restore_tab():
            i = nb.index(nb.select())
            name = nb.tab(i, "text")
            if i == 0 and self.recording:
                self._notice("Recording in progress",
                             "Recording settings can be reset after you "
                             "stop.", parent=win)
                return
            if not self._confirm(
                    "Restore defaults",
                    f"Put every setting on the '{name}' tab back to how it "
                    "was when the app was installed?",
                    yes="Restore defaults", no="Cancel", parent=win):
                return
            for var, key in tab_defaults.get(i, []):
                var.set(DEFAULTS[key])
            if i == 1:
                self.folder_var.set(paths.default_recordings_dir())
            if i == 3:
                self._populate_ptt_devices()
            if i == 4:
                _scrivox_redetect()

        def _on_settings_close():
            try:
                self._settings_tab = nb.index(nb.select())
            except tk.TclError:
                pass
            self._save_settings()
            # Commit any pending (debounced) hotkey change right away.
            if self._hotkey_job:
                try:
                    self.after_cancel(self._hotkey_job)
                except tk.TclError:
                    pass
            self._hotkey_status_lbl = None
            self.settings_win = None
            self._settings_rec_note = None
            self._settings_lockables = []
            self._reconfigure_hotkeys()
            # Apply a hand-edited Scrivox path without needing the Check
            # button: re-detect (skipping the cache) and refresh the library
            # so the Transcribe button appears/disappears immediately.
            if not self._transcribe_busy:
                self._scrivox_exe = scrivox_bridge.find_scrivox(
                    self.cfg.get("scrivox_path"), force=True)
            self._refresh_library()
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", _on_settings_close)
        win.bind("<Escape>", lambda e: _on_settings_close())

        # Size to the content (not a fixed pixel box that clipped at 150%),
        # clamped to the screen so Close is always reachable.
        win.update_idletasks()
        try:
            # Fit the monitor the main window is on (not half the virtual
            # screen, and not always the primary one).
            (ml, mt, mr, mb), _ = work_area(self, (
                self.winfo_rootx(), self.winfo_rooty(),
                self.winfo_width(), self.winfo_height()))
            need_w = max(b.winfo_reqwidth() for b in bodies) + int(60 * s)
            need_h = (max(b.winfo_reqheight() for b in bodies)
                      + bottom.winfo_reqheight() + int(110 * s))
            w = min(max(need_w, int(600 * s)), mr - ml - 40)
            h = min(max(need_h, int(440 * s)), mb - mt - 60)
            x = self.winfo_rootx() + max(0, (self.winfo_width() - w) // 2)
            y = self.winfo_rooty() + max(0, (self.winfo_height() - h) // 4)
            x = max(ml, min(x, mr - w))
            y = max(mt, min(y, mb - h - 40))
            win.geometry(f"{w}x{h}+{x}+{y}")
            win.minsize(min(int(520 * s), w), min(int(380 * s), h))
        except (tk.TclError, ValueError):
            win.geometry("760x640")
        try:
            nb.select(tab if tab is not None else self._settings_tab)
        except tk.TclError:
            pass
        self._sync_settings_lock()
        win.deiconify()
        set_dark_titlebar(win)
        win.focus_set()

    def _capture_hotkey(self, parent):
        """'Press a key...' dialog: records the next key combination in the
        exact format the global hotkey library expects."""
        win = self._modal_dialog("Set hotkey", parent=parent)
        frm = ttk.Frame(win, style="TFrame", padding=24)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Press the key or key combination to use",
                  style="Section.TLabel").pack(anchor="w")
        shown = ttk.Label(frm, text="Waiting for a key...",
                          style="Header.TLabel")
        shown.pack(anchor="w", pady=(12, 4))
        ttk.Label(frm, text="Function keys (F1-F12) work best. Esc cancels.",
                  style="Muted.TLabel").pack(anchor="w")
        btns = ttk.Frame(frm, style="TFrame")
        btns.pack(fill="x", pady=(16, 0))
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right")

        def on_key(e):
            if e.keysym == "Escape":
                win.destroy()
                return "break"
            combo = ux.hotkey_from_event(e.keysym, int(e.state),
                                         windows=(sys.platform == "win32"))
            if combo and not hotkeys.is_valid_hotkey(combo):
                shown.configure(text=f"'{combo}' can't be used as a "
                                     "hotkey. Try a function key (F1-F12).")
                return "break"
            if combo:
                shown.configure(text=combo)
                self.ptt_hotkey_var.set(combo)
                if not self.ptt_enabled_var.get():
                    self.ptt_enabled_var.set(True)
                win.after(350, win.destroy)
            return "break"
        win.bind("<KeyPress>", on_key)
        self._finish_dialog(win, lambda: None, focus=win,
                            bind_return=False)
        win.wait_window()

    def _sync_settings_lock(self):
        """While recording, grey out the Settings that only apply when a take
        starts and say so, instead of silently ignoring the change."""
        on = not (self.recording or self._starting)
        for w in list(self._settings_lockables):
            try:
                if not w.winfo_exists():
                    continue
                if isinstance(w, SegmentedControl):
                    w.set_enabled(on)
                elif isinstance(w, ttk.Combobox):
                    w.configure(state="readonly" if on else "disabled")
                else:
                    w.state(["!disabled"] if on else ["disabled"])
            except tk.TclError:
                pass
        note = self._settings_rec_note
        if note is not None:
            try:
                if not on:
                    note.pack(fill="x", padx=16, pady=(12, 0),
                              before=self._settings_nb)
                else:
                    note.pack_forget()
            except tk.TclError:
                pass

    # Kept for callers/tests that referenced the old constant.
    IDLE_TEXT = "Ready - press Record (or F9) to start. Everything saves automatically."

    def _make_record_icons(self):
        """Red dot (idle) and white square (recording) images for the big
        button - the red record dot every Windows recorder uses."""
        size = max(18, int(22 * self._s))
        try:
            from PIL import Image, ImageDraw, ImageTk
        except ImportError:
            return None, None
        # Drawn 4x and scaled down so the dot has a smooth edge.
        big = Image.new("RGBA", (size * 4, size * 4), (0, 0, 0, 0))
        ImageDraw.Draw(big).ellipse([4, 4, size * 4 - 5, size * 4 - 5],
                                    fill=(239, 83, 80, 255))
        dot = big.resize((size, size), Image.LANCZOS)
        sq = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        m = max(2, size // 6)
        ImageDraw.Draw(sq).rectangle([m, m, size - 1 - m, size - 1 - m],
                                     fill=(255, 255, 255, 255))
        # A transparent spacer keeps the button's pixel width while it
        # shows "Starting..." / "Saving..." (no image = width in characters).
        self._rec_blank = ImageTk.PhotoImage(
            Image.new("RGBA", (size, size), (0, 0, 0, 0)))
        return ImageTk.PhotoImage(dot), ImageTk.PhotoImage(sq)

    def _build_record(self, bar):
        """The command bar: Record/Stop, big timer, status, save location."""
        s = self._s
        bar.columnconfigure(1, weight=1)
        self._rec_blank = None
        self._rec_dot, self._rec_square = self._make_record_icons()
        self.record_btn = tk.Button(
            bar, command=self._toggle_record, relief="flat", bd=0,
            font=("Segoe UI Semibold", 14), compound="left",
            padx=int(18 * s), pady=int(10 * s), cursor="hand2", takefocus=1,
            highlightthickness=2, highlightbackground=COLORS["panel"],
            highlightcolor=COLORS["fg"], disabledforeground="#c7ccd4")
        self.record_btn.grid(row=0, column=0, rowspan=3, sticky="nsw",
                             padx=(0, 20))
        self._rec_mode = "idle"
        self._rec_hover = False
        self.record_btn.bind("<Enter>", lambda e: self._rec_hover_set(True),
                             add="+")
        self.record_btn.bind("<Leave>", lambda e: self._rec_hover_set(False),
                             add="+")
        self._style_record_btn("idle")
        Tooltip(self.record_btn, "Start or stop recording (F9 works from "
                                 "anywhere in this window).")

        self.elapsed_lbl = ttk.Label(bar, text="00:00:00", style="Timer.TLabel")
        self.elapsed_lbl.grid(row=0, column=1, sticky="w")
        self.status_lbl = ttk.Label(bar, text=self._idle_text(),
                                    style="Bar.TLabel")
        self.status_lbl.grid(row=1, column=1, sticky="ew")
        self.status_lbl.bind("<Configure>", lambda e: self.status_lbl.configure(
            wraplength=max(200, e.width - 4)))

        # Where files go - the top question from new users. Looks like a
        # link, opens the folder; "Change..." picks another one.
        srow = ttk.Frame(bar, style="Bar.TFrame")
        srow.grid(row=2, column=1, sticky="ew", pady=(2, 0))
        self._saveto_row = srow
        self._saveto_prefix = ttk.Label(srow, text="Saving to", style="BarMuted.TLabel")
        self._saveto_prefix.pack(side="left")
        self.saveto_lbl = ttk.Label(srow, style="Link.TLabel", cursor="hand2",
                                    text=self.cfg.resolved_save_folder())
        self.saveto_lbl.pack(side="left", padx=(6, 0))
        self.saveto_lbl.bind(
            "<Button-1>",
            lambda e: self._open_folder(self.cfg.resolved_save_folder()))
        Tooltip(self.saveto_lbl, "Open the recordings folder (Ctrl+O).")
        self._saveto_change = ttk.Label(srow, text="Change...",
                                        style="Link.TLabel", cursor="hand2")
        self._saveto_change.pack(side="left", padx=(12, 0))
        self._saveto_change.bind("<Button-1>", lambda e: self._browse_folder())
        self._saveto_font = tkfont.Font(family=FONT, size=10, underline=True)
        srow.bind("<Configure>", lambda e: self._update_saveto())
        self.folder_var.trace_add("write", lambda *a: self._update_saveto())

        right = ttk.Frame(bar, style="Bar.TFrame")
        right.grid(row=0, column=2, rowspan=3, sticky="ne", padx=(16, 0))
        self.settings_btn = ttk.Button(right, text="Settings",
                                       command=self._open_settings)
        self.settings_btn.pack(anchor="e")
        Tooltip(self.settings_btn, "Settings (Ctrl+,)")
        lights = ttk.Frame(right, style="Bar.TFrame")
        lights.pack(anchor="e", pady=(10, 0))
        self.audio_light = StatusLight(lights, "Audio", bg=COLORS["panel"])
        self.audio_light.pack(anchor="w")
        self.screen_light = StatusLight(lights, "Screen", bg=COLORS["panel"])
        self.screen_light.pack(anchor="w", pady=(2, 0))
        self._idle_lights()

    def _rec_hover_set(self, on):
        self._rec_hover = on
        look = _REC_LOOK.get(self._rec_mode, _REC_LOOK["busy"])
        try:
            self.record_btn.config(bg=look[1] if on else look[0])
        except tk.TclError:
            pass

    def _style_record_btn(self, mode):
        """idle: red dot + 'Record' on a raised surface with a red outline.
        recording: red Stop. busy: a word saying what is happening."""
        b = self.record_btn
        s = self._s
        common = {"width": int(150 * s)} if self._rec_dot else {"width": 10}
        self._rec_mode = mode if mode in ("idle", "recording") else "busy"
        bg, hover, outline = _REC_LOOK[self._rec_mode]
        bg_now = hover if self._rec_hover else bg
        if mode == "recording":
            b.config(text="  Stop", image=self._rec_square or "", cursor="hand2",
                     bg=bg_now, fg="#ffffff", highlightbackground=outline,
                     activebackground="#ff7b72", activeforeground="#ffffff",
                     state="normal", **common)
        elif mode in ("starting", "saving"):
            # Stays "normal" (a disabled image is drawn stippled); the start
            # and stop latches already ignore clicks while busy.
            b.config(text="Starting..." if mode == "starting"
                     else "Saving...", image=self._rec_blank or "",
                     bg=bg, fg=COLORS["muted"], highlightbackground=outline,
                     activebackground=bg,
                     activeforeground=COLORS["muted"], cursor="watch",
                     state="normal", **common)
            return
        else:
            b.config(text="  Record", image=self._rec_dot or "", cursor="hand2",
                     bg=bg_now, fg="#ffffff", highlightbackground=outline,
                     activebackground="#4a5261", activeforeground="#ffffff",
                     state="normal", **common)
            if not self._rec_dot:
                b.config(text="●  Record")

    def _update_saveto(self):
        """Middle-ellipsize the save path to the space available so both the
        drive and the folder name stay readable."""
        try:
            path = self.cfg.resolved_save_folder()
            avail = (self._saveto_row.winfo_width()
                     - self._saveto_prefix.winfo_reqwidth()
                     - self._saveto_change.winfo_reqwidth() - int(30 * self._s))
            if avail < 60:
                avail = int(360 * self._s)
            self.saveto_lbl.config(text=ux.middle_ellipsize(
                path, avail, self._saveto_font.measure))
        except tk.TclError:
            pass

    def _idle_text(self):
        """'Ready - 2 sources + screen. Press Record or F9.'"""
        try:
            n = len(self._gather_sources())
        except (AttributeError, tk.TclError):
            n = 0
        bits = []
        if n:
            bits.append(ux.plural(n, "audio source"))
        try:
            if self.screen_enabled.get():
                bits.append("the screen")
        except AttributeError:
            pass
        if not bits:
            return "Add a microphone or turn on screen recording to start."
        return f"Ready to record {' + '.join(bits)}. Press Record or F9."

    @staticmethod
    def _open_path(path):
        """Open a file or folder with the system's default app (Explorer for
        folders) on every OS - os.startfile only exists on Windows."""
        if not path or not os.path.exists(path):
            return False
        try:
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
            return True
        except OSError as e:
            log.warning("open failed for %s: %s", path, e)
            return False

    def _open_folder(self, path):
        if path and os.path.isdir(path):
            self._open_path(path)

    def _build_strip(self, parent):
        """A slim result strip under the command bar: 'Saved - 2 tracks -
        3:12 - 41 MB  [Open folder] [Rename] [Play]'. Hidden until needed."""
        s = self._s
        self.strip = tk.Frame(parent, bg=COLORS["panel2"],
                              highlightthickness=0)
        self._strip_accent = tk.Frame(self.strip, bg=COLORS["green"],
                                      width=max(3, int(4 * s)))
        self._strip_accent.pack(side="left", fill="y")
        inner = ttk.Frame(self.strip, style="StripOk.TFrame",
                          padding=(12, 8, 8, 8))
        inner.pack(side="left", fill="both", expand=True)
        self._strip_title = ttk.Label(inner, style="StripOk.TLabel")
        self._strip_title.pack(side="left")
        self._strip_text = ttk.Label(inner, style="Strip.TLabel")
        self._strip_text.pack(side="left", padx=(10, 0), fill="x", expand=True)
        self._strip_close = ttk.Button(inner, text="✕", width=3,
                                       style="Toolbar.TButton",
                                       command=self._hide_strip)
        self._strip_close.pack(side="right")
        Tooltip(self._strip_close, "Dismiss")
        self._strip_actions = ttk.Frame(inner, style="StripOk.TFrame")
        self._strip_actions.pack(side="right", padx=(8, 8))

    def _show_strip(self, kind, title, text, actions=(), timeout_ms=None):
        """kind: 'ok' (green), 'warn' (gold) or 'info' (accent)."""
        color = {"ok": COLORS["green"], "warn": COLORS["gold"]}.get(
            kind, COLORS["accent"])
        style = {"ok": "StripOk", "warn": "StripWarn"}.get(kind, "StripInfo")
        try:
            self._strip_accent.configure(bg=color)
            self._strip_title.configure(text=title, style=f"{style}.TLabel")
            self._strip_text.configure(text=text)
            for w in self._strip_actions.winfo_children():
                w.destroy()
            for i, (label, fn) in enumerate(actions):
                ttk.Button(self._strip_actions, text=label,
                           style="Accent.TButton" if i == 0 else "Toolbar.TButton",
                           command=fn).pack(side="left", padx=(0, 6))
            if not self.strip.winfo_manager():
                self.strip.grid(row=1, column=0, sticky="ew", pady=(8, 0))
            if self._strip_job:
                self.after_cancel(self._strip_job)
                self._strip_job = None
            if timeout_ms:
                self._strip_job = self.after(timeout_ms, self._hide_strip)
        except tk.TclError:
            pass

    def _hide_strip(self):
        self._strip_job = None
        try:
            self.strip.grid_remove()
        except tk.TclError:
            pass

    def _set_status_note(self, text, ms=4000):
        """Briefly show a note in the status line, then go back to whatever
        is really happening (instead of a stale message staying forever)."""
        if self.recording or self._finalizing or self._starting:
            return
        self.status_lbl.config(text=text)
        self.after(ms, lambda: None if (self.recording or self._finalizing)
                   else self._restore_status())

    def _set_busy(self, on, text=None):
        """Show/hide the background-job progress in the Recordings footer.
        Its row keeps its height either way, so nothing jumps."""
        if not hasattr(self, "busy_bar"):
            return  # called while the window is still being built
        try:
            if text is not None:
                self.busy_lbl.config(text=text)
            if on:
                if not self.busy_bar.winfo_manager():
                    self.busy_lbl.pack(side="left", padx=(0, 8))
                    self.busy_bar.pack(side="left")
                    self.busy_cancel.pack(side="left", padx=(8, 0))
                    if str(self.busy_bar.cget("mode")) == "indeterminate":
                        self.busy_bar.start(12)
                if not self._transcribe_busy:
                    self.busy_cancel.config(state=(
                        "normal" if self._combine_queue else "disabled"))
            else:
                self.busy_bar.stop()
                self.busy_bar.config(mode="indeterminate", value=0)
                self.busy_cancel.config(text="Cancel remaining",
                                        command=self._cancel_queued_jobs)
                for w in (self.busy_lbl, self.busy_bar, self.busy_cancel):
                    w.pack_forget()
        except tk.TclError:
            pass

    def _cancel_queued_jobs(self):
        """Drop combine/convert jobs that haven't started; the running one
        finishes (stopping ffmpeg mid-write would leave a broken file)."""
        dropped, self._combine_queue = self._combine_queue, []
        for _fn, out in dropped:
            self._pending_out_paths.discard(out)
            self._combine_results.append((False, out,
                                          "Cancelled before it started."))
        if dropped:
            self.busy_lbl.config(text="Finishing the current job...")
        self.busy_cancel.config(state="disabled")

    def _build_library(self, parent):
        """Recordings as a real Windows list: multi-select with Ctrl/Shift,
        sortable columns, double-click opens, F2 renames, Del removes."""
        s = self._s
        head = ttk.Frame(parent, style="TFrame")
        head.grid(row=0, column=0, sticky="ew")
        ttk.Label(head, text="Recordings", style="Section.TLabel").pack(
            side="left")
        self.lib_count_lbl = ttk.Label(head, text="", style="Muted.TLabel")
        self.lib_count_lbl.pack(side="left", padx=(8, 0), pady=(3, 0))
        refresh_btn = ttk.Button(
            head, text="Refresh", style="Toolbar.TButton",
            command=lambda: self._refresh_library(rescan=True))
        refresh_btn.pack(side="right")
        Tooltip(refresh_btn, "Look in the save folder for recordings made "
                             "outside this app.")

        # The toolbar wraps onto a second line in a narrow (snapped) window
        # instead of cutting buttons off at the pane edge.
        tb = ttk.Frame(parent, style="TFrame", height=int(30 * s))
        tb.grid(row=1, column=0, sticky="ew", pady=(8, 8))
        self.lib_btn_open = ttk.Button(tb, text="Open folder",
                                       style="Toolbar.TButton",
                                       command=self._open_selected_library)
        Tooltip(self.lib_btn_open, "Open the selected recording's folder "
                                   "(or double-click it).")
        self.lib_btn_combine = ttk.Menubutton(tb, text="Combine",
                                              direction="below")
        self.lib_combine_menu = tk.Menu(
            self.lib_btn_combine, tearoff=0, bg=COLORS["panel2"],
            fg=COLORS["fg"], activebackground=COLORS["accent"],
            activeforeground="#06120f", disabledforeground="#6b717c", bd=0)
        self.lib_combine_menu.add_command(
            label="Make one video with sound",
            command=lambda: self._combine_selected_library("video"))
        self.lib_combine_menu.add_command(
            label="Combine audio into one multitrack file",
            command=lambda: self._combine_selected_library("multitrack"))
        self.lib_combine_menu.add_command(
            label="Mix all audio into one stereo file",
            command=lambda: self._combine_selected_library("mix"))
        self.lib_btn_combine["menu"] = self.lib_combine_menu
        Tooltip(self.lib_btn_combine, "Join the selected recordings into one "
                                      "file. Your originals are kept.")
        self.lib_btn_convert = ttk.Button(
            tb, text="Convert...", style="Toolbar.TButton",
            command=self._convert_selected_library)
        Tooltip(self.lib_btn_convert, "Export each selected recording to "
                                      "another format (MP4, MP3, MKV, WAV...).")
        # Only shown when a Scrivox install is detected (see _refresh_library).
        self.lib_btn_transcribe = ttk.Button(
            tb, text="Transcribe...", style="Toolbar.TButton",
            command=self._transcribe_selected_library)
        Tooltip(self.lib_btn_transcribe, "Turn the selected recordings into "
                                         "text with Scrivox.")
        self.lib_btn_remove = ttk.Button(tb, text="Remove",
                                         style="Toolbar.TButton",
                                         command=self._remove_selected_library)
        Tooltip(self.lib_btn_remove, "Remove from this list (Del). Files on "
                                     "disk are never deleted.")
        self._lib_tb_relayout = self._flow(
            tb, [self.lib_btn_open, self.lib_btn_combine, self.lib_btn_convert,
                 self.lib_btn_transcribe, self.lib_btn_remove],
            visible=lambda w: (w is not self.lib_btn_transcribe
                               or bool(self._scrivox_exe)),
            gaps={self.lib_btn_remove: int(16 * s)})

        tf = ttk.Frame(parent, style="TFrame")
        tf.grid(row=2, column=0, sticky="nsew")
        tf.columnconfigure(0, weight=1)
        tf.rowconfigure(0, weight=1)
        cols = ("name", "created", "length", "contents", "size")
        tree = ttk.Treeview(tf, columns=cols, show="headings",
                            selectmode="extended")
        self.lib_tree = tree
        # Preferred widths come from the text they must hold, so they are
        # right at any DPI and with any font.
        f = tkfont.Font(family=FONT, size=10)
        pad = int(20 * s)
        self._lib_col_widths = {
            "created": f.measure("28 Sep 2026, 10:00") + pad,
            "length": f.measure("10:00:00") + pad,
            "contents": f.measure("3 tracks + screen") + pad,
            "size": f.measure("999 MB") + pad,
        }
        self._lib_name_min = f.measure("Recording 22 Sep 20") + pad
        self._lib_cols_shown = None
        self._lib_font = f
        self._lib_name_px = 0
        self._lib_relabel_job = None
        spec = {"name": ("Name", "w"), "created": ("Recorded", "w"),
                "length": ("Length", "e"), "contents": ("Contents", "w"),
                "size": ("Size", "e")}
        for c in cols:
            title, anchor = spec[c]
            tree.heading(c, text=title, anchor=anchor,
                         command=lambda c=c: self._sort_library(c))
            if c == "name":
                tree.column(c, width=self._lib_name_min * 2,
                            minwidth=self._lib_name_min, stretch=True,
                            anchor=anchor)
            else:
                w = self._lib_col_widths[c]
                tree.column(c, width=w, minwidth=w, stretch=False,
                            anchor=anchor)
        tree.bind("<Configure>", self._fit_library_columns, add="+")
        vsb = ttk.Scrollbar(tf, orient="vertical", command=tree.yview)

        def _yset(lo, hi):
            vsb.set(lo, hi)
            if float(lo) <= 0.0 and float(hi) >= 1.0:
                vsb.grid_remove()
            else:
                vsb.grid()
        tree.configure(yscrollcommand=_yset)
        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self.lib_empty = ttk.Label(
            tf, style="CardMuted.TLabel", justify="center",
            text="No recordings yet.\nPress Record - each recording shows "
                 "up here the moment you stop.")

        tree.bind("<<TreeviewSelect>>", lambda e: self._update_library_buttons())
        tree.bind("<Double-1>", self._on_library_double)
        tree.bind("<Return>", lambda e: self._open_selected_library())
        tree.bind("<F2>", lambda e: self._rename_selected_library())
        tree.bind("<Delete>", lambda e: self._remove_selected_library())
        tree.bind("<Control-a>", lambda e: (self._set_all_ticks(True), "break")[1])
        tree.bind("<Button-3>", self._on_library_right_click)
        tree.bind("<Button-1>", self._on_library_click, add="+")

        foot = ttk.Frame(parent, style="TFrame")
        foot.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        # One shared busy area for combine/convert/transcribe background
        # jobs. Packed first so the selection text can never squeeze it.
        self.busy_row = ttk.Frame(foot, style="TFrame")
        self.busy_row.pack(side="right")
        self.lib_sel_lbl = ttk.Label(foot, text="", style="Muted.TLabel",
                                     justify="left", wraplength=int(300 * s))
        self.lib_sel_lbl.pack(side="left", fill="x", expand=True)
        self.lib_sel_lbl.bind("<Configure>", lambda e: self.lib_sel_lbl.configure(
            wraplength=max(160, e.width - 4)))
        self.busy_lbl = ttk.Label(self.busy_row, text="", style="Muted.TLabel")
        self.busy_bar = ttk.Progressbar(self.busy_row, mode="indeterminate",
                                        length=int(120 * s),
                                        style="Busy.Horizontal.TProgressbar")
        self.busy_cancel = ttk.Button(self.busy_row, text="Cancel remaining",
                                      style="Toolbar.TButton",
                                      command=self._cancel_queued_jobs)
        # Reserve the row height so showing the progress never shifts the list.
        foot.update_idletasks()
        foot.rowconfigure(0, minsize=self.busy_cancel.winfo_reqheight())
        ttk.Frame(foot, height=self.busy_cancel.winfo_reqheight(),
                  width=1, style="TFrame").pack(side="right")
        self._refresh_library()

    def _fit_library_columns(self, _event=None):
        """Show only the columns that fit; Name takes what is left. In a
        snapped window Contents goes first, then Size - never a Name column
        squeezed to 'Record' or a Size column pushed off the edge."""
        tree = self.lib_tree
        try:
            avail = tree.winfo_width()
        except tk.TclError:
            return
        if avail <= 1:
            return
        shown = ux.library_columns(avail - 4, self._lib_col_widths,
                                   self._lib_name_min)
        if shown != self._lib_cols_shown:
            self._lib_cols_shown = shown
            tree.configure(displaycolumns=shown)
        rest = sum(self._lib_col_widths[c] for c in shown if c != "name")
        name_w = max(self._lib_name_min, avail - rest - 4)
        if int(tree.column("name", "width")) != name_w:
            tree.column("name", width=name_w)
        if name_w != self._lib_name_px:
            # Names that don't fit end in '…' instead of being cut mid-letter.
            self._lib_name_px = name_w
            if self._lib_relabel_job:
                self.after_cancel(self._lib_relabel_job)
            self._lib_relabel_job = self.after(60, self._relabel_library)

    def _relabel_library(self):
        self._lib_relabel_job = None
        for iid, e in self._lib_iids.items():
            if self.lib_tree.exists(iid):
                self.lib_tree.item(iid, values=self._lib_values(e))

    def _on_library_double(self, event):
        iid = self.lib_tree.identify_row(event.y)
        if iid and iid in self._lib_iids:
            self._open_entry_folder(self._lib_iids[iid])
        return "break"

    def _on_library_click(self, event):
        """Clicking empty space below the rows clears the selection, as in
        Explorer."""
        if self.lib_tree.identify_region(event.x, event.y) in ("nothing", ""):
            self.lib_tree.selection_set(())
            self.lib_tree.focus_set()

    def _select_all_library(self):
        self._set_all_ticks(True)
        self.lib_tree.focus_set()

    def _on_library_right_click(self, event):
        iid = self.lib_tree.identify_row(event.y)
        if not iid or iid not in self._lib_iids:
            return
        if iid not in self.lib_tree.selection():
            self.lib_tree.selection_set(iid)
        self.lib_tree.focus(iid)
        self._show_library_menu(event, self._lib_iids[iid])

    def _add_to_library(self, select_new=False):
        audio = [a for a in (self.last_outputs.get("audio") or [])
                 if a and os.path.isfile(a)]
        # A mid-take screen auto-restart splits the capture into several
        # segments; ALL of them belong to this take. make_entry orders them
        # chronologically and records them as video_segments so combining
        # joins every segment instead of dropping one.
        vids = [v for v in ([self.last_outputs.get("video") or ""]
                            + (self.last_outputs.get("videos_extra") or []))
                if v and os.path.isfile(v)]
        if not audio and not vids:
            return None
        self._lib_seq += 1
        entry = library.make_entry(
            entry_id=f"rec{self._lib_seq}-{int(self._record_start_mono)}",
            name=getattr(self, "_session_base", "recording"),
            out_dir=self.last_outputs.get("out_dir", ""),
            audio=audio, video=(vids[0] if vids else ""),
            video_segments=vids,
            created=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        if self._last_take_secs:
            self._lib_meta[entry["id"]] = (self._meta_key(entry),
                                           self._last_take_secs,
                                           ux.total_size(audio + vids))
        self._library.append(entry)
        self.cfg.set("recordings", self._library)
        self._refresh_library()
        # Select the recording that was just made so its actions are ready.
        if select_new:
            for iid, e in self._lib_iids.items():
                if e.get("id") == entry["id"]:
                    self.lib_tree.selection_set(iid)
                    self.lib_tree.focus(iid)
                    self.lib_tree.see(iid)
                    # Keyboard-ready: F2 renames, Enter opens, Del removes.
                    # (F9 is bound everywhere, so Record still works.)
                    if self.grab_current() is None:
                        self.lib_tree.focus_set()
                    break
            self._update_library_buttons()
        return entry

    @staticmethod
    def _meta_key(entry):
        return tuple(entry.get("audio") or []) + tuple(
            entry.get("video_segments") or [entry.get("video") or ""])

    def _refresh_library(self, rescan=False):
        if rescan:
            # Disk-bound: scan (and re-detect Scrivox) on a worker, then come
            # back here with the result.
            known = {e.get("out_dir") for e in self._library}
            folder = self.cfg.resolved_save_folder()
            override = self.cfg.get("scrivox_path")
            self._set_status_note("Looking for recordings...")

            def work():
                try:
                    exe = scrivox_bridge.find_scrivox(override, force=True)
                except Exception:
                    exe = None
                try:
                    found = library.scan_folder(folder, existing_dirs=known)
                except Exception as e:
                    log.warning("Library rescan failed: %s", e)
                    found = []
                self._safe_after(lambda: self._apply_scan(found, exe,
                                                          announce=True))
            threading.Thread(target=work, name="library-scan",
                             daemon=True).start()
            return
        tree = self.lib_tree
        # Remember the selection so a refresh (e.g. right after a long merge
        # finishes) doesn't make the user re-find it.
        selected = {e.get("id") for e in self._selected_library_entries()}
        focus_id = self._lib_iids.get(tree.focus(), {}).get("id")
        # Prune anything whose files vanished, then rebuild the list.
        self._library, pruned = library.prune(self._library)
        if pruned:
            self.cfg.set("recordings", self._library)
        tree.delete(*tree.get_children())
        self._lib_iids = {}
        self._lib_rows = []
        col, desc = self._lib_sort
        entries = sorted(self._library, key=lambda e: self._lib_sort_key(e, col),
                         reverse=desc)
        for i, e in enumerate(entries):
            iid = f"e{i}"
            self._lib_iids[iid] = e
            tree.insert("", "end", iid=iid, values=self._lib_values(e))
            if e.get("id") in selected:
                tree.selection_add(iid)
            if e.get("id") == focus_id:
                tree.focus(iid)
            self._lib_rows.append({"iid": iid, "entry": e, "frame": tree,
                                   "var": _TreeSelVar(tree, iid)})
        for c in ("name", "created", "length", "contents", "size"):
            title = tree.heading(c, "text").rstrip(" ▲▼")
            if c == col:
                title += " ▼" if desc else " ▲"
            tree.heading(c, text=title)
        n = len(self._library)
        self.lib_count_lbl.config(text=f"({n})" if n else "")
        if n:
            self.lib_empty.place_forget()
        else:
            self.lib_empty.place(relx=0.5, rely=0.45, anchor="center")
        # Re-detect Scrivox on every rebuild so dropping it next to the app (or
        # installing it) starts working without a restart - and removing it
        # hides the button again. Cached in the bridge; never re-detect
        # mid-transcription or before the background detection has run.
        if self._scrivox_checked and not self._transcribe_busy:
            self._scrivox_exe = scrivox_bridge.find_scrivox(
                self.cfg.get("scrivox_path"))
        self._lib_tb_relayout()
        self._update_library_buttons()
        self._fill_library_meta()

    def _lib_values(self, e):
        meta = self._lib_meta.get(e.get("id"))
        if meta and meta[0] == self._meta_key(e):
            length, size = ux.fmt_duration(meta[1]) if meta[1] else "", \
                ux.fmt_bytes(meta[2])
        else:
            length, size = "...", "..."
        n_audio = len(e.get("audio") or [])
        name = ux.friendly_recording_name(e.get("name", ""))
        if self._lib_name_px:
            name = ux.end_ellipsize(name, self._lib_name_px - int(14 * self._s),
                                    self._lib_font.measure)
        return (name,
                ux.friendly_created(e.get("created") or ""),
                length, ux.contents_text(n_audio, 1 if e.get("video") else 0),
                size)

    def _lib_sort_key(self, e, col):
        if col == "name":
            return ux.friendly_recording_name(e.get("name", "")).lower()
        if col in ("length", "size"):
            meta = self._lib_meta.get(e.get("id"))
            if not meta:
                return -1.0
            return float((meta[1] if col == "length" else meta[2]) or 0)
        if col == "contents":
            return (len(e.get("audio") or []), 1 if e.get("video") else 0)
        return e.get("created") or ""

    def _sort_library(self, col):
        cur, desc = self._lib_sort
        desc = (not desc) if col == cur else (col in ("created", "length",
                                                        "size"))
        self._lib_sort = (col, desc)
        self.cfg.set("library_sort", ("-" if desc else "") + col)
        self._refresh_library()

    def _fill_library_meta(self):
        """Length and size come from the files (WAV headers, file sizes), so
        they are read on a worker and filled in as they arrive."""
        todo = [e for e in self._library
                if (self._lib_meta.get(e.get("id")) or (None,))[0]
                != self._meta_key(e)]
        if not todo or self._lib_meta_busy:
            return
        self._lib_meta_busy = True

        def work():
            out = {}
            for e in todo:
                files = list(e.get("audio") or []) + list(
                    e.get("video_segments") or [e.get("video") or ""])
                secs = 0.0
                for a in e.get("audio") or []:
                    try:
                        import soundfile
                        secs = float(soundfile.info(a).duration)
                        break
                    except Exception as ex:
                        log.debug("no duration for %s: %s", a, ex)
                        continue
                out[e.get("id")] = (self._meta_key(e), secs,
                                    ux.total_size([f for f in files if f]))
            self._safe_after(lambda: self._apply_library_meta(out))
        threading.Thread(target=work, name="library-meta", daemon=True).start()

    def _apply_library_meta(self, meta):
        self._lib_meta_busy = False
        self._lib_meta.update(meta)
        for iid, e in self._lib_iids.items():
            if e.get("id") in meta and self.lib_tree.exists(iid):
                self.lib_tree.item(iid, values=self._lib_values(e))
        if self._lib_sort[0] in ("length", "size"):
            self._refresh_library()
        # Rows added while that pass was running (a rename, a rescan, a new
        # take) still show '...': fill them in now. No-op when all are done.
        self._fill_library_meta()

    def _update_library_buttons(self):
        """Enable the actions that fit the current selection, and say in
        words why the others are unavailable."""
        sel = self._selected_library_entries()
        n = len(sel)
        states = {}
        if n == 0:
            hint = ("Select recordings to combine, convert or transcribe. "
                    "Ctrl+click picks several; double-click opens one."
                    if self._library else "")
            self.lib_sel_lbl.config(text=hint)
            for i in range(3):
                self.lib_combine_menu.entryconfigure(i, state="disabled")
            for btn in (self.lib_btn_combine, self.lib_btn_convert,
                        self.lib_btn_transcribe, self.lib_btn_remove):
                btn.state(["disabled"])
            return
        all_video = all(e.get("video") for e in sel)
        any_video = any(e.get("video") for e in sel)
        total_audio = sum(len(e.get("audio", [])) for e in sel)
        states["video"] = all_video
        states["multi"] = total_audio >= 2
        states["mix"] = total_audio >= 1
        for i, key in enumerate(("video", "multi", "mix")):
            self.lib_combine_menu.entryconfigure(
                i, state="normal" if states[key] else "disabled")
        self.lib_btn_combine.state(["!disabled"] if any(states.values())
                                   else ["disabled"])
        self.lib_btn_convert.state(["!disabled"])
        self.lib_btn_remove.state(["!disabled"])
        has_media = any(e.get("audio") or e.get("video") for e in sel)
        self.lib_btn_transcribe.state(["!disabled"] if has_media
                                      else ["disabled"])
        text = f"{ux.plural(n, 'recording')} selected."
        if any_video and not all_video:
            text += (" 'Make one video' needs a screen recording in every "
                     "selected one.")
        elif n == 1 and total_audio < 2 and not all_video:
            text += " Select another to combine them."
        self.lib_sel_lbl.config(text=text)

    def _set_all_ticks(self, on):
        tree = self.lib_tree
        if on:
            tree.selection_set(tree.get_children())
        else:
            tree.selection_remove(tree.selection())
        self._update_library_buttons()

    def _selected_library_entries(self):
        try:
            return [self._lib_iids[i] for i in self.lib_tree.selection()
                    if i in self._lib_iids]
        except (AttributeError, tk.TclError):
            return []

    def _all_selected_audio(self, sel):
        files = []
        for e in sel:
            for a in e.get("audio", []):
                if a and os.path.isfile(a) and a not in files:
                    files.append(a)
        return files

    def _combine_selected_library(self, mode):
        """mode: 'video' (one video with mixed sound), 'multitrack' (one
        multichannel WAV), or 'mix' (one stereo mix). Acts on ticked rows."""
        sel = self._selected_library_entries()
        if not sel:
            self._notice("Combine recordings",
                                "Select at least one recording first.")
            return
        out_dir = sel[0].get("out_dir") or self.cfg.resolved_save_folder()
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        # Base the output name on the recording when one is selected, so a merge
        # of "Daytona-DHS" becomes "Daytona-DHS_<kind>_<stamp>" rather than a
        # generic SRR_ name. For multi-selection there is no single name, so use
        # the first entry's name as the prefix.
        prefix = (sel[0].get("name") or "SRR").strip() or "SRR"

        if mode == "video":
            if not all(e.get("video") for e in sel):
                self._notice(
                    "Need video",
                    "Every selected recording must have a screen video for this. "
                    "Deselect the audio-only ones, or use an audio option instead.")
                return
            ext = self.container_var.get()
            out = self._unique_path(
                os.path.join(out_dir, f"{prefix}_merged_{stamp}.{ext}"))
            # Each entry contributes ALL its video segments (a screen
            # restart can leave several); concat_sessions joins each take's
            # segments, then concatenates the takes.
            sessions = [{"audio": e.get("audio", []),
                         "videos": (e.get("video_segments")
                                    or ([e.get("video")] if e.get("video")
                                        else []))}
                        for e in sel]
            self._run_combine(
                lambda: combine.concat_sessions(sessions, out, True), out)
        elif mode == "multitrack":
            audio = self._all_selected_audio(sel)
            if len(audio) < 2:
                self._notice("Need more tracks",
                                    "Select recordings with at least two audio "
                                    "tracks between them.")
                return
            out = self._unique_path(
                os.path.join(out_dir, f"{prefix}_multitrack_{stamp}.wav"))
            self._run_combine(
                lambda: combine.merge_audio_to_channels(audio, out), out)
        else:  # mix
            audio = self._all_selected_audio(sel)
            if not audio:
                self._notice("No audio", "No audio in the selection.")
                return
            out = self._unique_path(
                os.path.join(out_dir, f"{prefix}_mixed_{stamp}.wav"))
            self._run_combine(
                lambda: combine.mix_audio_to_stereo(audio, out), out)

    # --- Scrivox transcription (only reachable when Scrivox is detected) --- #
    def _transcribe_selected_library(self):
        """Transcribe every ticked recording with the detected Scrivox."""
        sel = self._selected_library_entries()
        if not sel:
            self._notice("Transcribe",
                                "Select at least one recording first.")
            return
        self._transcribe_entries(sel)

    def _transcribe_entries(self, entries):
        if self._transcribe_busy:
            self._notice("Please wait",
                                "A transcription is already running.")
            return
        exe = self._scrivox_exe
        if not exe or not os.path.isfile(exe):
            # Scrivox was moved/removed since detection; re-check and hide.
            self._refresh_library()
            if not self._scrivox_exe:
                self._notice(
                    "Scrivox not found",
                    "Scrivox is no longer where it was detected. Put it back, "
                    "reinstall it, or set its location in Settings > "
                    "Transcription, then try again.")
                return
            exe = self._scrivox_exe
        any_video = any(e.get("video") for e in entries)
        multi_track = any(
            len([a for a in e.get("audio", []) if a]) > 1 for e in entries)
        # Combining can happen for any entry with several tracks, or with a
        # separate video + audio pair (the vision mux).
        any_combinable = any(
            len([a for a in e.get("audio", []) if a]) > 1
            or (e.get("video") and e.get("audio")) for e in entries)
        opts = self._transcribe_dialog(len(entries), any_video, multi_track,
                                       any_combinable)
        if not opts:
            return

        self._transcribe_busy = True
        n = len(entries)
        self._transcribe_status(f"Transcribing 1/{n} with Scrivox...")
        log.info("Transcription started: %d recording(s), opts=%s", n, opts)
        # Determinate progress (the total is known) + a safe between-files
        # stop: no processes are killed, the current file simply becomes the
        # last one.
        self._transcribe_cancel = threading.Event()
        try:
            # Marquee, not determinate: a determinate bar counting FILES sat
            # at zero for the whole of a single-recording run and looked
            # frozen. Motion means alive; the status label carries file i/n
            # and the live Scrivox output.
            self.busy_cancel.config(text="Stop after current file",
                                    state="normal",
                                    command=self._transcribe_cancel.set)
            self._set_busy(True)
        except Exception:
            pass

        def work():
            results = []
            for i, e in enumerate(entries):
                name = e.get("name") or "recording"
                if self._transcribe_cancel.is_set():
                    results.append((name, False, "Skipped - you pressed Stop."))
                    continue
                def status(msg, i=i, name=name):
                    self._safe_after(lambda: self._transcribe_status(
                        f"Transcribing {i + 1}/{n} ({name}): {msg}"))
                # One entry blowing up (e.g. ffmpeg mix timeout raises) must
                # not kill the worker - that would leave _transcribe_busy
                # stuck True and block every later transcription.
                try:
                    ok, detail = scrivox_bridge.transcribe_entry(
                        exe, e, opts, on_status=status)
                except Exception as ex:
                    ok, detail = False, str(ex)
                results.append((name, ok, detail))
            self._safe_after(lambda: self._transcribe_done(results))
        threading.Thread(target=work, name="scrivox", daemon=True).start()

    def _transcribe_status(self, text):
        # Recording status always wins the label; transcription is background.
        try:
            self.busy_lbl.config(text=text if len(text) < 70
                                 else text[:67] + "...")
        except tk.TclError:
            pass
        if not self.recording and not self._finalizing and not self._starting:
            self.status_lbl.config(text=text)

    def _restore_status(self):
        """Put the status label back to whatever is still going on, in
        priority order, so finishing one background job never hides another."""
        if not hasattr(self, "status_lbl"):
            return
        if self.recording:
            self.status_lbl.config(text=self._recording_status_text())
        elif self._starting:
            self.status_lbl.config(text="Starting...")
        elif self._finalizing:
            self.status_lbl.config(text="Saving your recording...")
        elif self._combine_busy:
            self.status_lbl.config(
                text="Combining... (this can take a while for video)")
        elif self._transcribe_busy:
            self.status_lbl.config(text="Transcribing with Scrivox...")
        else:
            self.status_lbl.config(text=self._idle_text())
        if not self._combine_busy and not self._transcribe_busy:
            self._set_busy(False)

    def _transcribe_done(self, results):
        self._transcribe_busy = False
        self._restore_status()
        # Success detail is a LIST of transcript paths (per-track mode can
        # produce several per recording); failure detail is an error string.
        done = [(n, d) for n, ok, d in results if ok]
        failed = [(n, d) for n, ok, d in results if not ok]
        paths_ = [p for _, ps in done for p in ps]
        for p in paths_:
            log.info("Transcript saved: %s", p)
        for n, d in failed:
            log.error("Transcription failed for '%s': %s", n, str(d)[:800])
        if paths_:
            first = paths_[0]
            names = ", ".join(os.path.basename(p) for p in paths_[:2])
            if len(paths_) > 2:
                names += f" and {len(paths_) - 2} more"
            self._show_strip(
                "ok" if not failed else "warn",
                "Transcribed" if not failed else "Partly transcribed",
                f"Saved {ux.plural(len(paths_), 'transcript')}: {names}",
                [("Show in folder", lambda: self._reveal_path(first)),
                 ("Open", lambda: self._open_path(first))])
        if failed:
            self._error(
                "Transcription failed" if not done else
                "Some transcriptions failed",
                ("No transcripts were made." if not done else
                 f"{ux.plural(len(failed), 'recording')} could not be "
                 "transcribed."),
                "Check that Scrivox works on its own (Open Scrivox in "
                "Settings > Transcription), then try again.",
                details="\n\n".join(f"{n}:\n{str(d)[-1500:]}"
                                    for n, d in failed))

    def _reveal_path(self, path):
        """Show a file highlighted in Explorer (Finder on macOS)."""
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                self._open_path(os.path.dirname(path))
        except OSError as e:
            log.warning("reveal failed: %s", e)

    def _modal_dialog(self, title, parent=None):
        """Shared boilerplate for the app's small themed dialogs. Created
        hidden and shown centered by _finish_dialog (no flash at 0,0)."""
        master = parent or self
        win = tk.Toplevel(master)
        win.withdraw()
        win._srr_dialog = True
        win.title(title)
        win.configure(bg=COLORS["bg"])
        try:
            # A transient of a minimized/hidden window is invisible on
            # Windows - only tie the dialog to a master that is on screen.
            if master.winfo_viewable():
                win.transient(master)
        except tk.TclError:
            pass
        win.resizable(False, False)
        try:
            ip = paths.icon_path()
            if ip:
                win.iconbitmap(ip)
        except tk.TclError:
            pass
        return win

    def _finish_dialog(self, win, ok_fn, focus=None, cancel_fn=None,
                       bind_return=True):
        """Keyboard parity + centering: Enter confirms (or presses the focused
        button), Escape cancels. Then show, grab and focus."""
        cancel = cancel_fn or win.destroy

        def on_return(_e):
            w = win.focus_get()
            if isinstance(w, ttk.Button):
                w.invoke()
            else:
                ok_fn()
            return "break"
        if bind_return:
            win.bind("<Return>", on_return)
        win.bind("<Escape>", lambda e: cancel())
        win.protocol("WM_DELETE_WINDOW", cancel)
        win.update_idletasks()
        try:
            master = win.master if win.master.winfo_viewable() else None
            ww, wh = win.winfo_reqwidth(), win.winfo_reqheight()
            if master is not None:
                x = master.winfo_rootx() + (master.winfo_width() - ww) // 2
                y = master.winfo_rooty() + (master.winfo_height() - wh) // 3
            else:
                x = (win.winfo_screenwidth() - ww) // 2
                y = (win.winfo_screenheight() - wh) // 3
            win.geometry(f"+{max(0, x)}+{max(0, y)}")
        except tk.TclError:
            pass
        win.deiconify()
        set_dark_titlebar(win)
        win.lift()
        try:
            win.grab_set()
        except tk.TclError:
            pass  # not viewable yet; the dialog still works without a grab
        (focus or win).focus_set()

    def _dialog_body(self, win, heading, message, kind="info"):
        """Heading + wrapped message in a padded frame; returns the frame."""
        s = self._s
        frm = ttk.Frame(win, style="TFrame", padding=(24, 20, 24, 16))
        frm.pack(fill="both", expand=True)
        color = {"warning": COLORS["gold"], "error": COLORS["red"]}.get(kind)
        head = ttk.Frame(frm, style="TFrame")
        head.pack(fill="x")
        if color:
            tk.Frame(head, bg=color, width=max(3, int(4 * s))).pack(
                side="left", fill="y", padx=(0, 10))
        ttk.Label(head, text=heading, style="Section.TLabel",
                  wraplength=int(440 * s), justify="left").pack(
            side="left", anchor="w")
        if message:
            ttk.Label(frm, text=message, style="TLabel", justify="left",
                      wraplength=int(440 * s), font=(FONT, 10)).pack(
                anchor="w", pady=(10, 0))
        return frm

    def _confirm(self, heading, message, yes="OK", no="Cancel",
                 default="no", parent=None, check=None, kind="info"):
        """Themed yes/no with verb buttons and a SAFE default: Enter and
        Escape pick `no` unless default='yes'. With check='text', returns
        (answer, checked)."""
        win = self._modal_dialog(heading, parent=parent)
        frm = self._dialog_body(win, heading, message, kind=kind)
        result = {"v": False}
        check_var = tk.BooleanVar(value=False)
        if check:
            ttk.Checkbutton(frm, text=check, variable=check_var).pack(
                anchor="w", pady=(12, 0))
        btns = ttk.Frame(frm, style="TFrame")
        btns.pack(fill="x", pady=(20, 0))

        def answer(v):
            result["v"] = v
            win.destroy()
        no_btn = ttk.Button(btns, text=no, command=lambda: answer(False),
                            style="Accent.TButton" if default == "no"
                            else "TButton")
        yes_btn = ttk.Button(btns, text=yes, command=lambda: answer(True),
                             style="Accent.TButton" if default == "yes"
                             else "TButton")
        no_btn.pack(side="right")
        yes_btn.pack(side="right", padx=(0, 8))
        focus = yes_btn if default == "yes" else no_btn
        self._finish_dialog(win, focus.invoke, focus=focus,
                            cancel_fn=lambda: answer(False))
        win.wait_window()
        return (result["v"], check_var.get()) if check else result["v"]

    def _notice(self, heading, message, kind="info", parent=None):
        """Themed replacement for messagebox.showinfo/showwarning."""
        win = self._modal_dialog(heading, parent=parent)
        frm = self._dialog_body(win, heading, message, kind=kind)
        btns = ttk.Frame(frm, style="TFrame")
        btns.pack(fill="x", pady=(20, 0))
        ok = ttk.Button(btns, text="OK", style="Accent.TButton",
                        command=win.destroy)
        ok.pack(side="right")
        self._finish_dialog(win, win.destroy, focus=ok)
        win.wait_window()

    def _error(self, heading, what, todo=None, details=None, parent=None):
        """Plain-language error: what happened, what to do, and the technical
        details folded away (with Copy and Open log) for support."""
        s = self._s
        win = self._modal_dialog(heading, parent=parent)
        msg = what + (f"\n\n{todo}" if todo else "")
        frm = self._dialog_body(win, heading, msg, kind="error")
        det = None
        if details:
            det = ttk.Frame(frm, style="TFrame")
            txt = tk.Text(det, height=7, width=60, wrap="word",
                          bg="#101216", fg="#c9ced6", relief="flat",
                          font=("Consolas", 9), padx=8, pady=6,
                          highlightthickness=1,
                          highlightbackground=COLORS["border"])
            txt.insert("1.0", str(details).strip())
            txt.configure(state="disabled")
            txt.pack(fill="both", expand=True)
        btns = ttk.Frame(frm, style="TFrame")
        btns.pack(fill="x", pady=(20, 0))
        if det is not None:
            def toggle():
                if det.winfo_manager():
                    det.pack_forget()
                    more.config(text="Show details")
                else:
                    det.pack(fill="both", expand=True, pady=(12, 0),
                             before=btns)
                    more.config(text="Hide details")
            more = ttk.Button(btns, text="Show details",
                              style="Toolbar.TButton", command=toggle)
            more.pack(side="left")

            def copy():
                self.clipboard_clear()
                self.clipboard_append(str(details))
            ttk.Button(btns, text="Copy details", style="Toolbar.TButton",
                       command=copy).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="Open log", style="Toolbar.TButton",
                   command=lambda: self._open_path(paths.logs_dir())).pack(
            side="left", padx=(8, 0))
        ok = ttk.Button(btns, text="OK", style="Accent.TButton",
                        command=win.destroy)
        ok.pack(side="right", padx=(int(24 * s), 0))
        self._finish_dialog(win, win.destroy, focus=ok)
        win.wait_window()

    def _ask_text(self, heading, prompt, initial="", ok_text="OK"):
        """Themed one-line text input (replaces simpledialog.askstring)."""
        s = self._s
        win = self._modal_dialog(heading)
        frm = self._dialog_body(win, heading, prompt)
        var = tk.StringVar(value=initial)
        ent = ttk.Entry(frm, textvariable=var, width=46, font=(FONT, 10))
        ent.pack(fill="x", pady=(12, 0), ipady=int(2 * s))
        result = {"v": None}

        def ok():
            result["v"] = var.get()
            win.destroy()
        btns = ttk.Frame(frm, style="TFrame")
        btns.pack(fill="x", pady=(20, 0))
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(btns, text=ok_text, style="Accent.TButton",
                   command=ok).pack(side="right", padx=(0, 8))
        self._finish_dialog(win, ok, focus=ent)
        ent.select_range(0, "end")
        ent.icursor("end")
        win.wait_window()
        return result["v"]

    _SCRIVOX_USE_SETTING = "Use Scrivox setting"

    def _transcribe_dialog(self, n_entries, any_video, multi_track,
                           any_combinable=False):
        """Modal dialog for transcription options: one clear question first
        (what do you want?), everything else under More settings.
        Returns a scrivox_bridge.default_options()-shaped dict, or None."""
        s = self._s
        win = self._modal_dialog("Transcribe with Scrivox")
        result = {"value": None}
        frm = ttk.Frame(win, style="TFrame", padding=(24, 20, 24, 16))
        frm.pack(fill="both", expand=True)

        word = "recording" if n_entries == 1 else "recordings"
        ttk.Label(frm, text=f"Transcribe {n_entries} {word}",
                  style="Section.TLabel").pack(anchor="w")

        # ---- quick presets: pick the deliverable, tune anything after ----
        fmt_var = tk.StringVar(value="Plain text (.txt)")
        preset_var = tk.StringVar(value="transcript")
        preset_opts = [("transcript", "Transcript"),
                       ("notes", "Meeting notes + transcript")]
        if any_video:
            preset_opts.insert(1, ("subtitles", "Subtitles for the video (.srt)"))

        def _apply_preset(*_a):
            p = preset_var.get()
            if p == "subtitles":
                fmt_var.set("Subtitles (.srt)")
            elif p == "notes":
                sum_var.set("On")
                if fmt_var.get().startswith("Subtitles"):
                    fmt_var.set("Plain text (.txt)")
            else:
                sum_var.set(self._SCRIVOX_USE_SETTING)
        preset_var.trace_add("write", _apply_preset)
        ttk.Label(frm, text="What do you want?", style="Header.TLabel").pack(
            anchor="w", pady=(14, 6))
        SegmentedControl(frm, preset_var, preset_opts, wraplength=440).pack(
            fill="x")

        mode_var = tk.StringVar(value="audio")
        vis_var = tk.BooleanVar(value=False)
        if any_video:
            vis_toggle = ToggleSwitch(
                frm, vis_var, text="Also describe what's on screen")
            vis_toggle.pack(anchor="w", pady=(12, 0))

            def _vis_to_mode(*_a):
                want = "vision" if vis_var.get() else "audio"
                if mode_var.get() != want:
                    mode_var.set(want)

            def _mode_to_vis(*_a):
                want = mode_var.get() == "vision"
                if vis_var.get() != want:
                    vis_var.set(want)
            vis_var.trace_add("write", _vis_to_mode)
            mode_var.trace_add("write", _mode_to_vis)

        # ---- output format ----
        save_row = ttk.Frame(frm, style="TFrame")
        save_row.pack(fill="x", pady=(14, 0))
        ttk.Label(save_row, text="Save as").pack(side="left")
        ttk.Combobox(save_row, textvariable=fmt_var, state="readonly",
                     width=22,
                     values=list(scrivox_bridge.TRANSCRIBE_FORMATS.keys())
                     ).pack(side="left", padx=8)

        # ---- More settings (collapsed by default) ----
        more_btn = ttk.Button(frm, text="More settings  ▸",
                              style="Toolbar.TButton")
        more_btn.pack(anchor="w", pady=(14, 0))
        adv = ttk.Frame(frm, style="TFrame")
        USE = self._SCRIVOX_USE_SETTING

        # ---- what Scrivox reads (shown whenever combining can happen) ----
        input_var = tk.StringVar(value="mix")
        output_var = tk.StringVar(value="separate")
        combo_var = tk.StringVar(value="auto")
        if any_combinable:
            cbox = ttk.Frame(adv, style="TFrame")
            cbox.pack(fill="x")
            combined_text = ("One combined file per recording - every audio "
                             "track merged into one, kept next to the "
                             "recording" if not any_video else
                             "One combined file per recording - every audio "
                             "track + the screen video merged into one video "
                             "file, kept next to the recording")
            ttk.Label(cbox, text="What Scrivox transcribes").pack(
                anchor="w", pady=(10, 0))
            SegmentedControl(cbox, input_var, [
                ("mix", combined_text),
                ("tracks", "Each audio track separately (per mic/playback)"),
            ], wraplength=440).pack(fill="x", pady=(4, 0))

            # Reuse policy for the combined file. In per-track mode it only
            # matters for the screen-description pass, so it hides unless
            # that pass will actually run.
            reuse_row = ttk.Frame(cbox, style="TFrame")
            ttk.Label(reuse_row,
                      text="If a combined file was already made with this app"
                      ).pack(anchor="w", pady=(10, 0))
            SegmentedControl(reuse_row, combo_var, [
                ("auto", ("Use it - only build one if it's missing or "
                          "older than the tracks")),
                ("rebuild", "Build a fresh one now"),
            ], wraplength=440).pack(fill="x", pady=(4, 0))

            out_row = ttk.Frame(cbox, style="TFrame")
            ttk.Label(out_row, text="Per-track results").pack(
                anchor="w", pady=(10, 0))
            SegmentedControl(out_row, output_var, [
                ("separate", "A transcript file per track"),
                ("merged", ("One combined file: screen descriptions + "
                            "every track's transcript")),
            ], wraplength=440).pack(fill="x", pady=(4, 0))

            def _relayout(*_a):
                per_track = input_var.get() == "tracks"
                # Repack in a fixed order so the rows never swap positions:
                # [what Scrivox transcribes] -> reuse -> per-track.
                reuse_row.pack_forget()
                out_row.pack_forget()
                if not per_track or mode_var.get() == "vision":
                    reuse_row.pack(fill="x")
                if per_track:
                    out_row.pack(fill="x")
                win.geometry("")  # re-fit the dialog to its content
            input_var.trace_add("write", _relayout)
            mode_var.trace_add("write", _relayout)
            _relayout()

        r1 = ttk.Frame(adv, style="TFrame")
        r1.pack(fill="x", pady=(10, 0))
        ttk.Label(r1, text="Identify speakers").pack(side="left")
        dia_var = tk.StringVar(value=USE)
        ttk.Combobox(r1, textvariable=dia_var, state="readonly", width=18,
                     values=[USE, "On", "Off"]).pack(side="left", padx=8)
        ttk.Label(r1, text="How many").pack(side="left", padx=(8, 0))
        spk_var = tk.StringVar(value="")
        ttk.Spinbox(r1, from_=1, to=20, width=4,
                    textvariable=spk_var).pack(side="left", padx=4)
        ttk.Label(r1, text="blank = auto", style="Muted.TLabel").pack(
            side="left", padx=4)

        r2 = ttk.Frame(adv, style="TFrame")
        r2.pack(fill="x", pady=(6, 0))
        ttk.Label(r2, text="Describe the screen every").pack(side="left")
        vi_var = tk.StringVar(value="")
        vi_sb = ttk.Spinbox(r2, from_=1, to=3600, width=6,
                            textvariable=vi_var)
        vi_sb.pack(side="left", padx=4)
        ttk.Label(r2, text="seconds  (blank = Scrivox setting)",
                  style="Muted.TLabel").pack(side="left", padx=4)
        if any_video:
            # Entering an interval IS asking for screen descriptions - flip
            # the capture mode on so the value can never be silently ignored
            # (a filled interval with descriptions off burned a real user).
            def _interval_implies_vision(*_a):
                if vi_var.get().strip() and mode_var.get() != "vision":
                    mode_var.set("vision")
            vi_var.trace_add("write", _interval_implies_vision)
        else:
            vi_sb.config(state="disabled")

        r3 = ttk.Frame(adv, style="TFrame")
        r3.pack(fill="x", pady=(6, 0))
        ttk.Label(r3, text="Model").pack(side="left")
        model_var = tk.StringVar(value=USE)
        ttk.Combobox(r3, textvariable=model_var, state="readonly", width=18,
                     values=[USE, "large-v3", "large-v3-turbo", "medium",
                             "small", "base", "tiny"]).pack(side="left", padx=8)
        ttk.Label(r3, text="Language").pack(side="left", padx=(8, 0))
        lang_var = tk.StringVar(value="")
        ttk.Entry(r3, textvariable=lang_var, width=6).pack(side="left", padx=4)
        ttk.Label(r3, text="e.g. en, ko - blank = auto",
                  style="Muted.TLabel").pack(side="left", padx=4)

        r4 = ttk.Frame(adv, style="TFrame")
        r4.pack(fill="x", pady=(6, 0))
        ttk.Label(r4, text="Meeting summary").pack(side="left")
        sum_var = tk.StringVar(value=USE)
        ttk.Combobox(r4, textvariable=sum_var, state="readonly", width=18,
                     values=[USE, "On", "Off"]).pack(side="left", padx=8)

        adv_open = {"on": False}

        def _toggle_adv():
            adv_open["on"] = not adv_open["on"]
            if adv_open["on"]:
                more_btn.config(text="More settings  ▾")
                adv.pack(fill="x", after=more_btn)
            else:
                more_btn.config(text="More settings  ▸")
                adv.pack_forget()
            win.geometry("")
        more_btn.config(command=_toggle_adv)

        ttk.Label(frm, style="Muted.TLabel", justify="left",
                  wraplength=int(440 * s),
                  text="Each transcript is saved next to its recording. "
                  "Anything left on 'Use Scrivox setting' (and the API keys) "
                  "comes from Scrivox - use the button below to change those."
                  ).pack(anchor="w", pady=(14, 0))

        btns = ttk.Frame(frm, style="TFrame")
        btns.pack(fill="x", pady=(16, 0))

        def open_settings():
            if not scrivox_bridge.open_scrivox(self._scrivox_exe):
                self._notice("Scrivox", "Scrivox could not be started.",
                             kind="error", parent=win)

        ttk.Button(btns, text="Open Scrivox settings", style="Toolbar.TButton",
                   command=open_settings).pack(side="left")

        def _num(var, cast):
            s_ = var.get().strip()
            try:
                return cast(s_) if s_ else None
            except ValueError:
                return None

        def ok():
            fmt, ext = scrivox_bridge.TRANSCRIBE_FORMATS[fmt_var.get()]
            opts = scrivox_bridge.default_options()
            opts["vision"] = (mode_var.get() == "vision")
            opts["fmt"], opts["ext"] = fmt, ext
            opts["input_mode"] = input_var.get()
            opts["merge"] = (output_var.get() == "merged")
            if (opts["input_mode"] == "tracks" and opts["merge"]
                    and fmt not in ("txt", "md")):
                self._notice(
                    "Combined file needs a text format",
                    "One combined file only works for Plain text or Markdown. "
                    "Pick one of those formats, or keep a file per track.",
                    kind="warning", parent=win)
                return
            opts["use_precombined"] = (combo_var.get() == "auto")
            # The interval only exists for vision; a stale value alongside
            # vision=False in the log reads like a contradiction.
            opts["vision_interval"] = (_num(vi_var, float)
                                       if opts["vision"] else None)
            dia = dia_var.get()
            opts["diarize"] = (True if dia == "On"
                               else False if dia == "Off" else None)
            if opts["diarize"]:
                opts["num_speakers"] = _num(spk_var, int)
            m = model_var.get()
            opts["model"] = None if m == USE else m
            opts["language"] = lang_var.get().strip() or None
            sm = sum_var.get()
            opts["summarize"] = (True if sm == "On"
                                 else False if sm == "Off" else None)
            result["value"] = opts
            win.destroy()

        go_btn = ttk.Button(btns, text="Transcribe", style="Accent.TButton",
                            command=ok)
        go_btn.pack(side="right")
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(
            side="right", padx=(0, 8))

        self._finish_dialog(win, ok, focus=go_btn)
        win.wait_window()
        return result["value"]

    def _convert_selected_library(self):
        """Convert the ticked recording(s) to another format via a dialog.
        Each recording becomes its own output file; several run back to back."""
        sel = self._selected_library_entries()
        if not sel:
            self._notice("Convert", "Select at least one recording first.")
            return
        n_audio = max(len([a for a in e.get("audio", []) if a]) for e in sel)
        has_video = any(e.get("video") for e in sel)
        choice = self._convert_dialog(n_audio, has_video, n_entries=len(sel))
        if not choice:
            return
        fmt_label, audio_mode = choice
        ext = combine.CONVERT_FORMATS[fmt_label][0]
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        for entry in sel:
            out_dir = entry.get("out_dir") or self.cfg.resolved_save_folder()
            out = self._unique_path(
                os.path.join(out_dir,
                             f"{entry['name']}_converted_{stamp}.{ext}"))
            self._run_combine(
                lambda e=entry, o=out: combine.convert(e, o, fmt_label,
                                                       audio_mode), out)

    def _convert_dialog(self, n_audio, has_video, n_entries=1):
        """Modal dialog to pick an output format and audio handling.
        Returns (fmt_label, audio_mode) or None if cancelled."""
        win = self._modal_dialog(
            "Convert recording" + ("" if n_entries == 1 else "s"))
        result = {"value": None}
        frm = ttk.Frame(win, style="TFrame", padding=(24, 20, 24, 16))
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Convert to", style="Section.TLabel").pack(
            anchor="w")
        fmt_var = tk.StringVar(value="MP4 (H.264 + AAC)")
        labels = list(combine.CONVERT_FORMATS.keys())
        fmt_combo = ttk.Combobox(frm, textvariable=fmt_var, values=labels,
                                 state="readonly", width=34)
        fmt_combo.pack(anchor="w", pady=(4, 10))

        ttk.Label(frm, text="Audio", style="Header.TLabel").pack(
            anchor="w")
        mode_var = tk.StringVar(value="mix")
        SegmentedControl(frm, mode_var, [
            ("mix", "Mix all audio into one stereo track"),
            ("tracks", "Keep each audio source as its own track"),
        ], wraplength=360).pack(fill="x", pady=(4, 8))

        info = ttk.Label(frm, style="Muted.TLabel", justify="left",
                         wraplength=int(360 * self._s))
        info.pack(anchor="w", pady=(0, 10))

        def describe(*_):
            ext, has_v, _ac, _va = combine.CONVERT_FORMATS[fmt_var.get()]
            if has_v and has_video:
                txt = f"Output: one .{ext} video with the audio included."
            elif has_v and not has_video:
                txt = (f".{ext} is a video format but this recording has no "
                       "video, so an audio-only file will be made.")
            else:
                txt = f"Output: one .{ext} audio file (video is ignored)."
            if n_audio < 2:
                txt += "  (Only one audio track, so the audio handling choice " \
                       "has no effect.)"
            elif mode_var.get() == "tracks" and ext == "mp3" and n_audio > 2:
                txt += ("  Note: MP3 holds at most 2 channels - with "
                        f"{n_audio} sources pick 'Mix' or another format.")
            info.config(text=txt)
        fmt_var.trace_add("write", describe)
        mode_var.trace_add("write", describe)
        describe()

        btns = ttk.Frame(frm, style="TFrame")
        btns.pack(fill="x", pady=(8, 0))

        def ok():
            result["value"] = (fmt_var.get(), mode_var.get())
            win.destroy()

        go_btn = ttk.Button(btns, text="Convert", style="Accent.TButton",
                            command=ok)
        go_btn.pack(side="right")
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(
            side="right", padx=(0, 8))

        self._finish_dialog(win, ok, focus=fmt_combo)
        win.wait_window()
        return result["value"]

    def _open_selected_library(self):
        sel = self._selected_library_entries()
        if not sel:
            # Nothing selected: open the recordings folder itself.
            self._open_folder(self.cfg.resolved_save_folder())
            return
        self._open_entry_folder(sel[0])

    def _rename_selected_library(self):
        sel = self._selected_library_entries()
        if sel:
            self._rename_entry(sel[0])
        return "break"

    def _remove_selected_library(self):
        sel = self._selected_library_entries()
        if not sel:
            return
        n = len(sel)
        if not self._confirm(
                "Remove from the list?",
                f"Remove {ux.plural(n, 'recording')} from this list?\n\n"
                "This does NOT delete the files on disk - they stay in their "
                "folder" + ("s" if n > 1 else "") + ".",
                yes="Remove from list", no="Cancel"):
            return
        sel_ids = {e["id"] for e in sel}
        self._library = [e for e in self._library if e["id"] not in sel_ids]
        self.cfg.set("recordings", self._library)
        self._refresh_library()

    def _show_library_menu(self, event, entry):
        menu = tk.Menu(self, tearoff=0, bg=COLORS["panel2"], fg=COLORS["fg"],
                       activebackground=COLORS["accent"], activeforeground="#06120f",
                       disabledforeground="#6b717c", bd=0)
        menu.add_command(label="Open folder",
                         command=lambda: self._open_entry_folder(entry))
        menu.add_command(label="Play", command=lambda: self._play_entry(entry))
        menu.add_command(label="Show in folder",
                         command=lambda: self._reveal_entry_folder(entry))
        menu.add_separator()
        menu.add_command(label="Rename...", accelerator="F2",
                         command=lambda: self._rename_entry(entry))
        menu.add_command(label="Convert...",
                         command=self._convert_selected_library)
        if self._scrivox_exe:
            menu.add_command(
                label="Transcribe with Scrivox...",
                command=lambda: self._transcribe_entries([entry]))
        menu.add_separator()
        menu.add_command(label="Select all", accelerator="Ctrl+A",
                         command=self._select_all_library)
        menu.add_command(label="Remove from list...", accelerator="Del",
                         command=self._remove_selected_library)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _play_entry(self, entry):
        """Open the recording in the default player: the screen video when
        there is one, else the first audio track."""
        target = entry.get("video") or next(
            (a for a in entry.get("audio") or [] if os.path.isfile(a)), "")
        if not target or not self._open_path(target):
            self._notice("Can't play this recording",
                         "Its files are no longer where they were saved.",
                         kind="warning")
            self._refresh_library()

    def _open_entry_folder(self, entry):
        d = entry.get("out_dir") or ""
        if not d or not os.path.isdir(d):
            self._notice("Folder not found",
                         "This recording's folder no longer exists.",
                         kind="warning")
            self._refresh_library()
            return
        self._open_path(d)

    def _reveal_entry_folder(self, entry):
        """Show the recording's folder highlighted in the file manager."""
        d = entry.get("out_dir") or ""
        if not d or not os.path.isdir(d):
            self._notice("Show folder location",
                                "This recording's folder no longer exists.")
            self._refresh_library()
            return
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(d)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", d])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(d) or d])
        except Exception as e:
            log.warning("reveal folder failed: %s", e)
            self._open_path(os.path.dirname(d) or d)

    def _rename_entry(self, entry):
        """Rename a recording's folder AND every file inside it to match, so the
        folder and its tracks share one name. Keeps all library-tracked paths
        pointing at the renamed files. Non-destructive otherwise."""
        if getattr(self, "_combine_busy", False):
            self._notice(
                "Please wait",
                "A merge/convert is running - rename when it finishes so its "
                "output isn't pulled out from under it.")
            return
        if getattr(self, "_transcribe_busy", False):
            self._notice(
                "Please wait",
                "A transcription is running - rename when it finishes so its "
                "transcript isn't written into a folder that no longer exists.")
            return
        old_dir = entry.get("out_dir") or ""
        if not old_dir or not os.path.isdir(old_dir):
            self._notice("Rename", "This recording's folder no longer exists.")
            self._refresh_library()
            return
        parent_dir = os.path.dirname(old_dir)
        old_base = os.path.basename(old_dir)
        shown = ux.friendly_recording_name(old_base)
        if shown != old_base:
            # An automatic name: the list shows it as a date, so don't
            # prefill the raw 'SRR_2026-...' folder name.
            prompt = (f"Currently '{shown}'. Type a name for this "
                      "recording - its folder and every track inside it "
                      "are renamed to match:")
            initial = ""
        else:
            prompt = "New name for the folder and every track inside it:"
            initial = old_base
        new_name = self._ask_text("Rename recording", prompt,
                                  initial=initial, ok_text="Rename")
        if not new_name:
            return
        # Sanitize to a safe, cross-platform name (no reserved chars).
        safe = "".join(c for c in new_name if c.isalnum() or c in " -_.()").strip()
        safe = safe.rstrip(". ")  # Windows dislikes trailing dot/space
        while "  " in safe:
            safe = safe.replace("  ", " ")
        if not safe:
            self._notice(
                "Rename", "That name has no usable characters - use letters, "
                "numbers, spaces, - _ . ( ).")
            return
        if safe.split(".")[0].upper() in (
                "CON", "PRN", "AUX", "NUL",
                "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8",
                "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7",
                "LPT8", "LPT9"):
            self._notice("Rename",
                                f"'{safe}' is a reserved name on Windows - "
                                "pick another.")
            return
        if safe == old_base:
            return
        new_dir = os.path.join(parent_dir, safe)
        case_only = safe.lower() == old_base.lower()
        if os.path.exists(new_dir) and not case_only:
            self._error("Rename", f"A folder named '{safe}' already exists.")
            return
        try:
            if case_only:
                # NTFS is case-insensitive: go through a temp name so a
                # capitalization fix ("daytona" -> "Daytona") works.
                tmp = new_dir + ".renaming-tmp"
                os.rename(old_dir, tmp)
                os.rename(tmp, new_dir)
            else:
                os.rename(old_dir, new_dir)
        except Exception as e:
            self._error("Rename failed",
                                 f"Could not rename the folder:\n{e}")
            return

        # Rename every file inside so its name matches the new folder name,
        # preserving the descriptive suffix/extension. Handles ALL of this app's
        # naming conventions:
        #   <base>_mic-1.wav, <base>_playback-1.wav, <base>_part2.wav,
        #   <base>_channels.wav, <base>_mix.wav, <base>_screen.mkv,
        #   <base>_converted_<stamp>.<ext>  (start with the old base)
        #   SRR_merged_<stamp>.<ext>, SRR_multitrack_<stamp>.wav,
        #   SRR_mixed_<stamp>.wav        (aggregate exports - swap the SRR token)
        name_map = {}  # old filename -> new filename (within new_dir)

        def new_filename(fname):
            stem, ext = os.path.splitext(fname)
            if stem == old_base:
                return safe + ext
            if stem.startswith(old_base + "_"):
                return safe + stem[len(old_base):] + ext
            for tok in ("SRR_merged", "SRR_multitrack", "SRR_mixed"):
                if stem == tok or stem.startswith(tok + "_") or stem.startswith(tok):
                    return safe + stem[len("SRR"):] + ext
            return None  # leave anything else untouched

        failures = []
        try:
            for fname in os.listdir(new_dir):
                full = os.path.join(new_dir, fname)
                if not os.path.isfile(full):
                    continue
                nf = new_filename(fname)
                if not nf or nf == fname:
                    continue
                target = os.path.join(new_dir, nf)
                file_case_only = nf.lower() == fname.lower()
                if os.path.exists(target) and not file_case_only:
                    continue  # never clobber an existing file
                try:
                    if file_case_only:
                        tmp = target + ".renaming-tmp"
                        os.rename(full, tmp)
                        os.rename(tmp, target)
                    else:
                        os.rename(full, target)
                    name_map[fname] = nf
                except Exception as e:
                    log.warning("Could not rename '%s' -> '%s': %s", fname, nf, e)
                    failures.append(fname)
        except Exception as e:
            log.warning("Rename pass over folder failed: %s", e)
        if failures:
            self._notice(
                "Some files kept their old names",
                "The folder was renamed, but these files are open in another "
                "program and kept their old names:\n\n  "
                + "\n  ".join(failures[:8])
                + ("\n  ..." if len(failures) > 8 else "")
                + "\n\nClose the program using them and rename again.",
                kind="warning")

        # Remap tracked paths: move into new_dir and apply the filename map.
        def remap(p):
            if not p:
                return p
            base = os.path.basename(p)
            base = name_map.get(base, base)
            return os.path.join(new_dir, base)

        entry["out_dir"] = new_dir
        entry["name"] = safe
        entry["audio"] = [remap(a) for a in entry.get("audio", [])]
        if entry.get("video"):
            entry["video"] = remap(entry["video"])
        # Every screen segment moves with the folder too - leaving these on
        # the old path silently dropped restart segments from later combines.
        if entry.get("video_segments"):
            entry["video_segments"] = [remap(v) for v in entry["video_segments"]]
        self._lib_meta.pop(entry.get("id"), None)
        self.cfg.set("recordings", self._library)
        self._refresh_library()
        log.info("Renamed recording '%s' -> '%s' (%d file(s) renamed)",
                 old_base, safe, len(name_map))

    def _build_log(self, parent):
        """The activity log is a diagnostic, so it lives in a drawer that is
        closed by default (Ctrl+L), with a count of new warnings."""
        head = ttk.Frame(parent, style="TFrame")
        head.pack(fill="x")
        self.log_toggle_btn = ttk.Button(head, style="Toolbar.TButton",
                                         command=self._toggle_log)
        self.log_toggle_btn.pack(side="left")
        Tooltip(self.log_toggle_btn, "Show or hide the activity log (Ctrl+L).")
        self.log_body = ttk.Frame(parent, style="TFrame")
        self.log_text = tk.Text(self.log_body, height=8, bg="#101216",
                                fg="#d0d0d0", insertbackground="#d0d0d0",
                                relief="flat", wrap="word", padx=8, pady=6,
                                font=("Consolas", 9), highlightthickness=1,
                                highlightbackground=COLORS["border"])
        sb = ttk.Scrollbar(self.log_body, orient="vertical",
                           command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set, state="disabled")
        sb.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        for tag, col in (("ERROR", COLORS["red"]), ("WARNING", COLORS["gold"]),
                         ("INFO", COLORS["fg"])):
            self.log_text.tag_config(tag, foreground=col)
        self._apply_log_visibility()

    def _toggle_log(self):
        self.log_open_var.set(not self.log_open_var.get())
        self.cfg.set("log_open", bool(self.log_open_var.get()))
        self._apply_log_visibility()

    def _apply_log_visibility(self):
        if self.log_open_var.get():
            self._log_unseen = 0
            if not self.log_body.winfo_manager():
                self.log_body.pack(fill="both", expand=True, pady=(6, 0))
            self.log_toggle_btn.config(text="▾  Activity log")
            self.log_text.see("end")
        else:
            self.log_body.pack_forget()
            extra = (f"  ({ux.plural(self._log_unseen, 'new warning')})"
                     if self._log_unseen else "")
            self.log_toggle_btn.config(text="▸  Activity log" + extra)

    def _restore_from_config(self):
        saved = self.cfg.get("audio_sources") or []
        pool = (self.inputs, self.outputs)
        any_added = False
        self._unresolved_sources = []
        for sel in saved:
            if resolve_selection(sel, devices=pool):
                self._add_row(preset=sel)
                any_added = True
            else:
                # Unplugged right now, not gone: keep it so its selection,
                # gain, and mute survive until it's plugged back in.
                self._unresolved_sources.append(dict(sel))
                log.info("Saved device not present now (kept in config): %s",
                         sel.get("name"))
        if not any_added:
            di, do = default_devices(devices=pool)
            for d, kind in ((di, "input"), (do, "loopback")):
                if d:
                    self._add_row(preset={"name": d["name"], "kind": kind,
                                          "hostapi": d["hostapi"]})
        self._check_duplicate_rows()

    def _request_save(self, delay=400):
        """Debounced save; coalesces rapid changes (e.g. dragging a fader)."""
        if self._save_job:
            try:
                self.after_cancel(self._save_job)
            except Exception:
                pass
        self._save_job = self.after(delay, self._save_settings)

    def _save_settings(self):
        self._save_job = None
        if not getattr(self, "_ui_ready", True):
            return  # still building/restoring - saving now would wipe config
        sels, gains, mutes = [], {}, {}
        for row in self._device_rows:
            d = row.get_selection()
            if d:
                sels.append({"id": d.get("id", ""), "name": d["name"],
                             "kind": d["kind"], "hostapi": d["hostapi"]})
                key = f'{d["name"]}|{d["kind"]}'
                gains[key] = round(row.get_gain(), 3)
                # Hotkey (PTT) mutes are transient - never persist them.
                mutes[key] = row.is_muted() and not getattr(
                    row, "_hotkey_muted", False)
        # A device that's merely unplugged right now must not be erased from
        # the config - keep its selection, gain, and mute until the user
        # removes it on purpose.
        saved_gains = self.cfg.get("audio_gains") or {}
        saved_mutes = self.cfg.get("audio_mutes") or {}
        present = {f'{s["name"]}|{s["kind"]}' for s in sels}
        for sel in self._unresolved_sources:
            key = f'{sel.get("name")}|{sel.get("kind")}'
            if key in present:
                continue
            sels.append(sel)
            if key in saved_gains:
                gains[key] = saved_gains[key]
            if key in saved_mutes:
                mutes[key] = saved_mutes[key]
        self.cfg.update({
            "audio_sources": sels,
            "audio_gains": gains,
            "audio_mutes": mutes,
            "live_levels": self.live_levels_var.get(),
            "audio_output_mode": self.output_mode.get(),
            "audio_subtype": self.subtype.get(),
            "screen_enabled": self.screen_enabled.get(),
            "screen_monitor": self._selected_monitor_number(),
            "screen_encoder": self.encoder_var.get(),
            "screen_container": self.container_var.get(),
            "screen_codec": self.codec_var.get(),
            "screen_framerate": self._fps(),
            "screen_quality": self.quality_var.get(),
            "screen_reliability": self.reliability_var.get(),
            "save_folder": self.folder_var.get(),
            "ask_every_time": self.ask_var.get(),
            "on_stop_action": self.on_stop_var.get(),
            "auto_restart": self.autorestart_var.get(),
            "alert_sound": self.sound_var.get(),
            "alert_banner": self.banner_var.get(),
            "alert_taskbar_flash": self.taskbar_var.get(),
            "alert_messagebox": self.msgbox_var.get(),
            "watchdog_enabled": self.watchdog_var.get(),
            "tray_enabled": self.tray_var.get(),
            "ptt_enabled": self.ptt_enabled_var.get(),
            "ptt_hotkey": self.ptt_hotkey_var.get().strip(),
            "ptt_target": self.ptt_target_var.get(),
            "ptt_mode": self.ptt_mode_var.get(),
            "scrivox_path": self.scrivox_path_var.get().strip(),
        })

    def _fps(self):
        """Screen FPS, tolerant of a blank/partial Spinbox (IntVar.get raises
        TclError on non-integer text, which would otherwise kill the autosave
        trace or the screen start)."""
        try:
            v = int(self.fps_var.get())
        except Exception:
            return 30
        return max(1, min(120, v))

    # ------------------------------------------------------------- devices #
    def _gain_for(self, preset):
        if not preset:
            return 1.0
        gains = self.cfg.get("audio_gains") or {}
        return float(gains.get(f'{preset.get("name")}|{preset.get("kind")}', 1.0))

    def _muted_for(self, preset):
        if not preset:
            return False
        mutes = self.cfg.get("audio_mutes") or {}
        return bool(mutes.get(f'{preset.get("name")}|{preset.get("kind")}', False))

    def _add_row(self, preset=None):
        if preset is None:
            # '+ Add device': the next device that isn't already in the list
            # (it used to clone row 1, which the recorder then silently
            # dropped as a duplicate).
            d = ux.next_unused_device(self.all_devices,
                                      self._used_device_ids())
            if d is None:
                self._notice("Every device is already added",
                             "All microphones and speakers this computer "
                             "reports are already in the list. Plug in "
                             "another one and press Refresh devices.")
                return None
            preset = {"id": d["id"], "name": d["name"], "kind": d["kind"],
                      "hostapi": d["hostapi"]}
        row = DeviceRow(self.rows_frame, self.all_devices, self._remove_row,
                        on_change=self._on_row_change, preset=preset,
                        gain=self._gain_for(preset), muted=self._muted_for(preset))
        row.pack(fill="x", pady=(0, 8))
        self._device_rows.append(row)
        row.combo.bind("<<ComboboxSelected>>",
                       lambda e, r=row: self._on_row_change("select", r))
        if self.recording or self._starting:
            row.set_editable(False)
        self._save_settings()
        self._check_duplicate_rows()
        return row

    def _used_device_ids(self):
        ids = set()
        for r in self._device_rows:
            d = r.get_selection()
            if d:
                ids.add(d.get("id"))
        return ids

    def _check_duplicate_rows(self):
        """Flag rows that pick a device another row already records - they
        are skipped when recording, and now the user can see that."""
        seen = set()
        for r in self._device_rows:
            d = r.get_selection()
            key = (d.get("id"), d.get("kind")) if d else None
            if key and key in seen:
                r.set_warning("Already added above - this row is skipped. "
                              "Pick another device or remove it.")
            else:
                r.set_warning("")
            if key:
                seen.add(key)
        if not self.recording and not self._finalizing and not self._starting \
                and not self._combine_busy and not self._transcribe_busy:
            self.status_lbl.config(text=self._idle_text())

    def _on_row_change(self, what, row):
        if what == "gain":
            label = row.current_source_label()
            g = row.get_gain()
            if label:
                self._set_pending_level(label, gain=g)
                if self.recording and self.audio_rec:
                    self.audio_rec.set_gain(label, g)
                elif self.level_monitor:
                    self.level_monitor.set_gain(label, g)
            self._request_save()
        elif what == "mute":
            label = row.current_source_label()
            m = row.is_muted()
            if label:
                self._set_pending_level(label, muted=m)
                if self.recording and self.audio_rec:
                    self.audio_rec.set_muted(label, m)
                if self.level_monitor:
                    self.level_monitor.set_muted(label, m)
            self._save_settings()
        else:
            if self.recording or self._starting:
                return  # device choice is locked during a take
            self._save_settings()
            self._check_duplicate_rows()
            self._refresh_monitor()

    def _set_pending_level(self, label, gain=None, muted=None):
        """Apply a Mute/Volume change to sources a worker is still opening
        (the recorder reads these objects, so it takes effect at once)."""
        for sources in self._pending_sources:
            for src in sources:
                if src.label != label:
                    continue
                if gain is not None:
                    src.gain = float(gain)
                if muted is not None:
                    src.muted = bool(muted)

    def _drop_pending(self, sources):
        self._pending_sources = [p for p in self._pending_sources
                                 if p is not sources]

    def _apply_row_levels(self, rec):
        """Push every card's current Mute and Volume into a recorder that
        just finished opening - whatever the user (or the push-to-talk key)
        changed while it was starting wins over the values it started with."""
        if rec is None:
            return
        for row in self._device_rows:
            label = row.current_source_label()
            if not label:
                continue
            try:
                rec.set_gain(label, row.get_gain())
                rec.set_muted(label, row.is_muted())
            except Exception:
                log.debug("re-applying levels failed", exc_info=True)

    def _remove_row(self, row):
        if self.recording or self._starting:
            return  # the take keeps the devices it started with
        if row in self._device_rows:
            self._device_rows.remove(row)
        row.destroy()
        self._save_settings()
        self._check_duplicate_rows()
        self._refresh_monitor()

    def _add_default_mic(self):
        di, _ = default_devices(devices=(self.inputs, self.outputs))
        if not di:
            self._notice("No microphone found",
                         "Windows doesn't report any microphone. Plug one in "
                         "and press Refresh devices.", kind="warning")
        elif di.get("id") in self._used_device_ids():
            self._set_status_note(f"'{di['name']}' is already in the list.")
        else:
            self._add_row(preset={"id": di["id"], "name": di["name"],
                                  "kind": "input", "hostapi": di["hostapi"]})

    def _add_system_playback(self):
        _, do = default_devices(devices=(self.inputs, self.outputs))
        if not do:
            self._notice("No speakers found",
                         "Windows doesn't report any speakers or headphones "
                         "to record from.", kind="warning")
        elif do.get("id") in self._used_device_ids():
            self._set_status_note(f"'{do['name']}' is already in the list.")
        else:
            self._add_row(preset={"id": do["id"], "name": do["name"],
                                  "kind": "loopback", "hostapi": do["hostapi"]})

    def _refresh_devices(self):
        if self.recording or self._starting:
            return
        self.inputs, self.outputs = list_devices()
        self.all_devices = self.inputs + self.outputs
        sels = [r.get_selection() for r in list(self._device_rows)]
        for r in list(self._device_rows):
            self._remove_row(r)
        for d in sels:
            if d:
                self._add_row(preset={"name": d["name"], "kind": d["kind"],
                                      "hostapi": d["hostapi"]})
        self._refresh_monitor()
        log.info("Devices refreshed.")
        self._set_status_note(
            f"Found {ux.plural(len(self.inputs), 'microphone')} and "
            f"{ux.plural(len(self.outputs), 'speaker')}.")

    def _toggle_live_levels(self):
        self._save_settings()
        self._refresh_monitor()

    def _selected_monitor_number(self):
        """Parse the monitor number out of the dropdown label (e.g. '2: 1920x1080')."""
        v = self.monitor_var.get()
        try:
            return int(str(v).split(":")[0].strip())
        except Exception:
            return 1

    def _refresh_monitor_list(self):
        """Populate the monitor dropdown from the live monitor list."""
        try:
            mons = list_monitors()
        except Exception as e:
            log.warning("monitor enumeration failed: %s", e)
            mons = []
        self._monitors = mons
        vals = []
        for m in mons:
            label = f'{m["number"]}: {m["width"]}x{m["height"]}'
            if m.get("primary"):
                label += "  (primary)"
            vals.append(label)
        self.monitor_combo["values"] = vals
        cur = self._selected_monitor_number()
        match = next((v for v in vals if v.split(":")[0].strip() == str(cur)), None)
        if match:
            self.monitor_var.set(match)
        elif vals:
            self.monitor_var.set(vals[0])

    def _identify_screens(self):
        try:
            mons = screenmod.show_identify_overlays(self)
            self._refresh_monitor_list()
            self._set_status_note(f"Found {ux.plural(len(mons), 'screen')}.")
        except Exception as e:
            log.exception("identify screens failed: %s", e)

    def _browse_folder(self, parent=None):
        d = filedialog.askdirectory(
            parent=parent or self, title="Choose where recordings are saved",
            initialdir=self.folder_var.get() or paths.default_recordings_dir())
        if d:
            self.folder_var.set(os.path.normpath(d))
            self._save_settings()
            self._update_saveto()

    def _stop_monitor(self):
        """Stop the idle level meters without blocking the window: joining
        the meter threads takes up to a second per device."""
        mon, self.level_monitor = self.level_monitor, None
        if mon is None:
            return
        mon.running = False

        def _join():
            try:
                mon.stop()
            except Exception:
                log.debug("level monitor stop failed", exc_info=True)
        threading.Thread(target=_join, name="meter-stop", daemon=True).start()

    def _refresh_monitor(self):
        self._stop_monitor()
        if self.recording or not self.live_levels_var.get():
            return
        sources = self._gather_sources()
        if not sources:
            return
        try:
            self.level_monitor = LevelMonitor(sources)
            self.level_monitor.start()
        except Exception as e:
            log.warning("Level monitor failed to start: %s", e)

    # ------------------------------------------------------------ logging #
    def _enqueue_log(self, msg, levelno):
        self._log_queue.put((msg, levelno))

    def _drain_log(self):
        appended = False
        warned = False
        while True:
            try:
                msg, levelno = self._log_queue.get_nowait()
            except queue.Empty:
                break
            appended = True
            tag = "INFO"
            if levelno >= 40:
                tag = "ERROR"
            elif levelno >= 30:
                tag = "WARNING"
            if levelno >= 30 and not self.log_open_var.get():
                self._log_unseen += 1
                warned = True
            self.log_text.configure(state="normal")
            self.log_text.insert("end", msg + "\n", tag)
            if int(self.log_text.index("end-1c").split(".")[0]) > 1000:
                self.log_text.delete("1.0", "200.0")
            self.log_text.configure(state="disabled")
        if appended:
            self.log_text.see("end")
        if warned:
            self._apply_log_visibility()

    # ---------------------------------------------------------- recording #
    def _toggle_record(self):
        # Debounce: a double-click must not stop the take it just started
        # (or start a second one the instant the user stops).
        now = time.monotonic()
        if now - self._toggle_ts < 0.5:
            return
        self._toggle_ts = now
        if self.recording:
            self.stop_recording()
        else:
            self.start_recording()

    def _gather_sources(self):
        """Build CaptureSources with clean, role-based track names.

        Names are like "mic-1", "playback-1", "mic-2" - short, file-safe, and
        meaningful, so output files read as recording-mic-1.wav rather than the
        raw device id. Numbering is per-role so multiple mics stay distinct.
        """
        sources, seen = [], set()
        counts = {"input": 0, "loopback": 0}
        for row in self._device_rows:
            d = row.get_selection()
            if not d:
                continue
            key = (d["id"], d["kind"])
            if key in seen:
                continue
            seen.add(key)
            counts[d["kind"]] = counts.get(d["kind"], 0) + 1
            role = "mic" if d["kind"] == "input" else "playback"
            track_name = f"{role}-{counts[d['kind']]}"
            sources.append(CaptureSource.from_device(
                d, gain=row.get_gain(), track_name=track_name,
                muted=row.is_muted()))
        return sources

    def start_recording(self):
        # Latch against re-entry: the dialogs below pump the Tk event loop, so
        # a double-click / tray click / hotkey could start a second session.
        if self.recording or self._starting or self._finalizing \
                or self._quitting:
            return
        self._starting = True
        try:
            plan = self._prepare_start()
        except BaseException:
            self._starting = False
            raise
        if plan is None:
            self._starting = False
            self._restore_status()
            return
        # Opening devices and spawning ffmpeg (which may try several
        # encoders, ~1.3 s each) happens on a worker so the window never
        # shows "Not Responding" right after Record is pressed.
        self._set_starting_ui(True)
        self._pending_sources.append(plan["sources"])

        def work():
            res = self._start_worker(plan)
            self._safe_after(lambda: self._finish_start(plan, res))
        threading.Thread(target=work, name="start", daemon=True).start()

    def _prepare_start(self):
        """Tk-thread part of starting: checks, dialogs, folders. Returns the
        plan for the worker, or None when the user cancelled."""
        sources = self._gather_sources()
        if not sources and not self.screen_enabled.get():
            self._notice("Nothing to record",
                         "Add at least one audio device, or turn on "
                         "'Record the screen too'.", kind="warning")
            return None
        self._save_settings()
        self._stop_monitor()  # release devices so the recorder owns them

        if self.ask_var.get():
            d = filedialog.askdirectory(
                parent=self, title="Choose where to save this recording",
                initialdir=self.folder_var.get() or
                paths.default_recordings_dir())
            if not d:
                self._refresh_monitor()
                return None
            out_dir = d
        else:
            out_dir = self.cfg.resolved_save_folder()
        # Refuse to start on a near-full disk - the worst time to discover it is
        # mid-recording. Warn (but allow) under 2 GB free.
        try:
            import shutil as _sh
            free_gb = _sh.disk_usage(out_dir if os.path.isdir(out_dir)
                                     else os.path.dirname(out_dir) or ".").free / 1e9
            if free_gb < 0.5:
                self._error(
                    "Not enough disk space",
                    f"Only {free_gb:.1f} GB is free where recordings are "
                    "saved.",
                    "Free up space, or choose another folder in "
                    "Settings > Saving, then press Record again.")
                self._refresh_monitor()
                return None
            if free_gb < 2.0:
                if not self._confirm(
                        "Low disk space",
                        f"Only {free_gb:.1f} GB is free. Audio uses about "
                        "0.7 GB per hour for each device, and screen "
                        "recording much more.",
                        yes="Record anyway", no="Cancel", kind="warning"):
                    self._refresh_monitor()
                    return None
        except OSError as e:
            log.warning("Disk space check skipped: %s", e)

        # ISO-style, file-safe session folder + base name (research-backed:
        # YYYY-MM-DD, no spaces or special chars, sorts chronologically).
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        out_dir = os.path.join(out_dir, f"SRR_{stamp}")
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            head, advice = ux.friendly_error(e)
            self._error("Recording did not start",
                        head or "The recording folder could not be created.",
                        advice or "Choose another folder in Settings > "
                        "Saving, then press Record again.", details=str(e))
            self._refresh_monitor()
            return None
        base = f"SRR_{stamp}"
        self._session_base = base

        # Per-process session dir: with a shared fixed dir, two app instances
        # interleave heartbeats and either one's stop flag kills BOTH
        # watchdogs, leaving the still-recording instance unprotected.
        self.session_dir = os.path.join(paths.data_dir(),
                                        f"session-{os.getpid()}")
        os.makedirs(self.session_dir, exist_ok=True)
        watchdog.clear_alert(self.session_dir)
        # Re-arm alerting and clear any leftover banner from a previous take.
        self.alerting = False
        try:
            if self.banner.winfo_manager():
                self.banner.stop()
        except tk.TclError:
            pass
        self.last_outputs = {"out_dir": out_dir, "audio": [], "video": None}
        ext = self.container_var.get()
        plan = {
            "sources": sources, "out_dir": out_dir, "base": base,
            "mode": self.output_mode.get(), "subtype": self.subtype.get(),
            "samplerate": int(self.cfg.get("audio_target_samplerate")),
            "screen": bool(self.screen_enabled.get()),
            "monitor": self._selected_monitor_number(), "ext": ext,
            "vpath": os.path.join(out_dir, f"{base}_screen.{ext}"),
            "encoder": self.encoder_var.get(), "codec": self.codec_var.get(),
            "fps": self._fps(), "quality": self.quality_var.get(),
            "capture": self.cfg.get("screen_capture_method"),
            "reliability": self.reliability_var.get(),
        }
        if plan["screen"]:
            self.last_outputs["video"] = plan["vpath"]
        return plan

    def _start_worker(self, plan):
        """Worker thread: open the devices and start ffmpeg. Touches no Tk."""
        res = {"audio": None, "audio_err": None, "screen": None,
               "screen_err": None, "fam": None}
        if plan["sources"]:
            rec = None
            try:
                rec = AudioRecorder(
                    plan["sources"], plan["mode"], plan["out_dir"], plan["base"],
                    target_samplerate=plan["samplerate"],
                    subtype=plan["subtype"], on_error=self._on_subsystem_error)
                rec.start()
                res["audio"] = rec
            except Exception as e:
                log.exception("Audio start failed: %s", e)
                # If capture threads were already spawned, shut them down so
                # they don't keep the devices open and write orphan files.
                try:
                    if rec:
                        rec.stop()
                except Exception:
                    log.debug("audio cleanup failed", exc_info=True)
                res["audio_err"] = e
                return res
        if plan["screen"]:
            # The encoder list is probed in the background at startup.
            self._encoders_ready.wait(25)
            try:
                mons = list_monitors()
                num = plan["monitor"]
                mon = next((m for m in mons if m["number"] == num),
                           mons[0] if mons else None)
                if mon is None:
                    raise RuntimeError("No monitor detected.")
                srec = ScreenRecorder(
                    mon, plan["vpath"], encoder_family=plan["encoder"],
                    codec=plan["codec"], container=plan["ext"],
                    framerate=plan["fps"], quality=plan["quality"],
                    capture_method=plan["capture"],
                    on_error=self._on_subsystem_error, available=self.encoders,
                    reliability=plan["reliability"])
                res["fam"] = srec.start()
                res["screen"] = srec
            except Exception as e:
                log.exception("Screen start failed: %s", e)
                res["screen_err"] = str(e)
        return res

    def _finish_start(self, plan, res):
        """Back on the Tk thread: enter the recording state, or explain why
        nothing started."""
        self._starting = False
        self._drop_pending(plan["sources"])
        if self._closing or self._quitting:
            # The user quit while we were starting: finalize whatever opened.
            for rec in (res["audio"], res["screen"]):
                if rec is not None:
                    threading.Thread(target=rec.stop, daemon=True).start()
            return
        if res["audio_err"] is not None:
            e = res["audio_err"]
            self._set_starting_ui(False)
            head, advice = ux.friendly_error(e)
            self._error(
                "Recording did not start",
                head or "The audio devices could not be opened.",
                advice or "Check the devices are plugged in, press Refresh "
                "devices, then press Record again.",
                details=f"{type(e).__name__}: {e}")
            self._refresh_monitor()
            return
        self.audio_rec = res["audio"]
        self.screen_rec = res["screen"]
        self._apply_row_levels(self.audio_rec)
        if self.audio_rec is not None:
            self.last_outputs["audio"] = list(self.audio_rec.output_files)
        if self.screen_rec is not None:
            self.last_outputs["video"] = self.screen_rec.final_path
            log.info("Screen recording via %s", res["fam"])
        screen_error = res["screen_err"]

        if self.audio_rec is None and self.screen_rec is None:
            # NOTHING actually started. Never enter the recording state - a red
            # button over zero capture is the worst possible lie this app can
            # tell. Surface the failure and bail out cleanly.
            try:
                self.banner.stop()
            except tk.TclError:
                pass
            self.alerting = False
            self._set_starting_ui(False)
            head, advice = ux.friendly_error(screen_error)
            self._error(
                "Recording did NOT start",
                head or "Screen recording could not start, and no audio "
                "device is selected - nothing is being recorded.",
                advice or "Add a microphone, or try a different video "
                "encoder in Settings > Recording.",
                details=screen_error or "Unknown error.")
            self._refresh_monitor()
            return

        # Reset liveness tracking BEFORE the heartbeat thread starts, so its
        # first write can never be computed from the previous take's state
        # (that race produced a stale heartbeat the watchdog could alert on).
        self._record_start_mono = time.monotonic()
        self._screen_last_size = -1
        self._screen_last_grow = time.monotonic()
        self._restart_cooldown = {}
        self._restart_counts = {}
        self._take_id += 1
        self.recording = True

        self.heartbeat = watchdog.HeartbeatWriter(self.session_dir,
                                                  self._heartbeat_status)
        self.heartbeat.start()
        if self.watchdog_var.get():
            self.wd_proc = watchdog.spawn_watchdog(
                self.session_dir, os.getpid(),
                stale_seconds=int(self.cfg.get("watchdog_stale_seconds")),
                alert_sound=self.sound_var.get(),
                show_messagebox=self.msgbox_var.get())
        else:
            self.wd_proc = None
            log.info("Background watchdog process disabled in settings.")

        self._set_recording_ui(True)
        log.info("RECORDING STARTED -> %s", plan["out_dir"])
        if screen_error:
            # Raised after recording=True so the auto-restart path can act
            # on a start-time screen failure too (audio is still running).
            self._raise_gold_alert(
                "Screen recording failed to start - audio is still "
                f"recording. ({screen_error.strip()[:160]})")

    def _set_starting_ui(self, on):
        if on:
            self._style_record_btn("starting")
            self.status_lbl.config(text="Starting...")
            self._hide_strip()
            self._set_editing_enabled(False)
        else:
            self._style_record_btn("idle")
            self._set_editing_enabled(True)
            self._restore_status()

    def _set_editing_enabled(self, on):
        """While a take runs, the device/screen choices are locked: changing
        them would only change the config, and the window would show
        something that is not being recorded. Mute and Volume stay live."""
        for row in self._device_rows:
            row.set_editable(on)
        for b in (self.add_dev_btn, self.add_mic_btn, self.add_play_btn,
                  self.dev_refresh_btn, self.ident_btn):
            b.state(["!disabled"] if on else ["disabled"])
        self.screen_toggle.set_enabled(on)
        self.monitor_combo.configure(state="readonly" if on else "disabled")
        self._sync_settings_lock()

    # Seconds after pressing record during which subsystems are still spinning
    # up; no "stopped" alert is raised in this window (prevents a false alarm the
    # instant recording starts, before the first disk write has landed).
    STARTUP_GRACE = 5.0

    def _heartbeat_status(self):
        st = {"recording": True}
        age = time.monotonic() - getattr(self, "_record_start_mono", 0.0)
        warming_up = age < self.STARTUP_GRACE
        st["startup_grace"] = warming_up
        if self.audio_rec:
            a = self.audio_rec.get_status()
            if a["last_write"]:
                secs = time.monotonic() - a["last_write"]
                healthy = bool(a["any_active"] and secs < 3.0)
                st["audio_detail"] = f"{secs:.1f}s since last write"
            else:
                # No write has landed yet. Healthy only while still warming up.
                secs = age
                healthy = False
                st["audio_detail"] = "starting up..."
            # During the grace window always report OK so the watchdog waits.
            st["audio_ok"] = healthy or warming_up
        else:
            st["audio_ok"] = True
        if self.screen_rec:
            s = self.screen_rec.get_status()
            st["screen_enabled"] = True
            # Treat as alive during warm-up so encoder spin-up is not flagged.
            st["screen_alive"] = bool(s["alive"]) or warming_up
            size = s["size"]
            st["screen_size"] = size
            pa = s.get("progress_age", -1.0)
            st["screen_frame"] = s.get("frame", 0)
            st["screen_progress_age"] = pa

            # Liveness uses TWO independent signals, OR'd together, so a single
            # signal's blind spot can never cause a false stall:
            #   1. ffmpeg's machine-readable -progress stream (out_time advancing
            #      on a 1s timer). Authoritative and works for EVERY encoder
            #      (NVENC, QSV, AMF, VideoToolbox, CPU), unlike the human-readable
            #      "frame=" stats that hardware encoders emit rarely.
            #   2. the output file growing on disk - a backstop in case progress
            #      output is ever delayed.
            # Healthy if EITHER advanced within the window. We only flag a stall
            # when both have been quiet, well beyond the 1s progress period.
            now = time.monotonic()
            last_size = getattr(self, "_screen_last_size", -1)
            last_grow = getattr(self, "_screen_last_grow", now)
            if size > last_size:
                last_grow = now
                self._screen_last_size = size
            self._screen_last_grow = last_grow
            size_age = now - last_grow
            # progress_age is stamped at spawn, so -1 means "no live process"
            # - that is NOT healthy (outside warm-up). An ffmpeg that never
            # produces its first frame must trip the stall alarm, not hide.
            progress_ok = (0.0 <= pa < 8.0)
            size_ok = size_age < 12.0
            st["screen_progressing"] = bool(warming_up or progress_ok or size_ok)
        else:
            st["screen_enabled"] = False
        return st

    def stop_recording(self, blocking=False):
        """Stop the take. The fast parts (heartbeat/watchdog teardown) happen
        inline; the slow parts (audio writer flush, ffmpeg 'q' + remux, which
        can take minutes for a long MP4) run on a worker thread so the window
        never goes 'Not responding' right after the user hits STOP - that's
        exactly when a panicked user would End-Task the app mid-finalize.
        blocking=True finalizes synchronously instead."""
        if not self.recording or self._finalizing:
            return
        log.info("Stopping recording...")
        self._last_take_secs = float(int(max(
            0.0, time.monotonic() - self._record_start_mono)))
        self.recording = False
        self._finalizing = True
        if self.heartbeat:
            self.heartbeat.stop()
            self.heartbeat = None
        if self.session_dir:
            watchdog.write_stop_flag(self.session_dir)
        if self.wd_proc:
            try:
                self.wd_proc.terminate()
            except Exception:
                pass
            self.wd_proc = None
        arec, srec = self.audio_rec, self.screen_rec
        self.audio_rec = None
        self.screen_rec = None
        self._set_recording_ui(False)
        # The user must never wonder whether STOP "took": say what's happening
        # on the button itself and keep the busy bar moving until done().
        self._style_record_btn("saving")
        self.status_lbl.config(text="Saving your recording... (don't unplug "
                                    "anything yet)")

        def finalize():
            audio_files, video_path = None, None
            if arec:
                try:
                    audio_files = arec.stop()
                except Exception as e:
                    log.exception("audio stop error: %s", e)
            if srec:
                try:
                    video_path = srec.stop()
                except Exception as e:
                    log.exception("screen stop error: %s", e)
            return audio_files, video_path

        def done(audio_files, video_path):
            # Merge instead of replace: restart segments collected earlier in
            # last_outputs must survive (they used to be silently dropped).
            if audio_files:
                merged = list(self.last_outputs.get("audio") or [])
                for f in audio_files:
                    if f and f not in merged:
                        merged.append(f)
                self.last_outputs["audio"] = merged
            if video_path:
                self.last_outputs["video"] = video_path
            self._finalizing = False
            try:
                self._style_record_btn("idle")
                self._set_editing_enabled(True)
                self._restore_status()
            except tk.TclError:
                pass
            entry = self._add_to_library(select_new=True)
            log.info("RECORDING STOPPED. Outputs: %s", self.last_outputs)
            self._show_take_saved(entry)
            if not self._quitting:
                self._refresh_monitor()
                self._offer_stop_combine()

        if blocking:
            a, v = finalize()
            done(a, v)
        else:
            def work():
                a, v = finalize()
                self._safe_after(lambda: done(a, v))
            threading.Thread(target=work, name="finalize", daemon=True).start()

    def _show_take_saved(self, entry):
        """The 'done' moment: what was saved, how long, how big, where - and
        one click to open, rename or play it."""
        self.elapsed_lbl.config(text=_fmt_elapsed(self._last_take_secs))
        if entry is None:
            self._show_strip("warn", "Nothing was saved",
                             "No audio or video reached the disk for this "
                             "recording. Open the activity log for details.",
                             [("Show log", lambda: (
                                 self.log_open_var.get() or self._toggle_log()))])
            return
        text = self._show_saved_strip(entry)
        if self.tray is not None and self.state() in ("iconic", "withdrawn"):
            icon = getattr(self.tray, "_icon", None)
            try:
                if icon is not None and hasattr(icon, "notify"):
                    icon.notify(f"Recording saved: {text}", APP_TITLE)
            except Exception:
                log.debug("tray notify failed", exc_info=True)
        self._set_status_note("Saved. Press Record (F9) to start another "
                              "recording.", ms=8000)

    def _show_saved_strip(self, entry):
        """'Saved - 2 tracks - 3:12 - 41 MB - Recording 23 Sep 2026, 06:52'
        with Open folder / Rename / Play. Uses the same name as the list."""
        audio = entry.get("audio") or []
        vids = entry.get("video_segments") or ([entry["video"]]
                                               if entry.get("video") else [])
        text = ux.take_summary(len(audio), 1 if vids else 0,
                               self._last_take_secs,
                               ux.total_size(audio + vids))
        name = ux.friendly_recording_name(entry.get("name") or "")
        self._show_strip(
            "ok", "✓ Saved", f"{text}  ·  {name}",
            [("Open folder", lambda: self._open_entry_folder(entry)),
             ("Rename...", lambda: self._rename_from_strip(entry)),
             ("Play", lambda: self._play_entry(entry))])
        return text

    def _rename_from_strip(self, entry):
        before = entry.get("name")
        self._rename_entry(entry)
        if entry.get("name") != before and not self.recording:
            self._show_saved_strip(entry)  # show the new name right away

    def _offer_stop_combine(self):
        """Honor the 'When screen+audio ends' setting: ask / combine /
        separate. Non-destructive - the separate tracks are always kept."""
        action = self.on_stop_var.get()
        if action == "separate":
            return
        segments = library.order_video_segments(
            [v for v in ([self.last_outputs.get("video") or ""]
                         + (self.last_outputs.get("videos_extra") or []))
             if v and os.path.isfile(v)])
        audio = [a for a in (self.last_outputs.get("audio") or [])
                 if a and os.path.isfile(a)]
        if not (segments and audio):
            return
        if action == "ask":
            yes, remember = self._confirm(
                "Make one video with sound?",
                "Your screen and audio were saved as separate files. "
                "Combine them into one video now? The separate files are "
                "kept either way.",
                yes="Make one video", no="Keep separate", default="yes",
                check="Don't ask again (change it in Settings > Saving)")
            if remember:
                self.on_stop_var.set("combine" if yes else "separate")
            if not yes:
                return
        out_dir = (self.last_outputs.get("out_dir")
                   or os.path.dirname(segments[0]))
        base = getattr(self, "_session_base", None) or "SRR"
        # Several segments must re-encode into one container; a single segment
        # keeps its own extension (fast stream-copy in combine_take).
        ext = ("mkv" if len(segments) > 1
               else (os.path.splitext(segments[0])[1].lstrip(".") or "mkv"))
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        out = self._unique_path(
            os.path.join(out_dir, f"{base}_merged_{stamp}.{ext}"))
        self._run_combine(
            lambda: combine.combine_take(segments, audio, out), out)

    def _set_recording_ui(self, on):
        if on:
            self._style_record_btn("recording")
            self.elapsed_lbl.config(text="00:00:00")
            self.status_lbl.config(text=self._recording_status_text())
            self._hide_strip()
            self._set_editing_enabled(False)
        else:
            self._style_record_btn("idle")
            self._restore_status()
            self._idle_lights()
        self._update_title()
        if self.tray:
            self.tray.set_recording(on)
        self._set_taskbar_recording(on)

    def _idle_lights(self):
        if self.recording or not hasattr(self, "screen_light"):
            return
        self.audio_light.set_state(COLORS["muted"], ": ready")
        self.screen_light.set_state(
            COLORS["muted"], ": ready" if self.screen_enabled.get() else ": off")

    def _recording_status_text(self):
        bits = []
        if self.audio_rec is not None:
            n = len(getattr(self.audio_rec, "sources", []) or []) or \
                len(self._gather_sources())
            bits.append(ux.plural(n, "audio source"))
        if self.screen_rec is not None:
            bits.append("the screen")
        what = " + ".join(bits) if bits else "..."
        name = ux.friendly_recording_name(
            getattr(self, "_session_base", "") or "")
        return (f"Recording {what} as '{name}'. Press Stop (F9) when "
                "done.")

    def _update_title(self):
        """The taskbar hover shows the state: '* REC 00:12:04 - ...'."""
        try:
            if self.recording:
                t = _fmt_elapsed(time.monotonic() - self._record_start_mono)
                self.title(f"● REC {t} - {APP_TITLE}")
            else:
                self.title(APP_TITLE)
        except tk.TclError:
            pass

    def _on_subsystem_error(self, label, reason):
        self._log_queue.put((f"SUBSYSTEM ERROR [{label}]: {reason}", 40))
        msg = ux.humanize_subsystem_error(label, reason)
        self._safe_after(lambda: self._raise_gold_alert(msg))

    def _root_hwnd(self):
        """Top-level window handle. winfo_id() on a Tk root is the CHILD hwnd,
        and FlashWindowEx on a child does not flash the taskbar button."""
        try:
            import ctypes
            h = ctypes.windll.user32.GetAncestor(self.winfo_id(), 2)  # GA_ROOT
            return h or self.winfo_id()
        except Exception:
            return self.winfo_id()

    def _raise_gold_alert(self, reason):
        now = time.monotonic()
        first = not self.alerting
        new_reason = reason != self._last_alert_reason
        self.alerting = True
        self._last_alert_reason = reason
        if first or new_reason:
            log.error("GOLD ALERT: %s", reason)
        if self.banner_var.get():
            self.banner.show(reason)
        # Rate-limit the loud channels so a device retry loop (every 0.5s)
        # can't strobe the beep/flash - and so they keep working even when the
        # banner channel is switched off (the old dedup keyed on the banner
        # being visible, which silenced everything else with it).
        if first or new_reason or (now - self._last_alert_fx) > 5.0:
            self._last_alert_fx = now
            if self.sound_var.get():
                alerts.beep()
            if self.taskbar_var.get():
                try:
                    alerts.flash_taskbar(self._root_hwnd())
                except Exception:
                    pass
            if self.autorestart_var.get() and self.recording:
                self.after(500, self._auto_restart_failed)

    def _dismiss_alert(self):
        self.alerting = False
        self._last_alert_reason = ""
        alerts.stop_beep()
        try:
            alerts.stop_flash_taskbar(self._root_hwnd())
        except Exception:
            pass
        if self.session_dir:
            watchdog.clear_alert(self.session_dir)

    # Audio capture already self-heals an unplugged device in place (it keeps
    # the file open, fills the gap with silence, and retries every 0.5s), so
    # tearing the whole subsystem down mustn't repeat forever: each rebuild
    # creates another junk WAV set. Cap the attempts and back off between them.
    _AUDIO_RESTART_CAP = 3

    def _auto_restart_failed(self):
        now = time.monotonic()
        if self.audio_rec is not None:
            a = self.audio_rec.get_status()
            secs = (time.monotonic() - a["last_write"]) if a["last_write"] else 999
            n = self._restart_counts.get("audio", 0)
            cooldown = 8 * (2 ** n)
            if ((not a["any_active"] or secs > 4)
                    and n < self._AUDIO_RESTART_CAP
                    and now - self._restart_cooldown.get("audio", 0) > cooldown):
                self._restart_cooldown["audio"] = now
                self._restart_counts["audio"] = n + 1
                log.warning("Auto-restarting AUDIO subsystem (attempt %d/%d)...",
                            n + 1, self._AUDIO_RESTART_CAP)
                self._restart_audio()
        if self.screen_rec is not None:
            s = self.screen_rec.get_status()
            if not s["alive"] and now - self._restart_cooldown.get("screen", 0) > 8:
                self._restart_cooldown["screen"] = now
                log.warning("Auto-restarting SCREEN subsystem...")
                self._restart_screen()

    def _merge_audio_outputs(self, files):
        """Add finalized file paths into last_outputs['audio'] without dupes."""
        if not files:
            return
        merged = list(self.last_outputs.get("audio") or [])
        for f in files:
            if f and f not in merged:
                merged.append(f)
        self.last_outputs["audio"] = merged

    def _restart_audio(self):
        """Rebuild the audio recorder mid-take. The new recorder opens on a
        worker (device opens can take seconds); the old one is finalized off
        the Tk thread too, so a gold alert never comes with a frozen window."""
        if self._restart_inflight.get("audio"):
            return
        sources = self._gather_sources()
        out_dir = self.last_outputs.get("out_dir")
        if not sources or not out_dir:
            return
        old = self.audio_rec
        self.audio_rec = None
        if old is not None:
            # Keep its finalized files - incl. any 4GiB rollover segments -
            # in last_outputs so the take stays complete.
            def _stop_old_audio():
                try:
                    files = old.stop()
                except Exception:
                    log.exception("old audio recorder stop failed")
                    files = []
                self._safe_after(lambda: self._merge_audio_outputs(files))
            threading.Thread(target=_stop_old_audio,
                             name="audio-restart-stop", daemon=True).start()
        base = self._combine_base() + "_restart-" + datetime.now().strftime("%H%M%S")
        # NOTE: _record_start_mono is deliberately NOT reset here - it is
        # the take's true start; resetting it lied to the elapsed timer
        # and re-armed the watchdog's startup grace mid-recording.
        mode, subtype = self.output_mode.get(), self.subtype.get()
        sr = int(self.cfg.get("audio_target_samplerate"))
        take = self._take_id
        self._restart_inflight["audio"] = True
        self._pending_sources.append(sources)

        def work():
            rec = None
            try:
                rec = AudioRecorder(sources, mode, out_dir, base,
                                    target_samplerate=sr, subtype=subtype,
                                    on_error=self._on_subsystem_error)
                rec.start()
            except Exception as e:
                log.exception("Audio restart failed: %s", e)
                rec = None
            self._safe_after(lambda: self._restart_audio_done(take, rec,
                                                              sources))
        threading.Thread(target=work, name="audio-restart", daemon=True).start()

    def _restart_audio_done(self, take, rec, sources=None):
        self._restart_inflight["audio"] = False
        if sources is not None:
            self._drop_pending(sources)
        if rec is None:
            return
        if not self.recording or take != self._take_id:
            # The take ended while the new recorder was opening: finalize it
            # and keep the files with the take they belong to.
            log.info("Audio restart finished after stop; finalizing it.")

            def _stop():
                try:
                    rec.stop()
                except Exception:
                    log.exception("late audio recorder stop failed")
            threading.Thread(target=_stop, daemon=True).start()
            return
        self.audio_rec = rec
        self._apply_row_levels(rec)
        self.last_outputs.setdefault("audio", []).extend(rec.output_files)
        log.info("Audio subsystem restarted -> %s", rec.output_files)
        self._note_recovered("Audio recording")

    def _restart_screen(self):
        """Restart screen capture mid-take on a worker (the encoder chain can
        take several seconds); see _restart_audio."""
        if self._restart_inflight.get("screen"):
            return
        out_dir = self.last_outputs.get("out_dir")
        if not out_dir:
            return
        old = self.screen_rec
        self.screen_rec = None
        if old is not None:
            # Finalize the dead recorder's file off-thread: for hybrid MP4
            # this runs the remux, so the pre-crash segment stays playable
            # on disk instead of being abandoned as a .recording fragment.
            def _stop_old_screen():
                try:
                    p = old.stop()
                except Exception:
                    log.exception("old screen recorder stop failed")
                    p = None
                if p:
                    self._safe_after(
                        lambda: self.last_outputs.setdefault(
                            "videos_extra", []).append(p))
            threading.Thread(target=_stop_old_screen,
                             name="screen-restart-stop", daemon=True).start()
        ext = self.container_var.get()
        vpath = os.path.join(
            out_dir,
            f"{self._combine_base()}_screen-restart-{datetime.now():%H%M%S}.{ext}")
        kw = {"encoder_family": self.encoder_var.get(),
              "codec": self.codec_var.get(), "container": ext,
              "framerate": self._fps(), "quality": self.quality_var.get(),
              "capture_method": self.cfg.get("screen_capture_method"),
              "on_error": self._on_subsystem_error, "available": self.encoders,
              "reliability": self.reliability_var.get()}
        num = self._selected_monitor_number()
        take = self._take_id
        self._restart_inflight["screen"] = True

        def work():
            rec = None
            try:
                mons = list_monitors()
                mon = next((m for m in mons if m["number"] == num),
                           mons[0] if mons else None)
                if mon is not None:
                    rec = ScreenRecorder(mon, vpath, **kw)
                    rec.start()
            except Exception as e:
                log.exception("Screen restart failed: %s", e)
                rec = None
            self._safe_after(lambda: self._restart_screen_done(take, rec,
                                                               vpath))
        threading.Thread(target=work, name="screen-restart", daemon=True).start()

    def _restart_screen_done(self, take, rec, vpath):
        self._restart_inflight["screen"] = False
        if rec is None:
            return
        if not self.recording or take != self._take_id:
            log.info("Screen restart finished after stop; finalizing it.")

            def _stop():
                try:
                    rec.stop()
                except Exception:
                    log.exception("late screen recorder stop failed")
            threading.Thread(target=_stop, daemon=True).start()
            return
        self.screen_rec = rec
        # Reset growth tracking: the new (smaller) file must not have to
        # out-grow the old one's byte count before it registers as alive.
        self._screen_last_size = -1
        self._screen_last_grow = time.monotonic()
        # Track the new segment so a later combine joins EVERY segment of
        # this take, not just the pre-restart one.
        self.last_outputs.setdefault("videos_extra", []).append(vpath)
        log.info("Screen subsystem restarted -> %s", vpath)
        self._note_recovered("Screen recording")

    def _note_recovered(self, what):
        """The watchdog stopped a stalled subsystem and the app already
        restarted it on its own. Clear the scary alert and say so, instead of
        leaving a flashing RECORDING PROBLEM banner (with a Restart button)
        up for a problem that is already fixed and still recording."""
        self._dismiss_alert()
        log.info("%s auto-recovered; recording continues.", what)
        self._log_queue.put((f"AUTO-RECOVERED: {what} restarted itself; "
                             "recording continues.", 20))
        if self.banner_var.get():
            try:
                self.banner.show_recovered(
                    f"{what} stopped and was restarted automatically - "
                    "still recording. It continues in a new segment; the "
                    "segments are joined when you combine into one file.")
            except Exception:
                pass

    def _restart_recording(self):
        self._dismiss_alert()
        if self.recording:
            self.stop_recording()
        # stop_recording finalizes on a worker thread; start once it's done.
        self._restart_when_ready(time.monotonic() + 30.0)

    def _restart_when_ready(self, deadline):
        if self._finalizing and time.monotonic() < deadline:
            self.after(300, lambda: self._restart_when_ready(deadline))
            return
        self.start_recording()

    # --------------------------------------------------------- poll loops #
    def _poll(self):
        if getattr(self, "_closing", False):
            return
        # Each step gets its own guard: one repeatedly-failing step (e.g. a
        # status-light hiccup) must not silently disable ALERT.json polling -
        # that would kill every alert channel for the rest of the recording.
        try:
            self._drain_log()
        except Exception as e:
            self._poll_err("log", e)
        if self.recording:
            try:
                self._update_status_lights()
            except Exception as e:
                self._poll_err("lights", e)
            try:
                self._check_watchdog_alert()
            except Exception as e:
                self._poll_err("alert", e)
        if not getattr(self, "_closing", False):
            self.after(400, self._poll)

    def _poll_err(self, key, e):
        """Log poll-step failures visibly, throttled to one per 30s per step."""
        now = time.monotonic()
        if now - self._poll_err_ts.get(key, 0.0) > 30.0:
            self._poll_err_ts[key] = now
            log.warning("poll step '%s' failing: %s", key, e)

    def _meter_loop(self):
        if getattr(self, "_closing", False):
            return
        try:
            self._update_meters()
        except Exception as e:
            log.debug("meter error: %s", e)
        if not getattr(self, "_closing", False):
            self.after(70, self._meter_loop)

    def _update_meters(self):
        levels = {}
        if self.recording and self.audio_rec:
            levels = self.audio_rec.get_levels()
        elif self.level_monitor:
            levels = self.level_monitor.get_levels()
        for row in self._device_rows:
            lb = row.current_source_label()
            row.set_level(levels.get(lb, 0.0) if lb else 0.0)

    def _update_status_lights(self):
        # The timer must tick for screen-only takes too - a frozen 00:00:00
        # reads as "not recording" to exactly the users this app is for.
        if self.recording and self._record_start_mono:
            self.elapsed_lbl.config(text=_fmt_elapsed(
                time.monotonic() - self._record_start_mono))
        if self.audio_rec:
            a = self.audio_rec.get_status()
            secs = (time.monotonic() - a["last_write"]) if a["last_write"] else 999
            if a["any_active"] and secs < 3:
                self.audio_light.set_state(COLORS["red"], ": recording")
            else:
                self.audio_light.set_state(COLORS["gold"], ": no sound arriving")
            self.elapsed_lbl.config(text=_fmt_elapsed(a["elapsed"]))
        else:
            self.audio_light.set_state(COLORS["muted"], ": off")
        if self.screen_rec:
            s = self.screen_rec.get_status()
            if s["alive"]:
                self.screen_light.set_state(
                    COLORS["red"], f": recording {ux.fmt_bytes(s['size'])}")
            else:
                self.screen_light.set_state(COLORS["gold"], ": stopped")
        else:
            self.screen_light.set_state(COLORS["muted"], ": off")
        self._update_title()

    def _check_watchdog_alert(self):
        if not self.session_dir:
            return
        alert = watchdog.read_alert(self.session_dir)
        if not alert:
            return
        # Dedup on content + a window, NOT on self.alerting: gating on the
        # latched flag meant one early alert suppressed every later (different)
        # watchdog alert for the rest of the session.
        reason = alert.get("reason", "Recording problem detected.")
        now = time.monotonic()
        if reason == self._last_wd_reason and (now - self._last_wd_time) < 30.0:
            return
        self._last_wd_reason = reason
        self._last_wd_time = now
        self._raise_gold_alert(reason)

    # ----------------------------------------------------------- combine #
    def _combine_base(self):
        return getattr(self, "_session_base", None) or "SRR_recording"

    def _unique_path(self, path):
        """Never silently overwrite an existing export (the stamp is only
        second-granular, so two quick runs can collide). Paths promised to
        still-queued jobs count as taken even though not on disk yet."""
        pending = getattr(self, "_pending_out_paths", set())

        def taken(p):
            return os.path.exists(p) or p in pending

        if not taken(path):
            return path
        stem, ext = os.path.splitext(path)
        for i in range(2, 100):
            cand = f"{stem}_{i}{ext}"
            if not taken(cand):
                return cand
        return path

    def _run_combine(self, fn, out):
        self._pending_out_paths.add(out)
        if getattr(self, "_combine_busy", False):
            # Queue it instead of refusing: several jobs (e.g. converting many
            # ticked recordings) run back to back with one summary at the end.
            self._combine_queue.append((fn, out))
            self._combine_total += 1
            self._set_busy(True, text=self._combine_progress_text())
            return
        if not self._combine_results:
            self._combine_total = 1 + len(self._combine_queue)
        self._combine_busy = True
        self._restore_status()
        self._combine_frac = None
        self._combine_t0 = time.monotonic()
        try:
            self.busy_bar.stop()
            self.busy_bar.config(mode="indeterminate", value=0)
        except (tk.TclError, AttributeError):
            pass
        self._set_busy(True, text=self._combine_progress_text())
        log.info("Combine started -> %s", out)

        def progress(frac):
            self._safe_after(lambda: self._combine_progress(frac))

        def work():
            try:
                with combine.report_progress(progress):
                    ok, detail = fn()
            except Exception as e:
                ok, detail = False, str(e)
            self._safe_after(lambda: self._combine_done(ok, out, detail))
        threading.Thread(target=work, name="combine", daemon=True).start()

    def _combine_progress_text(self):
        total = max(1, self._combine_total)
        i = min(total, len(self._combine_results) + 1)
        base = "Combining" if total == 1 else f"Combining {i} of {total}"
        frac = getattr(self, "_combine_frac", None)
        if frac is None:
            return base + "..."
        text = f"{base} - {int(frac * 100)}%"
        eta = ux.eta_text(time.monotonic() - self._combine_t0, frac)
        return f"{text}, {eta}" if eta else text

    def _combine_progress(self, frac):
        """ffmpeg reported how far it got: show a real percentage (and a
        rough time left) instead of an endless marquee."""
        if not self._combine_busy:
            return
        self._combine_frac = frac
        try:
            if str(self.busy_bar.cget("mode")) != "determinate":
                self.busy_bar.stop()
                self.busy_bar.config(mode="determinate", maximum=100)
            self.busy_bar.config(value=frac * 100)
        except tk.TclError:
            pass
        self._set_busy(True, text=self._combine_progress_text())

    def _combine_done(self, ok, out, detail):
        self._combine_busy = False
        self._combine_frac = None
        self._pending_out_paths.discard(out)
        ok = bool(ok) and os.path.isfile(out)
        if ok:
            log.info("Combined -> %s", out)
        else:
            log.error("Combine failed: %s", str(detail)[:800])
        self._combine_results.append((ok, out, detail))
        # More jobs waiting? Start the next one; the summary comes at the end.
        if self._combine_queue and not getattr(self, "_closing", False):
            fn, nxt = self._combine_queue.pop(0)
            self._run_combine(fn, nxt)
            return
        results, self._combine_results = self._combine_results, []
        self._combine_total = 0
        self._restore_status()
        self._refresh_library()  # the merged files may add new session folders
        saved = [o for k, o, _ in results if k]
        failed = [(o, d) for k, o, d in results if not k
                  and d != "Cancelled before it started."]
        cancelled = len(results) - len(saved) - len(failed)
        if saved:
            first = saved[0]
            names = ", ".join(os.path.basename(o) for o in saved[:2])
            if len(saved) > 2:
                names += f" and {len(saved) - 2} more"
            extra = f"  ({cancelled} cancelled)" if cancelled else ""
            self._show_strip(
                "ok" if not failed else "warn",
                "✓ Combined" if not failed else "Partly combined",
                f"Saved {names}{extra}",
                [("Show in folder", lambda: self._reveal_path(first)),
                 ("Play", lambda: self._open_path(first))])
        elif cancelled and not failed:
            self._show_strip("info", "Cancelled",
                             "Nothing was combined.", timeout_ms=8000)
        if failed:
            text = "\n\n".join(f"{os.path.basename(o)}:\n{str(d)[-1500:]}"
                               for o, d in failed)
            head, advice = ux.friendly_error(text)
            self._error(
                "Couldn't combine" if not saved else "Some files weren't made",
                head or ("The combined file could not be made." if not saved
                         else f"{ux.plural(len(failed), 'file')} could not "
                         "be made."),
                advice or "Your original recordings are untouched. Try "
                "again, or pick a different option.",
                details=text)

    def on_close(self):
        """Quit, but never lose a take: confirm with a safe default, then
        finalize on the worker while a small 'Saving...' window is shown."""
        if self._closing or self._quitting:
            return
        if self._close_asking:
            # A second X click / tray Quit while the question is up: bring
            # the existing question forward instead of stacking another.
            self._raise_open_dialog()
            return
        if self._starting:
            # Let the start finish; then the normal prompt below applies.
            # One pending retry only, however often X is clicked.
            if self._close_retry_job is None:
                def retry():
                    self._close_retry_job = None
                    self.on_close()
                self._close_retry_job = self.after(250, retry)
            return
        self._close_asking = True  # latch until the questions are answered
        try:
            if not self._close_questions():
                return
        finally:
            self._close_asking = False
        if self.recording or self._finalizing:
            self._quitting = True
            if self.recording:
                self.stop_recording()
            self._show_closing_window()
        self._quitting = True
        self._close_when_idle()

    def _raise_open_dialog(self):
        dialogs = [w for w in self.winfo_children()
                   if getattr(w, "_srr_dialog", False)]
        for w in reversed(dialogs):
            try:
                if w.winfo_ismapped():
                    w.lift()
                    w.focus_force()
                    return
            except tk.TclError:
                continue

    def _close_questions(self):
        """Ask whatever must be asked before quitting; False = stay open."""
        if self.recording:
            if not self._confirm(
                    "Stop recording and quit?",
                    "A recording is in progress. Quitting stops it and "
                    "saves everything recorded so far.",
                    yes="Stop and quit", no="Keep recording", kind="warning"):
                return False
        if self._combine_busy:
            if not self._confirm(
                    "Quit while combining?",
                    "A combine or convert is still running and will be "
                    "abandoned if you quit now. Your original recordings "
                    "are not affected.",
                    yes="Quit anyway", no="Keep working"):
                return False
        if self._transcribe_busy:
            if not self._confirm(
                    "Quit while transcribing?",
                    "If you quit now, Scrivox keeps working in the "
                    "background and the transcript is still saved next to "
                    "the recording - but this app won't be around to tell "
                    "you when it's done.",
                    yes="Quit anyway", no="Keep working"):
                return False
        return True

    def _show_closing_window(self):
        """Hide the main window and show a small progress window while the
        recording is finalized, so quitting never looks like a hang."""
        try:
            self._save_window_state()
            self.withdraw()
            win = tk.Toplevel(self)
            win.title(APP_TITLE)
            win.configure(bg=COLORS["bg"])
            win.resizable(False, False)
            win.protocol("WM_DELETE_WINDOW", lambda: None)
            frm = ttk.Frame(win, style="TFrame", padding=24)
            frm.pack(fill="both", expand=True)
            ttk.Label(frm, text="Saving your recording before closing...",
                      style="Section.TLabel").pack(anchor="w")
            ttk.Label(frm, text="This usually takes a few seconds. Please "
                      "don't turn off the computer.",
                      style="Muted.TLabel").pack(anchor="w", pady=(6, 12))
            bar = ttk.Progressbar(frm, mode="indeterminate",
                                  length=int(360 * self._s),
                                  style="Busy.Horizontal.TProgressbar")
            bar.pack(fill="x")
            bar.start(12)
            win.update_idletasks()
            x = (win.winfo_screenwidth() - win.winfo_reqwidth()) // 2
            y = (win.winfo_screenheight() - win.winfo_reqheight()) // 3
            win.geometry(f"+{x}+{y}")
            set_dark_titlebar(win)
        except tk.TclError:
            pass

    def _close_when_idle(self):
        if self._finalizing:
            self.after(150, self._close_when_idle)
            return
        self._teardown()

    def _save_window_state(self):
        try:
            state = self.state()
            if state == "withdrawn":
                return
            geom = self._normal_geom if state == "zoomed" else self.geometry()
            self.cfg.update({"window_geometry": geom or "",
                             "window_zoomed": state == "zoomed"})
        except tk.TclError:
            pass

    def _teardown(self):
        self._save_window_state()
        try:
            self._save_settings()
        except Exception:
            log.debug("final settings save failed", exc_info=True)
        # Stop the after() poll/meter loops cleanly so they don't fire on a
        # destroyed window (which would raise TclError during shutdown).
        self._closing = True
        self._stop_monitor()
        if self.hotkeys:
            try:
                self.hotkeys.stop()
            except Exception:
                log.debug("hotkey stop failed", exc_info=True)
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                log.debug("tray stop failed", exc_info=True)
        try:
            self.destroy()
        except tk.TclError:
            pass


def _fmt_elapsed(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def _enable_dpi_awareness():
    """Make the app crisp and correctly sized on high-DPI displays, and stop it
    from resizing when a window appears on a monitor with a different DPI."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            # PROCESS_SYSTEM_DPI_AWARE (1): consistent, no per-monitor rescale jumps.
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def close_splash():
    """Close the PyInstaller splash (Windows one-file build) if one is up.
    A no-op when running from source or when the splash was suppressed."""
    try:
        import pyi_splash  # only exists inside a PyInstaller build
    except ImportError:
        return
    try:
        if pyi_splash.is_alive():
            pyi_splash.close()
    except Exception:  # never let the splash block startup
        log.debug("closing the splash screen failed", exc_info=True)


def run():
    _enable_dpi_awareness()
    try:
        app = App()
    except Exception as e:
        close_splash()  # the error box must not hide behind the splash
        import traceback
        tb = traceback.format_exc()
        log.error("FATAL startup error: %s\n%s", e, tb)
        try:
            crash = os.path.join(paths.data_dir(), "startup_crash.txt")
            with open(crash, "w", encoding="utf-8") as fh:
                fh.write(tb)
        except OSError:
            crash = "(could not write crash file)"
        try:
            from tkinter import messagebox as _mb
            _mb.showerror(
                APP_TITLE + " could not start",
                "Something went wrong while opening the app.\n\n"
                f"{type(e).__name__}: {e}\n\n"
                "Try starting it again. If it keeps happening, send this "
                f"file to support:\n{crash}")
            e._srr_reported = True  # main() must not show a second box
        except tk.TclError:
            pass
        raise
    # The window is built; close the splash once it has actually painted,
    # so there is never a moment with nothing on screen.
    app.after(150, close_splash)
    app.mainloop()
