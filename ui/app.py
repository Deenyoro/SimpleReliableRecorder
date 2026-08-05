"""Main GUI for SimpleReliableRecorder.

Simple by design: pick devices, balance levels, hit RECORD. Everything else
(crash-safe writing, resilience, the gold alert, screen capture, combine) hangs
off that core flow.
"""

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

from recorder import (alerts, combine, ffmpeg_tools, hotkeys, library, paths,
                      screen as screenmod, scrivox_bridge, tray, watchdog)
from recorder.audio import (AudioRecorder, CaptureSource, LevelMonitor,
                            default_devices, list_devices, resolve_selection)
from recorder.config import ConfigManager
from recorder.logging_setup import get_logger, install_inapp_handler
from recorder.screen import ScreenRecorder, list_monitors
from ui.widgets import (COLORS, FONT, DeviceRow, GoldBanner, ScrollFrame,
                        SegmentedControl, StatusLight, ToggleSwitch, Tooltip,
                        apply_dark_theme)

log = get_logger("gui")


def _ellipsize(s, n=46):
    """Middle-ellipsis so both the start and the distinctive tail survive."""
    return s if len(s) <= n else s[: n // 2 - 1] + "..." + s[-(n // 2 - 2):]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SimpleReliableRecorder")
        apply_dark_theme(self)
        try:
            _dpi = self.winfo_fpixels("1i")
            if _dpi and _dpi > 0:
                self.tk.call("tk", "scaling", _dpi / 72.0)
        except Exception:
            pass
        # Size to the screen rather than a fixed pixel box (which looks tiny on
        # high-DPI displays), then start maximized so nothing is cramped.
        try:
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
            # A single monitor's height; with vertically stacked monitors
            # winfo_screenheight can report the combined height, so clamp it.
            mon_h = sh if sh < 2000 else sh // 2
            # Proportional to the screen (no hard pixel cap) so it is never tiny
            # on a wide/high-DPI display even if maximizing does not take.
            w = max(1100, int(sw * 0.66))
            h = max(760, int(mon_h * 0.85))
            x = max(0, (sw - w) // 2)
            y = max(0, (mon_h - h) // 3)
            self.geometry(f"{w}x{h}+{x}+{y}")
            self.minsize(min(1000, sw - 40), min(700, mon_h - 80))
        except Exception:
            self.geometry("1280x860")
            self.minsize(1000, 700)
        # Maximize after the window is actually mapped - calling zoomed during
        # __init__ is unreliable on multi-monitor / high-DPI Windows.
        self.after(60, self._maximize)
        ip = paths.icon_path()
        if ip:
            try:
                self.iconbitmap(ip)
            except Exception:
                pass
        # Build normal + recording (red) window icons. Swapping the window icon
        # is what makes the TASKBAR button turn red while recording on Windows.
        self._icon_normal = None
        self._icon_recording = None
        self._build_window_icons()

        self.cfg = ConfigManager()
        self._cleanup_stale_sessions()
        self.inputs, self.outputs = list_devices()
        self.all_devices = self.inputs + self.outputs
        self.encoders = ffmpeg_tools.probe_encoders()

        # Recordings library: prune entries whose files were moved/deleted, keep
        # the rest so the user can combine past takes without reopening the app.
        self._library, _pruned = library.prune(self.cfg.get("recordings") or [])
        # Back-fill recordings that exist on disk but predate the library (or
        # were made by an older build) by scanning the save folder.
        try:
            known_dirs = {e.get("out_dir") for e in self._library}
            discovered = library.scan_folder(
                self.cfg.resolved_save_folder(), existing_dirs=known_dirs)
            if discovered:
                # Oldest first so newest-first display stays chronological.
                discovered.sort(key=lambda e: e.get("created", ""))
                self._library.extend(discovered)
                _pruned = True
                log.info("Imported %d existing recording(s) into the library.",
                         len(discovered))
        except Exception as e:
            log.warning("Library scan skipped: %s", e)
        if _pruned:
            self.cfg.set("recordings", self._library)
        self._lib_rows = []
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
        self.last_outputs = {}
        self._restart_cooldown = {}
        self._restart_counts = {}
        self._log_queue = queue.Queue()
        self._device_rows = []
        self.level_monitor = None
        self._save_job = None
        self._combine_busy = False
        # FIFO of pending combine/convert jobs: firing several operations (or
        # converting several ticked recordings) runs them one after another
        # instead of refusing everything after the first.
        self._combine_queue = []
        self._combine_results = []
        # Output paths promised to queued/running jobs but not on disk yet, so
        # _unique_path can't hand the same name to two queued jobs.
        self._pending_out_paths = set()
        # Fallback lane for UI callbacks whose after() scheduling failed (see
        # _safe_after); drained by _poll so completions can never be lost.
        self._ui_calls = queue.Queue()
        self._transcribe_busy = False
        # Optional Scrivox integration: when no Scrivox install is found, every
        # Scrivox-related control stays hidden (users without it never see it).
        self._scrivox_exe = scrivox_bridge.find_scrivox(
            self.cfg.get("scrivox_path"))
        if self._scrivox_exe:
            log.info("Scrivox detected: %s", self._scrivox_exe)
        self._closing = False
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
        self._poll()
        self._meter_loop()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        log.info("GUI ready. %d devices, encoders=%s", len(self.all_devices),
                 self.encoders)

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
        """Schedule fn on the Tk thread, ignoring it if we're shutting down or
        the interpreter is already gone (prevents TclError from tray/hotkey
        threads firing into a destroyed window)."""
        if getattr(self, "_closing", False):
            return
        try:
            self.after(0, fn)
        except Exception:
            # after() from a worker thread can fail while the main thread is
            # not dispatching events (e.g. a blocking wait loop). Silently
            # dropping the callback here once left _combine_busy stuck True
            # forever - every later merge was refused with "already running".
            # Park the callback instead; _poll runs it on the Tk thread.
            try:
                self._ui_calls.put(fn)
            except Exception:
                pass

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
        if self.ptt_enabled_var.get() and not ok:
            log.warning("Hotkey '%s' could not be registered - "
                        "push-to-talk is INACTIVE.",
                        self.ptt_hotkey_var.get().strip())

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
        for d in self.all_devices:
            label = f'{d["name"]} [{d["kind"]}]'
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

    # ------------------------------------------------------------------ UI #
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
        # Debounced: rebinding on every keystroke of the hotkey field would
        # briefly register single-character global hotkeys and (in ptt mode)
        # mute the mics the moment the user types the first letter.
        for v in (self.ptt_enabled_var, self.ptt_hotkey_var,
                  self.ptt_target_var, self.ptt_mode_var):
            v.trace_add("write", lambda *a: self._request_hotkey_reconfig())

    def _build_ui(self):
        root = ttk.Frame(self, style="TFrame")
        root.pack(fill="both", expand=True, padx=14, pady=12)

        header = ttk.Frame(root, style="TFrame")
        header.pack(fill="x", pady=(0, 10))
        ttk.Label(header, text="SimpleReliableRecorder", style="Title.TLabel").pack(side="left")
        ttk.Button(header, text="Settings", command=self._open_settings).pack(
            side="left", padx=14)
        self.audio_light = StatusLight(header, "Audio")
        self.audio_light.pack(side="right", padx=4)
        self.screen_light = StatusLight(header, "Screen")
        self.screen_light.pack(side="right", padx=4)
        self.audio_light.set_state(COLORS["muted"], ": idle")
        self.screen_light.set_state(COLORS["muted"], ": off")
        self.elapsed_lbl = ttk.Label(header, text="00:00:00", style="Header.TLabel")
        self.elapsed_lbl.pack(side="right", padx=12)

        body = ttk.Frame(root, style="TFrame")
        body.pack(fill="both", expand=True)
        left_scroll = ScrollFrame(body)
        left_scroll.pack(side="left", fill="both", expand=True, padx=(0, 10))
        left = left_scroll.body
        # The right column scrolls too: on a small window the Live log and the
        # library buttons used to be pushed below the bottom edge with no way
        # to reach them.
        right_scroll = ScrollFrame(body)
        right_scroll.pack(side="left", fill="both", expand=True)
        right = right_scroll.body

        self._build_devices(left)
        self._build_screen(left)

        self._build_record(right)
        self._build_library(right)
        self._build_log(right)

        self.banner = GoldBanner(self, on_ack=self._dismiss_alert,
                                 on_restart=self._restart_recording)

    def _section(self, parent, title):
        lf = ttk.Labelframe(parent, text=title, style="TLabelframe")
        lf.pack(fill="x", pady=6)
        inner = ttk.Frame(lf, style="TFrame")
        inner.pack(fill="x", padx=12, pady=8)
        return inner

    def _build_devices(self, parent):
        inner = self._section(parent, "Audio devices  (mic + system playback)")
        self.rows_frame = ttk.Frame(inner, style="TFrame")
        self.rows_frame.pack(fill="x")
        btns = ttk.Frame(inner, style="TFrame")
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="+ Add device", command=lambda: self._add_row()).pack(side="left")
        ttk.Button(btns, text="+ Default mic",
                   command=self._add_default_mic).pack(side="left", padx=6)
        ttk.Button(btns, text="+ System playback",
                   command=self._add_system_playback).pack(side="left")
        # Own row: sharing the row above clipped this pair off the right edge
        # whenever the window was narrow.
        btns2 = ttk.Frame(inner, style="TFrame")
        btns2.pack(fill="x", pady=(6, 0))
        meters_toggle = ToggleSwitch(btns2, self.live_levels_var,
                                     text="Live meters")
        meters_toggle.pack(side="left")
        Tooltip(meters_toggle, "Show each device's live level even while not "
                               "recording, so you can check a mic works.")
        dev_refresh = ttk.Button(btns2, text="Refresh",
                                 command=self._refresh_devices)
        dev_refresh.pack(side="right")
        Tooltip(dev_refresh, "Re-scan for plugged-in/unplugged devices.")
        ttk.Label(inner, text="Balance each device with the faders; meters are live.",
                  style="Muted.TLabel").pack(anchor="w", pady=(8, 0))

    def _build_screen(self, parent):
        inner = self._section(parent, "Screen recording  (optional)")
        top = ttk.Frame(inner, style="TFrame")
        top.pack(fill="x")
        ttk.Label(top, text="Record a screen").pack(side="left")
        ToggleSwitch(top, self.screen_enabled, command=self._toggle_screen).pack(
            side="left", padx=(12, 0))

        # Collapsible options panel: only visible when the toggle is on.
        self.screen_opts = ttk.Frame(inner, style="TFrame")
        row = ttk.Frame(self.screen_opts, style="TFrame")
        row.pack(fill="x", pady=(10, 0))
        ttk.Label(row, text="Monitor:").pack(side="left")
        self.monitor_combo = ttk.Combobox(row, textvariable=self.monitor_var,
                                          width=26, state="readonly")
        self.monitor_combo.pack(side="left", padx=6)
        ident_btn = ttk.Button(row, text="Identify screens",
                               command=self._identify_screens)
        ident_btn.pack(side="left", padx=6)
        Tooltip(ident_btn, "Flashes a big number on each monitor so you can "
                           "tell which is which.")
        self._refresh_monitor_list()
        ttk.Label(self.screen_opts,
                  text="Encoder, quality and crash-safety options are in Settings.",
                  style="Muted.TLabel").pack(anchor="w", pady=(8, 0))
        self._toggle_screen()

    def _toggle_screen(self):
        if self.screen_enabled.get():
            self.screen_opts.pack(fill="x")
        else:
            self.screen_opts.pack_forget()

    # --------------------------------------------------------- settings win #
    def _open_settings(self):
        if self.settings_win is not None and self.settings_win.winfo_exists():
            self.settings_win.deiconify()
            self.settings_win.lift()
            self.settings_win.focus_force()
            return
        win = tk.Toplevel(self)
        self.settings_win = win
        win.title("Settings  -  SimpleReliableRecorder")
        win.configure(bg=COLORS["bg"])
        # Wide enough that the longest single row (FPS / Quality / Crash-safety)
        # and the resilience toggle labels are fully visible without horizontal
        # scrolling, but clamped to the screen so the window (and its Close
        # button) can never open partly off a small display.
        try:
            sw = win.winfo_screenwidth()
            sh = win.winfo_screenheight()
            mon_h = sh if sh < 2000 else sh // 2  # stacked-monitor clamp
            w, h = min(760, sw - 40), min(780, mon_h - 80)
        except Exception:
            w, h = 760, 780
        win.geometry(f"{w}x{h}")
        win.minsize(min(720, w), min(560, h))
        try:
            ip = paths.icon_path()
            if ip:
                win.iconbitmap(ip)
        except Exception:
            pass
        win.transient(self)

        bottom = ttk.Frame(win, style="TFrame")
        bottom.pack(side="bottom", fill="x", padx=12, pady=10)
        ttk.Button(bottom, text="Open logs folder",
                   command=lambda: os.startfile(paths.logs_dir())).pack(side="left")
        ttk.Button(bottom, text="Close", style="Accent.TButton",
                   command=lambda: _on_settings_close()).pack(side="right")

        # Tabs instead of one tall scroll: each concern fits on screen and the
        # safety-critical alerts page is findable by name instead of being
        # below the fold.
        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=12, pady=(12, 0))
        pages = {}
        for name in ("Recording", "Saving", "Safety & alerts", "Hotkey & tray"):
            pg = ScrollFrame(nb)
            nb.add(pg, text=name)
            pages[name] = pg.body

        # --- Audio output ---
        a = self._section(pages["Recording"], "Audio output")
        modes = [
            ("separate", "Separate file per device  (safest, default)"),
            ("channels", "Separate channels in ONE file  (mic=ch1, playback=ch2 ...)"),
            ("mixed", "Single mixed file"),
        ]
        SegmentedControl(a, self.output_mode, modes).pack(fill="x")
        sub = ttk.Frame(a, style="TFrame")
        sub.pack(fill="x", pady=(8, 0))
        ttk.Label(sub, text="Sample format:").pack(side="left")
        ttk.Combobox(sub, textvariable=self.subtype, width=10, state="readonly",
                     values=["PCM_16", "FLOAT"]).pack(side="left", padx=6)
        ttk.Label(sub, text="WAVs are crash-safe (flushed every ~2s).",
                  style="Muted.TLabel").pack(side="left", padx=8)

        # --- Screen quality ---
        s = self._section(pages["Recording"], "Screen recording quality")
        r1 = ttk.Frame(s, style="TFrame")
        r1.pack(fill="x")
        ttk.Label(r1, text="Encoder:").pack(side="left")
        enc_values = ["auto", "cpu"]
        for fam in ("nvenc", "qsv", "amf", "videotoolbox"):
            if self.encoders.get(fam):
                enc_values.insert(-1, fam)
        ttk.Combobox(r1, textvariable=self.encoder_var, width=8, state="readonly",
                     values=enc_values).pack(side="left", padx=6)
        ttk.Label(r1, text="Container:").pack(side="left", padx=(10, 0))
        ttk.Combobox(r1, textvariable=self.container_var, width=6, state="readonly",
                     values=["mkv", "mp4"]).pack(side="left", padx=6)
        ttk.Label(r1, text="Codec:").pack(side="left", padx=(10, 0))
        ttk.Combobox(r1, textvariable=self.codec_var, width=6, state="readonly",
                     values=["h264", "hevc"]).pack(side="left", padx=6)

        r2 = ttk.Frame(s, style="TFrame")
        r2.pack(fill="x", pady=(8, 0))
        ttk.Label(r2, text="FPS:").pack(side="left")
        ttk.Spinbox(r2, from_=5, to=60, width=4,
                    textvariable=self.fps_var).pack(side="left", padx=4)
        ttk.Label(r2, text="Quality:").pack(side="left", padx=(10, 0))
        ttk.Combobox(r2, textvariable=self.quality_var, width=10, state="readonly",
                     values=["high", "balanced", "small"]).pack(side="left", padx=6)
        ttk.Label(r2, text="Crash safety:").pack(side="left", padx=(10, 0))
        ttk.Combobox(r2, textvariable=self.reliability_var, width=12, state="readonly",
                     values=["hybrid", "fragmented", "standard"]).pack(side="left", padx=6)
        ttk.Label(s, text="hybrid = crash-safe fragments, auto-cleaned on stop "
                  "(MKV is always safe).",
                  style="Muted.TLabel").pack(anchor="w", pady=(8, 0))

        # --- Output location ---
        o = self._section(pages["Saving"], "Output location")
        f = ttk.Frame(o, style="TFrame")
        f.pack(fill="x")
        ttk.Label(f, text="Save to:").pack(side="left")
        ttk.Entry(f, textvariable=self.folder_var, width=40).pack(
            side="left", padx=6, fill="x", expand=True)
        ttk.Button(f, text="Browse", command=self._browse_folder).pack(side="left")
        f2 = ttk.Frame(o, style="TFrame")
        f2.pack(fill="x", pady=(10, 0))
        ToggleSwitch(f2, self.ask_var, text="Ask for folder each time").pack(side="left")
        ttk.Label(f2, text="When screen+audio ends:").pack(side="left", padx=(18, 4))
        ttk.Combobox(f2, textvariable=self.on_stop_var, width=10, state="readonly",
                     values=["ask", "combine", "separate"]).pack(side="left")

        # --- System tray ---
        tsec = self._section(pages["Hotkey & tray"], "System tray")
        ToggleSwitch(tsec, self.tray_var,
                     text="Show a tray icon (turns red while recording)").pack(
            anchor="w", pady=3)
        ttk.Label(tsec, text="Takes effect on next launch.",
                  style="Muted.TLabel").pack(anchor="w", pady=(4, 0))

        # --- Push to talk / push to mute ---
        psec = self._section(pages["Hotkey & tray"],
                             "Push to talk / push to mute hotkey")
        ToggleSwitch(psec, self.ptt_enabled_var,
                     text="Enable a global hotkey that mutes/unmutes a device").pack(
            anchor="w", pady=3)
        pr = ttk.Frame(psec, style="TFrame")
        pr.pack(fill="x", pady=(8, 0))
        ttk.Label(pr, text="Hotkey:").pack(side="left")
        ttk.Entry(pr, textvariable=self.ptt_hotkey_var, width=14).pack(
            side="left", padx=6)
        ttk.Label(pr, text="e.g. f8, ctrl+space", style="Muted.TLabel").pack(
            side="left")
        pr2 = ttk.Frame(psec, style="TFrame")
        pr2.pack(fill="x", pady=(8, 0))
        ttk.Label(pr2, text="Mode:").pack(side="left")
        ttk.Combobox(pr2, textvariable=self.ptt_mode_var, width=8, state="readonly",
                     values=["ptt", "ptm", "toggle"]).pack(side="left", padx=6)
        ttk.Label(pr2, text="Device:").pack(side="left", padx=(10, 0))
        self.ptt_device_combo = ttk.Combobox(pr2, width=30, state="readonly")
        self.ptt_device_combo.pack(side="left", padx=6)
        self._populate_ptt_devices()
        self.ptt_device_combo.bind("<<ComboboxSelected>>",
                                   lambda e: self._on_ptt_device_pick())
        ttk.Label(psec,
                  text="ptt = hold to talk (released = muted).  "
                  "ptm = hold to mute.  toggle = press to flip.  "
                  "Device blank = all microphones.",
                  style="Muted.TLabel", wraplength=560, justify="left").pack(
            anchor="w", pady=(6, 0))

        # --- Scrivox transcription (always shown, so the path can be set
        # even when auto-detection finds nothing) ---
        if not self._transcribe_busy:
            self._scrivox_exe = scrivox_bridge.find_scrivox(
                self.cfg.get("scrivox_path"))
        xsec = self._section(pages["Saving"], "Transcription (Scrivox)")
        scrivox_status = ttk.Label(xsec, style="Muted.TLabel", wraplength=560,
                                   justify="left")
        scrivox_status.pack(anchor="w")
        xpath = ttk.Frame(xsec, style="TFrame")
        xpath.pack(fill="x", pady=(8, 0))
        ttk.Label(xpath, text="Scrivox location:").pack(side="left")
        ttk.Entry(xpath, textvariable=self.scrivox_path_var, width=34).pack(
            side="left", padx=6, fill="x", expand=True)
        xrow = ttk.Frame(xsec, style="TFrame")
        xrow.pack(fill="x", pady=(8, 0))
        open_btn = ttk.Button(xrow, text="Open Scrivox",
                              command=lambda: scrivox_bridge.open_scrivox(
                                  self._scrivox_exe))
        scrivox_info = ttk.Label(
            xsec,
            text="Transcription options (model, language, speakers, "
            "API keys, screen-description detail) are configured "
            "inside Scrivox and used automatically here.",
            style="Muted.TLabel", wraplength=560, justify="left")

        def _scrivox_update_status():
            # Until Scrivox is actually found, the ONLY Scrivox UI anywhere
            # is this location setting - no dead buttons, no explainer text.
            if self._scrivox_exe:
                scrivox_status.config(
                    text=f"Scrivox detected:  {self._scrivox_exe}")
                if not open_btn.winfo_manager():
                    open_btn.pack(side="left", padx=(6, 0))
                if not scrivox_info.winfo_manager():
                    scrivox_info.pack(anchor="w", pady=(6, 0))
            else:
                scrivox_status.config(
                    text="Scrivox not found. Point 'Scrivox location' at "
                         "Scrivox.exe (or its folder), or leave it blank to "
                         "auto-detect an installed/portable Scrivox.")
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
            p = filedialog.askopenfilename(
                parent=win, title="Locate Scrivox.exe",
                filetypes=[("Scrivox", "Scrivox.exe"),
                           ("Programs", "*.exe"), ("All files", "*.*")])
            if p:
                self.scrivox_path_var.set(p)
                _scrivox_redetect()

        ttk.Button(xrow, text="Browse", command=_scrivox_browse).pack(
            side="left")
        ttk.Button(xrow, text="Check", command=_scrivox_redetect).pack(
            side="left", padx=6)
        _scrivox_update_status()

        # --- Resilience ---
        rsec = self._section(pages["Safety & alerts"], "Resilience")
        ToggleSwitch(rsec, self.autorestart_var,
                     text="Auto-restart a subsystem if it fails mid-recording").pack(
            anchor="w", pady=3)
        ToggleSwitch(rsec, self.watchdog_var,
                     text="Run an independent background watchdog process").pack(
            anchor="w", pady=3)
        ttk.Label(rsec, text="The watchdog is a separate process that alerts you even "
                  "if this app freezes.", style="Muted.TLabel").pack(anchor="w",
                                                                     pady=(4, 0))

        # --- Alerts ---
        al = self._section(pages["Safety & alerts"], "Alert me if recording stops")
        for text, var in (("Play a sound", self.sound_var),
                          ("Flashing gold banner", self.banner_var),
                          ("Flash the taskbar button", self.taskbar_var),
                          ("Watchdog pop-up message box", self.msgbox_var)):
            ToggleSwitch(al, var, text=text).pack(anchor="w", pady=3)

        def _on_settings_close():
            self._save_settings()
            # Commit any pending (debounced) hotkey change right away.
            if self._hotkey_job:
                try:
                    self.after_cancel(self._hotkey_job)
                except Exception:
                    pass
                self._reconfigure_hotkeys()
            # Apply a hand-edited Scrivox path without needing the Check
            # button: re-detect (skipping the cache) and refresh the library
            # so the Transcribe button appears/disappears immediately.
            if not self._transcribe_busy:
                self._scrivox_exe = scrivox_bridge.find_scrivox(
                    self.cfg.get("scrivox_path"), force=True)
            self._refresh_library()
            self.settings_win = None
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", _on_settings_close)
        win.bind("<Escape>", lambda e: _on_settings_close())

    IDLE_TEXT = "Ready - press RECORD (or F9) to start. Everything saves automatically."

    def _build_record(self, parent):
        inner = self._section(parent, "Record")
        self.record_btn = tk.Button(inner, text="●  RECORD", command=self._toggle_record,
                                    bg=COLORS["green"], fg="#0b0b0b",
                                    font=("Segoe UI", 20, "bold"), relief="flat",
                                    height=2, activebackground="#7fd687",
                                    cursor="hand2", takefocus=1,
                                    highlightthickness=2,
                                    highlightbackground=COLORS["bg"],
                                    highlightcolor=COLORS["fg"])
        self.record_btn.pack(fill="x", pady=4)
        # F9 works everywhere (a bare Space would fight with text entries).
        self.bind("<F9>", lambda e: self._toggle_record())
        self.status_lbl = ttk.Label(inner, text=self.IDLE_TEXT,
                                    style="Muted.TLabel")
        self.status_lbl.pack(anchor="w", pady=(6, 0))
        # Where files go - the top question from new users. Click to open.
        self.saveto_lbl = ttk.Label(
            inner, style="Muted.TLabel", cursor="hand2",
            text=f"Saving to: {self.cfg.resolved_save_folder()}")
        self.saveto_lbl.pack(anchor="w", pady=(2, 0))
        self.saveto_lbl.bind(
            "<Button-1>",
            lambda e: self._open_folder(self.cfg.resolved_save_folder()))
        self.folder_var.trace_add(
            "write", lambda *a: self.saveto_lbl.config(
                text=f"Saving to: {self.cfg.resolved_save_folder()}"))
        Tooltip(self.saveto_lbl, "Click to open this folder.")
        # One shared busy area for combine/convert/transcribe background jobs:
        # motion while something runs, and a way to drop what hasn't started.
        self.busy_row = ttk.Frame(inner, style="TFrame")
        self.busy_bar = ttk.Progressbar(self.busy_row, mode="indeterminate",
                                        style="Busy.Horizontal.TProgressbar")
        self.busy_bar.pack(side="left", fill="x", expand=True)
        self.busy_cancel = ttk.Button(self.busy_row, text="Cancel queued",
                                      command=self._cancel_queued_jobs)
        self.busy_cancel.pack(side="left", padx=(8, 0))

    def _open_folder(self, path):
        try:
            if path and os.path.isdir(path):
                os.startfile(path)
        except Exception as e:
            log.warning("open folder failed: %s", e)

    def _set_busy(self, on):
        """Show/hide the animated busy bar under the status label."""
        try:
            if on:
                if not self.busy_row.winfo_manager():
                    self.busy_row.pack(fill="x", pady=(6, 0))
                    if str(self.busy_bar.cget("mode")) == "indeterminate":
                        self.busy_bar.start(12)
                if not self._transcribe_busy:
                    self.busy_cancel.config(state=(
                        "normal" if self._combine_queue else "disabled"))
            else:
                self.busy_bar.stop()
                self.busy_bar.config(mode="indeterminate", value=0)
                self.busy_cancel.config(text="Cancel queued",
                                        command=self._cancel_queued_jobs)
                self.busy_row.pack_forget()
        except Exception:
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
            self.status_lbl.config(
                text="Finishing the current job... (queued ones cancelled)")
        self.busy_cancel.config(state="disabled")

    # ----------------------------------------------------- recordings library #
    def _build_library(self, parent):
        inner = self._section(parent, "Recordings library")
        desc = ttk.Label(
            inner, style="Muted.TLabel", justify="left",
            text="Past recordings stay listed here so you can keep recording, "
            "then tick any (click anywhere on a row) and use the buttons "
            "below. Double-click renames; right-click for more options. "
            "Entries whose files are moved are removed automatically.")
        desc.pack(fill="x", anchor="w")
        # Wrap the text to the actual panel width instead of a fixed value, so it
        # fills the column rather than hugging the left edge.
        desc.bind("<Configure>",
                  lambda e, w=desc: w.configure(wraplength=max(200, e.width - 4)))

        self.lib_holder = ScrollFrame(inner)
        self.lib_holder.configure(height=150)
        self.lib_holder.pack(fill="x", pady=(8, 0))
        self.lib_body = self.lib_holder.body
        self.lib_empty = ttk.Label(
            inner, style="Muted.TLabel", justify="left",
            text="No recordings yet. Your first one will appear here the "
                 "moment you press STOP - nothing to save manually.")
        self.lib_empty.pack(anchor="w", pady=(4, 0))

        selrow = ttk.Frame(inner, style="TFrame")
        selrow.pack(fill="x", pady=(6, 0))
        ttk.Button(selrow, text="Select all", width=10,
                   command=lambda: self._set_all_ticks(True)).pack(side="left")
        ttk.Button(selrow, text="Clear", width=8,
                   command=lambda: self._set_all_ticks(False)).pack(
            side="left", padx=6)
        self.lib_sel_lbl = ttk.Label(selrow, text="Nothing selected.",
                                     style="Muted.TLabel")
        self.lib_sel_lbl.pack(side="left", padx=(10, 0))

        b = ttk.Frame(inner, style="TFrame")
        b.pack(fill="x", pady=(4, 0))
        self.lib_btn_video = ttk.Button(
            b, text="Make one video with sound",
            command=lambda: self._combine_selected_library("video"),
            state="disabled")
        self.lib_btn_video.pack(fill="x", pady=2)
        self.lib_btn_multi = ttk.Button(
            b, text="Combine audio into one multitrack file",
            command=lambda: self._combine_selected_library("multitrack"),
            state="disabled")
        self.lib_btn_multi.pack(fill="x", pady=2)
        self.lib_btn_mix = ttk.Button(
            b, text="Mix all audio into one stereo file",
            command=lambda: self._combine_selected_library("mix"),
            state="disabled")
        self.lib_btn_mix.pack(fill="x", pady=2)
        self.lib_btn_convert = ttk.Button(
            b, text="Convert to...  (MP4, MP3, MKV, WAV ...)",
            command=self._convert_selected_library, state="disabled")
        self.lib_btn_convert.pack(fill="x", pady=2)
        # Only shown when a Scrivox install is detected (see _refresh_library).
        self.lib_btn_transcribe = ttk.Button(
            b, text="Transcribe with Scrivox...",
            command=self._transcribe_selected_library, state="disabled")

        b2 = ttk.Frame(inner, style="TFrame")
        b2.pack(fill="x", pady=(2, 0))
        open_btn = ttk.Button(b2, text="Open folder", width=12,
                              command=self._open_selected_library)
        open_btn.pack(side="left")
        remove_btn = ttk.Button(b2, text="Remove", width=10,
                                command=self._remove_selected_library)
        remove_btn.pack(side="left", padx=6)
        refresh_btn = ttk.Button(
            b2, text="Refresh", width=10,
            command=lambda: self._refresh_library(rescan=True))
        refresh_btn.pack(side="right")

        # Disabled buttons should say WHY on hover; the text is updated in
        # _update_library_buttons as the selection changes.
        self._lib_tips = {
            "video": Tooltip(self.lib_btn_video,
                             "Joins the ticked recordings end to end into one "
                             "video with their sound."),
            "multi": Tooltip(self.lib_btn_multi,
                             "One WAV where every ticked track keeps its own "
                             "channel - great for editing."),
            "mix": Tooltip(self.lib_btn_mix,
                           "Everything summed into one stereo file."),
            "convert": Tooltip(self.lib_btn_convert,
                               "Export each ticked recording to another "
                               "format (MP4, MP3, MKV, WAV...)."),
            "transcribe": Tooltip(self.lib_btn_transcribe,
                                  "Turn the ticked recordings into text with "
                                  "Scrivox - saved next to each recording."),
        }
        Tooltip(remove_btn, "Removes from this list only - never deletes "
                            "files on disk.")
        Tooltip(refresh_btn, "Re-scan the save folder for recordings made "
                             "outside this app.")
        Tooltip(open_btn, "Open the selected recording's folder.")
        self._refresh_library()

    def _add_to_library(self, select_new=False):
        audio = [a for a in (self.last_outputs.get("audio") or [])
                 if a and os.path.isfile(a)]
        # A mid-take screen auto-restart leaves the pre-restart video in
        # videos_extra; the entry must reference the MAIN segment (usually the
        # long pre-restart one, per _pick_video), not just the tail fragment.
        vids = [v for v in ([self.last_outputs.get("video") or ""]
                            + (self.last_outputs.get("videos_extra") or []))
                if v and os.path.isfile(v)]
        video = library._pick_video(vids) if vids else ""
        if not audio and not video:
            return
        self._lib_seq += 1
        entry = library.make_entry(
            entry_id=f"rec{self._lib_seq}-{int(self._record_start_mono)}",
            name=getattr(self, "_session_base", "recording"),
            out_dir=self.last_outputs.get("out_dir", ""),
            audio=audio, video=video,
            created=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        extras = [v for v in vids if v != video]
        if extras:
            # Kept for visibility/future use; prune() preserves unknown keys.
            entry["videos_extra"] = extras
        self._library.append(entry)
        self.cfg.set("recordings", self._library)
        self._refresh_library()
        # Auto-tick the recording that was just made so its actions are ready.
        if select_new:
            for r in self._lib_rows:
                if r["entry"].get("id") == entry["id"]:
                    r["var"].set(True)
                    break
            self._update_library_buttons()

    def _refresh_library(self, rescan=False):
        # Remember which entries are ticked so a refresh (e.g. right after a
        # long merge finishes) doesn't make the user re-find their selection.
        ticked = set()
        for r in self._lib_rows:
            try:
                if r["var"].get():
                    ticked.add(r["entry"].get("id"))
            except Exception:
                pass
        # Prune anything whose files vanished, then rebuild the checklist.
        self._library, pruned = library.prune(self._library)
        if rescan:
            try:
                known = {e.get("out_dir") for e in self._library}
                found = library.scan_folder(self.cfg.resolved_save_folder(),
                                            existing_dirs=known)
                if found:
                    self._library.extend(found)
                    # Keep the whole list chronological so back-filled old
                    # recordings don't show up above yesterday's takes.
                    self._library.sort(key=lambda e: e.get("created") or "")
                    pruned = True
            except Exception as e:
                log.warning("Library rescan failed: %s", e)
        if pruned:
            self.cfg.set("recordings", self._library)
        for r in self._lib_rows:
            try:
                r["frame"].destroy()
            except Exception:
                pass
        self._lib_rows = []
        for e in reversed(self._library):  # newest first
            row = ttk.Frame(self.lib_body, style="Card.TFrame", padding=6)
            row.pack(fill="x", pady=2)
            var = tk.BooleanVar(value=(e.get("id") in ticked))
            cb = ToggleSwitch(row, var, command=self._update_library_buttons)
            cb.pack(side="left")
            # Name + muted metadata on separate lines: the date is how people
            # actually remember a take, and long names get middle-ellipsized
            # instead of pushing everything off-screen.
            txt = ttk.Frame(row, style="Card.TFrame")
            txt.pack(side="left", padx=(10, 0), fill="x", expand=True)
            name_lbl = ttk.Label(txt, text=_ellipsize(e["name"]),
                                 style="Card.TLabel")
            name_lbl.pack(anchor="w")
            created = (e.get("created") or "")[:16]
            meta = library.summarize(e) + (f"   ·   {created}" if created else "")
            meta_lbl = tk.Label(txt, text=meta, bg=COLORS["panel2"],
                                fg=COLORS["muted"], font=(FONT, 9))
            meta_lbl.pack(anchor="w")
            # Click anywhere on the row to tick it (the tiny toggle was the
            # only target before). Double-click still renames: the two rapid
            # clicks toggle twice (net unchanged), then the dialog opens.
            def _toggle(ev, v=var):
                v.set(not v.get())
                self._update_library_buttons()
            for w in (row, txt, name_lbl, meta_lbl):
                w.bind("<Button-1>", _toggle)
                w.bind("<Double-Button-1>",
                       lambda ev, ent=e: self._rename_entry(ent))
            # Right-click a row for a context menu (rename / open / show).
            for w in (row, txt, name_lbl, meta_lbl, cb,
                      getattr(cb, "label", None), getattr(cb, "canvas", None)):
                if w is not None:
                    w.bind("<Button-3>",
                           lambda ev, ent=e: self._show_library_menu(ev, ent))
            self._lib_rows.append({"frame": row, "var": var, "entry": e})
        has = bool(self._library)
        self.lib_empty.pack_forget() if has else self.lib_empty.pack(
            anchor="w", pady=(4, 0))
        # Re-detect Scrivox on every rebuild so dropping it next to the app (or
        # installing it) starts working without a restart - and removing it
        # hides the button again. Cached in the bridge; the Refresh button
        # (rescan=True) forces a fresh sweep. Never re-detect mid-transcription.
        if not self._transcribe_busy:
            self._scrivox_exe = scrivox_bridge.find_scrivox(
                self.cfg.get("scrivox_path"), force=rescan)
        if self._scrivox_exe:
            if not self.lib_btn_transcribe.winfo_manager():
                self.lib_btn_transcribe.pack(fill="x", pady=2)
        else:
            self.lib_btn_transcribe.pack_forget()
        self._update_library_buttons()

    def _update_library_buttons(self):
        """Light the library action buttons based on what is currently ticked."""
        sel = self._selected_library_entries()
        n = len(sel)
        if n == 0:
            self.lib_sel_lbl.config(text="Tick recordings above to combine them.")
            for btn in (self.lib_btn_video, self.lib_btn_multi, self.lib_btn_mix,
                        self.lib_btn_convert, self.lib_btn_transcribe):
                btn.config(state="disabled")
            return
        all_video = all(e.get("video") for e in sel)
        total_audio = sum(len(e.get("audio", [])) for e in sel)
        word = "recording" if n == 1 else "recordings"
        self.lib_sel_lbl.config(text=f"{n} {word} selected.")
        # Video: only when every selected take has a video.
        self.lib_btn_video.config(state=("normal" if all_video else "disabled"))
        if not all_video:
            self._lib_tips["video"].set_text(
                "Needs a screen recording in EVERY ticked take - untick the "
                "audio-only ones, or use an audio button instead.")
        else:
            self._lib_tips["video"].set_text(
                "Joins the ticked recordings end to end into one video with "
                "their sound.")
        # Multitrack: needs at least two audio tracks across the selection.
        self.lib_btn_multi.config(state=("normal" if total_audio >= 2 else "disabled"))
        self._lib_tips["multi"].set_text(
            "One WAV where every ticked track keeps its own channel - great "
            "for editing." if total_audio >= 2 else
            "Needs at least two audio tracks across the ticked recordings.")
        # Mix: any audio present.
        self.lib_btn_mix.config(state=("normal" if total_audio >= 1 else "disabled"))
        self._lib_tips["mix"].set_text(
            "Everything summed into one stereo file." if total_audio else
            "No audio in the current selection.")
        # Convert: each ticked recording is exported on its own.
        self.lib_btn_convert.config(state="normal")
        # Transcribe: anything with audio or video qualifies.
        has_media = any(e.get("audio") or e.get("video") for e in sel)
        self.lib_btn_transcribe.config(
            state=("normal" if has_media else "disabled"))

    def _set_all_ticks(self, on):
        for r in self._lib_rows:
            r["var"].set(on)
        self._update_library_buttons()

    def _selected_library_entries(self):
        return [r["entry"] for r in self._lib_rows if r["var"].get()]

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
            messagebox.showinfo("Combine recordings",
                                "Tick at least one recording first.")
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
                messagebox.showinfo(
                    "Need video",
                    "Every ticked recording must have a video for this. "
                    "Untick the audio-only ones, or use an audio option instead.")
                return
            ext = self.container_var.get()
            out = self._unique_path(
                os.path.join(out_dir, f"{prefix}_merged_{stamp}.{ext}"))
            sessions = [{"audio": e.get("audio", []), "video": e.get("video", "")}
                        for e in sel]
            self._run_combine(
                lambda: combine.concat_sessions(sessions, out, True), out)
        elif mode == "multitrack":
            audio = self._all_selected_audio(sel)
            if len(audio) < 2:
                messagebox.showinfo("Need more tracks",
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
                messagebox.showinfo("No audio", "No audio in the selection.")
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
            messagebox.showinfo("Transcribe",
                                "Tick at least one recording first.")
            return
        self._transcribe_entries(sel)

    def _transcribe_entries(self, entries):
        if self._transcribe_busy:
            messagebox.showinfo("Please wait",
                                "A transcription is already running.")
            return
        exe = self._scrivox_exe
        if not exe or not os.path.isfile(exe):
            # Scrivox was moved/removed since detection; re-check and hide.
            self._refresh_library()
            if not self._scrivox_exe:
                messagebox.showinfo(
                    "Scrivox not found",
                    "Scrivox is no longer where it was detected. Put it back, "
                    "reinstall it, or set its location in Settings > Saving > "
                    "Transcription (Scrivox), then try again.")
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
            self.busy_bar.stop()
            self.busy_bar.config(mode="determinate", maximum=n, value=0)
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
                self._safe_after(
                    lambda i=i: self.busy_bar.config(value=i))
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
        if not self.recording and not self._finalizing:
            self.status_lbl.config(text=text)

    def _restore_status(self):
        """Put the status label back to whatever is still going on, in
        priority order, so finishing one background job never hides another."""
        if self.recording:
            self.status_lbl.config(text="Recording...")
        elif self._finalizing:
            self.status_lbl.config(text="Finalizing recording...")
        elif self._combine_busy:
            self.status_lbl.config(
                text="Combining... (this can take a while for video)")
        elif self._transcribe_busy:
            self.status_lbl.config(text="Transcribing with Scrivox...")
        else:
            self.status_lbl.config(text=self.IDLE_TEXT)
        if not self._combine_busy and not self._transcribe_busy:
            self._set_busy(False)

    def _transcribe_done(self, results):
        self._transcribe_busy = False
        self._restore_status()
        # Success detail is a LIST of transcript paths (per-track mode can
        # produce several per recording); failure detail is an error string.
        done = [(n, d) for n, ok, d in results if ok]
        failed = [(n, d) for n, ok, d in results if not ok]
        paths = [p for _, ps in done for p in ps]
        for p in paths:
            log.info("Transcript saved: %s", p)
        for n, d in failed:
            log.error("Transcription failed for '%s': %s", n, str(d)[:800])
        if done and not failed:
            shown = paths if len(paths) <= 12 else (
                paths[:12] + [f"... and {len(paths) - 12} more"])
            if messagebox.askyesno(
                    "Transcription complete",
                    f"Saved {len(paths)} transcript"
                    + ("" if len(paths) == 1 else "s") + ":\n"
                    + "\n".join(shown)
                    + "\n\nShow the first one in its folder?"):
                self._reveal_path(paths[0])
        elif done:
            messagebox.showwarning(
                "Transcription partly done",
                f"Saved {len(paths)} transcript(s), but "
                f"{len(failed)} recording(s) failed:\n\n"
                + "\n".join(f"{n}: {str(d)[:200]}" for n, d in failed))
        else:
            messagebox.showerror(
                "Transcription failed",
                "No transcripts were made.\n\n"
                + "\n".join(f"{n}: {str(d)[:300]}" for n, d in failed))

    def _reveal_path(self, path):
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                os.startfile(os.path.dirname(path))
        except Exception as e:
            log.warning("reveal transcript failed: %s", e)

    def _modal_dialog(self, title):
        """Shared boilerplate for the app's small option dialogs."""
        win = tk.Toplevel(self)
        win.title(title)
        win.configure(bg=COLORS["bg"])
        win.transient(self)
        win.grab_set()
        win.resizable(False, False)
        try:
            ip = paths.icon_path()
            if ip:
                win.iconbitmap(ip)
        except Exception:
            pass
        return win

    def _finish_dialog(self, win, ok_fn, focus=None):
        """Keyboard parity + centering: Enter confirms, Escape cancels."""
        win.bind("<Return>", lambda e: ok_fn())
        win.bind("<Escape>", lambda e: win.destroy())
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        win.update_idletasks()
        try:
            x = self.winfo_rootx() + (self.winfo_width() - win.winfo_width()) // 2
            y = self.winfo_rooty() + (self.winfo_height() - win.winfo_height()) // 3
            win.geometry(f"+{max(0, x)}+{max(0, y)}")
        except Exception:
            pass
        (focus or win).focus_set()

    _SCRIVOX_USE_SETTING = "Use Scrivox setting"

    def _transcribe_dialog(self, n_entries, any_video, multi_track,
                           any_combinable=False):
        """Modal dialog for transcription options.
        Returns a scrivox_bridge.default_options()-shaped dict, or None."""
        win = self._modal_dialog("Transcribe with Scrivox")
        result = {"value": None}
        frm = ttk.Frame(win, style="TFrame")
        frm.pack(fill="both", expand=True, padx=16, pady=14)

        word = "recording" if n_entries == 1 else "recordings"
        ttk.Label(frm, text=f"Transcribe {n_entries} {word}:",
                  style="Header.TLabel").pack(anchor="w")

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
        SegmentedControl(frm, preset_var, preset_opts).pack(
            fill="x", pady=(6, 10))

        mode_var = tk.StringVar(value="audio")
        options = [("audio", "Transcribe the audio")]
        if any_video:
            options.append(("vision",
                            "Transcribe the audio + describe what's on screen"))
        SegmentedControl(frm, mode_var, options).pack(fill="x", pady=(0, 4))

        # ---- what Scrivox reads (shown whenever combining can happen) ----
        input_var = tk.StringVar(value="mix")
        output_var = tk.StringVar(value="separate")
        combo_var = tk.StringVar(value="auto")
        if any_combinable:
            combined_text = ("One combined file per recording - every audio "
                             "track merged into one, kept next to the "
                             "recording" if not any_video else
                             "One combined file per recording - every audio "
                             "track + the screen video merged into one video "
                             "file, kept next to the recording")
            ttk.Label(frm, text="What Scrivox transcribes:").pack(
                anchor="w", pady=(8, 0))
            SegmentedControl(frm, input_var, [
                ("mix", combined_text),
                ("tracks", "Each audio track separately (per mic/playback)"),
            ]).pack(fill="x", pady=(4, 0))

            # Reuse policy for the combined file. In per-track mode it only
            # matters for the screen-description pass, so it hides unless
            # that pass will actually run.
            reuse_row = ttk.Frame(frm, style="TFrame")
            ttk.Label(reuse_row,
                      text="If a combined file was already made with SRR:"
                      ).pack(anchor="w", pady=(8, 0))
            SegmentedControl(reuse_row, combo_var, [
                ("auto", "Use it - only build one if it's missing or older "
                         "than the tracks"),
                ("rebuild", "Build a fresh one now"),
            ]).pack(fill="x", pady=(4, 0))

            out_row = ttk.Frame(frm, style="TFrame")
            ttk.Label(out_row, text="Per-track results:").pack(
                anchor="w", pady=(8, 0))
            SegmentedControl(out_row, output_var, [
                ("separate", "A transcript file per track"),
                ("merged", "One combined file: screen descriptions + every "
                           "track's transcript"),
            ]).pack(fill="x", pady=(4, 0))

            def _relayout(*_a):
                per_track = input_var.get() == "tracks"
                # Repack in a fixed order so the rows never swap positions:
                # [what Scrivox transcribes] -> reuse -> per-track -> Save as.
                reuse_row.pack_forget()
                out_row.pack_forget()
                if not per_track or mode_var.get() == "vision":
                    reuse_row.pack(fill="x", before=save_row)
                if per_track:
                    out_row.pack(fill="x", before=save_row)
                win.geometry("")  # re-fit the dialog to its content
            input_var.trace_add("write", _relayout)
            mode_var.trace_add("write", _relayout)

        # ---- output format (after the input choices - it describes results)
        save_row = ttk.Frame(frm, style="TFrame")
        save_row.pack(fill="x", pady=(8, 0))
        ttk.Label(save_row, text="Save as:").pack(side="left")
        ttk.Combobox(save_row, textvariable=fmt_var, state="readonly",
                     width=22,
                     values=list(scrivox_bridge.TRANSCRIBE_FORMATS.keys())
                     ).pack(side="left", padx=6)
        if any_combinable:
            _relayout()

        # ---- More settings (collapsed by default) ----
        more_btn = ttk.Button(frm, text="More settings  ▸")
        more_btn.pack(anchor="w", pady=(12, 0))
        adv = ttk.Frame(frm, style="TFrame")
        USE = self._SCRIVOX_USE_SETTING

        r1 = ttk.Frame(adv, style="TFrame")
        r1.pack(fill="x", pady=(8, 0))
        ttk.Label(r1, text="Identify speakers:").pack(side="left")
        dia_var = tk.StringVar(value=USE)
        ttk.Combobox(r1, textvariable=dia_var, state="readonly", width=18,
                     values=[USE, "On", "Off"]).pack(side="left", padx=6)
        ttk.Label(r1, text="How many:").pack(side="left", padx=(10, 0))
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
        ttk.Label(r3, text="Model:").pack(side="left")
        model_var = tk.StringVar(value=USE)
        ttk.Combobox(r3, textvariable=model_var, state="readonly", width=18,
                     values=[USE, "large-v3", "large-v3-turbo", "medium",
                             "small", "base", "tiny"]).pack(side="left", padx=6)
        ttk.Label(r3, text="Language:").pack(side="left", padx=(10, 0))
        lang_var = tk.StringVar(value="")
        ttk.Entry(r3, textvariable=lang_var, width=6).pack(side="left", padx=4)
        ttk.Label(r3, text="e.g. en, ko - blank = auto",
                  style="Muted.TLabel").pack(side="left", padx=4)

        r4 = ttk.Frame(adv, style="TFrame")
        r4.pack(fill="x", pady=(6, 0))
        ttk.Label(r4, text="Meeting summary:").pack(side="left")
        sum_var = tk.StringVar(value=USE)
        ttk.Combobox(r4, textvariable=sum_var, state="readonly", width=18,
                     values=[USE, "On", "Off"]).pack(side="left", padx=6)

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

        ttk.Label(frm, style="Muted.TLabel", justify="left", wraplength=460,
                  text="Each transcript is saved next to its recording. "
                  "Anything left on 'Use Scrivox setting' (and the API keys) "
                  "comes from Scrivox - use the button below to change those."
                  ).pack(anchor="w", pady=(10, 8))

        btns = ttk.Frame(frm, style="TFrame")
        btns.pack(fill="x")

        def open_settings():
            if not scrivox_bridge.open_scrivox(self._scrivox_exe):
                messagebox.showerror("Scrivox", "Could not launch Scrivox.",
                                     parent=win)

        ttk.Button(btns, text="Open Scrivox settings",
                   command=open_settings).pack(side="left")

        def _num(var, cast):
            s = var.get().strip()
            try:
                return cast(s) if s else None
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
                messagebox.showwarning(
                    "Combined file needs a text format",
                    "One combined file only works for Plain text or Markdown. "
                    "Pick one of those formats, or keep a file per track.",
                    parent=win)
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
            s = sum_var.get()
            opts["summarize"] = (True if s == "On"
                                 else False if s == "Off" else None)
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
            messagebox.showinfo("Convert", "Tick at least one recording first.")
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
        frm = ttk.Frame(win, style="TFrame")
        frm.pack(fill="both", expand=True, padx=16, pady=14)

        ttk.Label(frm, text="Convert to format:", style="Header.TLabel").pack(
            anchor="w")
        fmt_var = tk.StringVar(value="MP4 (H.264 + AAC)")
        labels = list(combine.CONVERT_FORMATS.keys())
        fmt_combo = ttk.Combobox(frm, textvariable=fmt_var, values=labels,
                                 state="readonly", width=34)
        fmt_combo.pack(anchor="w", pady=(4, 10))

        ttk.Label(frm, text="Audio handling:", style="Header.TLabel").pack(
            anchor="w")
        mode_var = tk.StringVar(value="mix")
        SegmentedControl(frm, mode_var, [
            ("mix", "Mix all audio into one stereo track"),
            ("tracks", "Keep each audio source as its own track"),
        ]).pack(fill="x", pady=(4, 8))

        info = ttk.Label(frm, style="Muted.TLabel", justify="left",
                         wraplength=360)
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
        btns.pack(fill="x")

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
        target = sel[0] if sel else (self._library[-1] if self._library else None)
        if not target:
            return
        d = target.get("out_dir") or ""
        try:
            if d and os.path.isdir(d):
                os.startfile(d)
        except Exception as e:
            log.warning("open library folder failed: %s", e)

    def _remove_selected_library(self):
        sel = self._selected_library_entries()
        if not sel:
            return
        if not messagebox.askyesno(
                "Remove from list",
                f"Remove {len(sel)} entr" + ("y" if len(sel) == 1 else "ies")
                + " from the list?\n\nThis does NOT delete the files on disk."):
            return
        sel_ids = {e["id"] for e in sel}
        self._library = [e for e in self._library if e["id"] not in sel_ids]
        self.cfg.set("recordings", self._library)
        self._refresh_library()

    # --- library right-click context menu -------------------------------- #
    def _show_library_menu(self, event, entry):
        menu = tk.Menu(self, tearoff=0, bg=COLORS["panel2"], fg=COLORS["fg"],
                       activebackground=COLORS["accent"], activeforeground="#06120f",
                       bd=0)
        menu.add_command(label="Rename...",
                         command=lambda: self._rename_entry(entry))
        menu.add_command(label="Open folder",
                         command=lambda: self._open_entry_folder(entry))
        menu.add_command(label="Show folder location",
                         command=lambda: self._reveal_entry_folder(entry))
        if self._scrivox_exe:
            menu.add_separator()
            menu.add_command(
                label="Transcribe with Scrivox...",
                command=lambda: self._transcribe_entries([entry]))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _open_entry_folder(self, entry):
        d = entry.get("out_dir") or ""
        if not d or not os.path.isdir(d):
            messagebox.showinfo("Open folder",
                                "This recording's folder no longer exists.")
            self._refresh_library()
            return
        try:
            os.startfile(d)
        except Exception as e:
            log.warning("open folder failed: %s", e)

    def _reveal_entry_folder(self, entry):
        """Show the recording's folder highlighted in the file manager."""
        d = entry.get("out_dir") or ""
        if not d or not os.path.isdir(d):
            messagebox.showinfo("Show folder location",
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
            try:
                os.startfile(os.path.dirname(d) or d)
            except Exception:
                pass

    def _rename_entry(self, entry):
        """Rename a recording's folder AND every file inside it to match, so the
        folder and its tracks share one name. Keeps all library-tracked paths
        pointing at the renamed files. Non-destructive otherwise."""
        from tkinter import simpledialog
        if getattr(self, "_combine_busy", False):
            messagebox.showinfo(
                "Please wait",
                "A merge/convert is running - rename when it finishes so its "
                "output isn't pulled out from under it.")
            return
        if getattr(self, "_transcribe_busy", False):
            messagebox.showinfo(
                "Please wait",
                "A transcription is running - rename when it finishes so its "
                "transcript isn't written into a folder that no longer exists.")
            return
        old_dir = entry.get("out_dir") or ""
        if not old_dir or not os.path.isdir(old_dir):
            messagebox.showinfo("Rename", "This recording's folder no longer exists.")
            self._refresh_library()
            return
        parent_dir = os.path.dirname(old_dir)
        old_base = os.path.basename(old_dir)
        new_name = simpledialog.askstring(
            "Rename recording",
            "New name (applies to the folder and every track inside):",
            initialvalue=old_base, parent=self)
        if not new_name:
            return
        # Sanitize to a safe, cross-platform name (no reserved chars).
        safe = "".join(c for c in new_name if c.isalnum() or c in " -_.()").strip()
        safe = safe.rstrip(". ")  # Windows dislikes trailing dot/space
        while "  " in safe:
            safe = safe.replace("  ", " ")
        if not safe:
            messagebox.showinfo(
                "Rename", "That name has no usable characters - use letters, "
                "numbers, spaces, - _ . ( ).")
            return
        if safe.split(".")[0].upper() in (
                "CON", "PRN", "AUX", "NUL",
                "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8",
                "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7",
                "LPT8", "LPT9"):
            messagebox.showinfo("Rename",
                                f"'{safe}' is a reserved name on Windows - "
                                "pick another.")
            return
        if safe == old_base:
            return
        new_dir = os.path.join(parent_dir, safe)
        case_only = safe.lower() == old_base.lower()
        if os.path.exists(new_dir) and not case_only:
            messagebox.showerror("Rename", f"A folder named '{safe}' already exists.")
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
            messagebox.showerror("Rename failed",
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
            messagebox.showwarning(
                "Some files kept their old names",
                "The folder was renamed, but these files are open in another "
                "program and kept their old names:\n\n  "
                + "\n  ".join(failures[:8])
                + ("\n  ..." if len(failures) > 8 else "")
                + "\n\nClose the program using them and rename again.")

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
        self.cfg.set("recordings", self._library)
        self._refresh_library()
        log.info("Renamed recording '%s' -> '%s' (%d file(s) renamed)",
                 old_base, safe, len(name_map))

    def _build_log(self, parent):
        inner = self._section(parent, "Live log")
        self.log_text = tk.Text(inner, height=16, bg="#141414", fg="#d0d0d0",
                                insertbackground="#d0d0d0", relief="flat", wrap="word",
                                font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")
        for tag, col in (("ERROR", COLORS["red"]), ("WARNING", COLORS["gold"]),
                         ("INFO", COLORS["fg"])):
            self.log_text.tag_config(tag, foreground=col)

    # -------------------------------------------------------------- config #
    def _restore_from_config(self):
        saved = self.cfg.get("audio_sources") or []
        any_added = False
        self._unresolved_sources = []
        for sel in saved:
            if resolve_selection(sel):
                self._add_row(preset=sel)
                any_added = True
            else:
                # Unplugged right now, not gone: keep it so its selection,
                # gain, and mute survive until it's plugged back in.
                self._unresolved_sources.append(dict(sel))
                log.info("Saved device not present now (kept in config): %s",
                         sel.get("name"))
        if not any_added:
            self._add_default_mic()
            self._add_system_playback()

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
        row = DeviceRow(self.rows_frame, self.all_devices, self._remove_row,
                        on_change=self._on_row_change, preset=preset,
                        gain=self._gain_for(preset), muted=self._muted_for(preset))
        row.pack(fill="x", pady=3)
        self._device_rows.append(row)
        row.combo.bind("<<ComboboxSelected>>",
                       lambda e, r=row: self._on_row_change("select", r))
        self._save_settings()
        return row

    def _on_row_change(self, what, row):
        if what == "gain":
            label = row.current_source_label()
            g = row.get_gain()
            if label:
                if self.recording and self.audio_rec:
                    self.audio_rec.set_gain(label, g)
                elif self.level_monitor:
                    self.level_monitor.set_gain(label, g)
            self._request_save()
        elif what == "mute":
            label = row.current_source_label()
            m = row.is_muted()
            if label:
                if self.recording and self.audio_rec:
                    self.audio_rec.set_muted(label, m)
                if self.level_monitor:
                    self.level_monitor.set_muted(label, m)
            self._save_settings()
        else:
            self._save_settings()
            self._refresh_monitor()

    def _remove_row(self, row):
        if row in self._device_rows:
            self._device_rows.remove(row)
        row.destroy()
        self._save_settings()
        self._refresh_monitor()

    def _add_default_mic(self):
        di, _ = default_devices()
        if di:
            self._add_row(preset={"name": di["name"], "kind": "input",
                                  "hostapi": di["hostapi"]})
        else:
            messagebox.showwarning("No microphone", "No input device found.")

    def _add_system_playback(self):
        _, do = default_devices()
        if do:
            self._add_row(preset={"name": do["name"], "kind": "loopback",
                                  "hostapi": do["hostapi"]})
        else:
            messagebox.showwarning("No playback device", "No output device found.")

    def _refresh_devices(self):
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
            self.status_lbl.config(text=f"{len(mons)} monitor(s) detected.")
        except Exception as e:
            log.exception("identify screens failed: %s", e)

    def _browse_folder(self):
        d = filedialog.askdirectory(initialdir=self.folder_var.get() or
                                    paths.default_recordings_dir())
        if d:
            self.folder_var.set(d)
            self._save_settings()

    # ---------------------------------------------------- level monitoring #
    def _stop_monitor(self):
        if self.level_monitor:
            try:
                self.level_monitor.stop()
            except Exception:
                pass
            self.level_monitor = None

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
            self.log_text.configure(state="normal")
            self.log_text.insert("end", msg + "\n", tag)
            if int(self.log_text.index("end-1c").split(".")[0]) > 1000:
                self.log_text.delete("1.0", "200.0")
            self.log_text.configure(state="disabled")
        if appended:
            self.log_text.see("end")

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
        if self.recording or self._starting or self._finalizing:
            return
        self._starting = True
        try:
            self._start_recording_inner()
        finally:
            self._starting = False

    def _start_recording_inner(self):
        sources = self._gather_sources()
        if not sources and not self.screen_enabled.get():
            messagebox.showwarning("Nothing selected",
                                   "Add at least one audio device or enable screen recording.")
            return
        self._save_settings()
        self._stop_monitor()  # release devices so the recorder owns them

        if self.ask_var.get():
            d = filedialog.askdirectory(initialdir=self.folder_var.get() or
                                        paths.default_recordings_dir())
            if not d:
                self._refresh_monitor()
                return
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
                messagebox.showerror(
                    "Not enough disk space",
                    f"Only {free_gb:.1f} GB free where recordings are saved.\n"
                    "Free up space or choose another folder before recording.")
                self._refresh_monitor()
                return
            if free_gb < 2.0:
                if not messagebox.askyesno(
                        "Low disk space",
                        f"Only {free_gb:.1f} GB free. Audio uses ~0.7 GB/hour per "
                        "device and screen recording much more.\n\nRecord anyway?"):
                    self._refresh_monitor()
                    return
        except Exception as e:
            log.warning("Disk space check skipped: %s", e)

        # ISO-style, file-safe session folder + base name (research-backed:
        # YYYY-MM-DD, no spaces or special chars, sorts chronologically).
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        out_dir = os.path.join(out_dir, f"SRR_{stamp}")
        os.makedirs(out_dir, exist_ok=True)
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
        except Exception:
            pass
        self.last_outputs = {"out_dir": out_dir, "audio": [], "video": None}

        if sources:
            try:
                self.audio_rec = AudioRecorder(
                    sources, self.output_mode.get(), out_dir, base,
                    target_samplerate=int(self.cfg.get("audio_target_samplerate")),
                    subtype=self.subtype.get(), on_error=self._on_subsystem_error)
                self.audio_rec.start()
                self.last_outputs["audio"] = list(self.audio_rec.output_files)
            except Exception as e:
                log.exception("Audio start failed: %s", e)
                # If capture threads were already spawned, shut them down so
                # they don't keep the devices open and write orphan files.
                try:
                    if self.audio_rec:
                        self.audio_rec.stop()
                except Exception:
                    pass
                self.audio_rec = None
                messagebox.showerror("Audio error", f"Could not start audio:\n{e}")
                self._refresh_monitor()
                return

        screen_error = None
        if self.screen_enabled.get():
            try:
                mons = list_monitors()
                num = self._selected_monitor_number()
                mon = next((m for m in mons if m["number"] == num),
                           mons[0] if mons else None)
                if mon is None:
                    raise RuntimeError("No monitor detected.")
                ext = self.container_var.get()
                vpath = os.path.join(out_dir, f"{base}_screen.{ext}")
                self.last_outputs["video"] = vpath
                self.screen_rec = ScreenRecorder(
                    mon, vpath, encoder_family=self.encoder_var.get(),
                    codec=self.codec_var.get(), container=ext,
                    framerate=self._fps(), quality=self.quality_var.get(),
                    capture_method=self.cfg.get("screen_capture_method"),
                    on_error=self._on_subsystem_error, available=self.encoders,
                    reliability=self.reliability_var.get())
                fam = self.screen_rec.start()
                self.last_outputs["video"] = self.screen_rec.final_path
                log.info("Screen recording via %s", fam)
            except Exception as e:
                log.exception("Screen start failed: %s", e)
                self.screen_rec = None
                screen_error = str(e)

        if self.audio_rec is None and self.screen_rec is None:
            # NOTHING actually started. Never enter the recording state - a red
            # button over zero capture is the worst possible lie this app can
            # tell. Surface the failure and bail out cleanly.
            self.status_lbl.config(text=self.IDLE_TEXT)
            messagebox.showerror(
                "Recording did NOT start",
                "Screen recording failed to start and no audio devices are "
                "selected.\n\n" + (screen_error or "Unknown error.")
                + "\n\nNothing is being recorded.")
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
        log.info("RECORDING STARTED -> %s", out_dir)
        if screen_error:
            # Raised after recording=True so the auto-restart path can act
            # on a start-time screen failure too (audio is still running).
            self._raise_gold_alert(
                f"Screen recording failed to start: {screen_error}")

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
        blocking=True (used by on_close) finalizes synchronously instead."""
        if not self.recording or self._finalizing:
            return
        log.info("Stopping recording...")
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
        self.record_btn.config(state="disabled",
                               text="Saving your recording...",
                               bg=COLORS["panel3"])
        self.status_lbl.config(text="Finalizing recording...")
        self._set_busy(True)

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
                self.record_btn.config(state="normal", text="●  RECORD",
                                       bg=COLORS["green"])
                self._restore_status()
                self._set_busy(False)
            except Exception:
                pass
            self._add_to_library(select_new=True)
            log.info("RECORDING STOPPED. Outputs: %s", self.last_outputs)
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

    def _offer_stop_combine(self):
        """Honor the 'When screen+audio ends' setting: ask / combine /
        separate. Non-destructive - the separate tracks are always kept."""
        action = self.on_stop_var.get()
        if action == "separate":
            return
        video = self.last_outputs.get("video") or ""
        audio = [a for a in (self.last_outputs.get("audio") or [])
                 if a and os.path.isfile(a)]
        if not (video and os.path.isfile(video) and audio):
            return
        if action == "ask":
            if not messagebox.askyesno(
                    "Combine now?",
                    "Make one video file with the sound included?\n\n"
                    "Your separate tracks are kept either way. (You can "
                    "change this prompt in Settings > Saving.)"):
                return
        out_dir = (self.last_outputs.get("out_dir")
                   or os.path.dirname(video))
        base = getattr(self, "_session_base", None) or "SRR"
        ext = os.path.splitext(video)[1].lstrip(".") or "mkv"
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        out = self._unique_path(
            os.path.join(out_dir, f"{base}_merged_{stamp}.{ext}"))
        self._run_combine(
            lambda: combine.combine_av(video, audio, out), out)

    def _set_recording_ui(self, on):
        if on:
            self.record_btn.config(text="■  STOP", bg=COLORS["red"],
                                   activebackground="#ff7b72")
            self.status_lbl.config(text="Recording...")
        else:
            self.record_btn.config(text="●  RECORD", bg=COLORS["green"],
                                   activebackground="#7fd687")
            self._restore_status()
            self.audio_light.set_state(COLORS["muted"], ": idle")
            self.screen_light.set_state(COLORS["muted"], ": off")
            self.elapsed_lbl.config(text="00:00:00")
        if self.tray:
            self.tray.set_recording(on)
        self._set_taskbar_recording(on)

    # -------------------------------------------------------- error/alert #
    def _on_subsystem_error(self, label, reason):
        self._log_queue.put((f"SUBSYSTEM ERROR [{label}]: {reason}", 40))
        self._safe_after(lambda: self._raise_gold_alert(f"[{label}] {reason}"))

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
        try:
            sources = self._gather_sources()
            out_dir = self.last_outputs.get("out_dir")
            if not sources or not out_dir:
                return
            old = self.audio_rec
            self.audio_rec = None
            if old is not None:
                # Stop off the Tk thread (stop() can block seconds per device)
                # and keep its finalized files - incl. any 4GiB rollover
                # segments - in last_outputs so the take stays complete.
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
            self.audio_rec = AudioRecorder(
                sources, self.output_mode.get(), out_dir, base,
                target_samplerate=int(self.cfg.get("audio_target_samplerate")),
                subtype=self.subtype.get(), on_error=self._on_subsystem_error)
            self.audio_rec.start()
            self.last_outputs.setdefault("audio", []).extend(self.audio_rec.output_files)
            log.info("Audio subsystem restarted -> %s", self.audio_rec.output_files)
        except Exception as e:
            log.exception("Audio restart failed: %s", e)

    def _restart_screen(self):
        try:
            out_dir = self.last_outputs.get("out_dir")
            mons = list_monitors()
            mon = next((m for m in mons if m["number"] == self._selected_monitor_number()),
                       mons[0] if mons else None)
            if mon is None or not out_dir:
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
            self.screen_rec = ScreenRecorder(
                mon, vpath, encoder_family=self.encoder_var.get(),
                codec=self.codec_var.get(), container=ext,
                framerate=self._fps(), quality=self.quality_var.get(),
                capture_method=self.cfg.get("screen_capture_method"),
                on_error=self._on_subsystem_error, available=self.encoders,
                reliability=self.reliability_var.get())
            self.screen_rec.start()
            # Reset growth tracking: the new (smaller) file must not have to
            # out-grow the old one's byte count before it registers as alive.
            self._screen_last_size = -1
            self._screen_last_grow = time.monotonic()
            log.info("Screen subsystem restarted -> %s", vpath)
        except Exception as e:
            log.exception("Screen restart failed: %s", e)

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
        # Run any UI callbacks whose direct after() scheduling failed. Each is
        # guarded on its own so one bad callback can't starve the rest.
        try:
            for _ in range(50):
                fn = self._ui_calls.get_nowait()
                try:
                    fn()
                except Exception as e:
                    self._poll_err("uicall", e)
        except queue.Empty:
            pass
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
                self.audio_light.set_state(COLORS["green"], ": REC")
            else:
                self.audio_light.set_state(COLORS["gold"], ": NO DATA")
            self.elapsed_lbl.config(text=_fmt_elapsed(a["elapsed"]))
        else:
            self.audio_light.set_state(COLORS["muted"], ": off")
        if self.screen_rec:
            s = self.screen_rec.get_status()
            if s["alive"]:
                self.screen_light.set_state(COLORS["green"],
                                            f": REC {s['size']//1024//1024}MB")
            else:
                self.screen_light.set_state(COLORS["gold"], ": STOPPED")
        else:
            self.screen_light.set_state(COLORS["muted"], ": off")

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
            self.status_lbl.config(
                text=f"Combining... ({len(self._combine_queue)} more queued)")
            self._set_busy(True)
            return
        self._combine_busy = True
        self.status_lbl.config(text="Combining... (this can take a while for video)")
        self._set_busy(True)
        log.info("Combine started -> %s", out)

        def work():
            try:
                ok, detail = fn()
            except Exception as e:
                ok, detail = False, str(e)
            self._safe_after(lambda: self._combine_done(ok, out, detail))
        threading.Thread(target=work, name="combine", daemon=True).start()

    def _combine_done(self, ok, out, detail):
        self._combine_busy = False
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
        self._restore_status()
        self._refresh_library()  # the merged files may add new session folders
        saved = [o for k, o, _ in results if k]
        failed = [(o, d) for k, o, d in results if not k]
        if saved and not failed:
            word = "it" if len(saved) == 1 else "the first one"
            # Basenames + one folder line: full absolute paths per file wrap
            # horribly for deep folders.
            names = "\n".join("  " + os.path.basename(o) for o in saved)
            if messagebox.askyesno(
                    "Merge complete",
                    f"Saved:\n{names}\n\nIn: {os.path.dirname(saved[0])}"
                    + f"\n\nShow {word} in the folder?"):
                folder = os.path.dirname(saved[0])
                try:
                    if os.path.isdir(folder):
                        os.startfile(folder)
                except Exception as e:
                    log.warning("open folder failed: %s", e)
        elif saved:
            messagebox.showwarning(
                "Merge partly complete",
                "Saved:\n" + "\n".join(saved) + "\n\nFailed:\n"
                + "\n".join(f"{os.path.basename(o)}: {str(d)[-200:]}"
                            for o, d in failed))
        else:
            messagebox.showerror(
                "Combine failed",
                "The merge did not complete.\n\n"
                + "\n\n".join(str(d)[-400:] for _, d in failed))

    # ------------------------------------------------------------- close #
    def on_close(self):
        # The teardown below must ONLY run when the user really is quitting.
        # (A previous version ran it from a finally: even when the user
        # answered "No, keep recording" - destroying the window and orphaning
        # the recording. Never put the cancel return inside that try.)
        if getattr(self, "_closing", False):
            return
        if self.recording:
            if not messagebox.askyesno("Quit",
                                       "Recording is active. Stop and quit?"):
                return
        if self._combine_busy:
            if not messagebox.askyesno(
                    "Quit",
                    "A merge/convert is still running and will be abandoned "
                    "if you quit now.\n\nQuit anyway?"):
                return
        if self._transcribe_busy:
            if not messagebox.askyesno(
                    "Quit",
                    "A transcription is still running. If you quit now, "
                    "Scrivox keeps working in the background and the "
                    "transcript will still be saved next to the recording - "
                    "but this app won't be around to tell you when it's "
                    "done.\n\nQuit anyway?"):
                return
        if self.recording:
            try:
                self.stop_recording(blocking=True)
            except Exception:
                log.exception("stop during close failed")
        elif self._finalizing:
            # A background finalize is still flushing files - wait for it so
            # quitting can't truncate the recording it just made.
            deadline = time.monotonic() + 30.0
            while self._finalizing and time.monotonic() < deadline:
                try:
                    self.update()
                except Exception:
                    break
                time.sleep(0.05)
        try:
            self._save_settings()
        except Exception:
            pass
        # Stop the after() poll/meter loops cleanly so they don't fire on a
        # destroyed window (which would raise TclError during shutdown).
        self._closing = True
        self._stop_monitor()
        if self.hotkeys:
            try:
                self.hotkeys.stop()
            except Exception:
                pass
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        try:
            self.destroy()
        except Exception:
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


def run():
    _enable_dpi_awareness()
    try:
        app = App()
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        log.error("FATAL startup error: %s\n%s", e, tb)
        try:
            crash = os.path.join(paths.data_dir(), "startup_crash.txt")
            with open(crash, "w", encoding="utf-8") as fh:
                fh.write(tb)
        except Exception:
            crash = "(could not write crash file)"
        try:
            from tkinter import messagebox as _mb
            _mb.showerror(
                "SimpleReliableRecorder failed to start",
                f"{e}\n\nDetails written to:\n{crash}")
        except Exception:
            pass
        raise
    app.mainloop()
