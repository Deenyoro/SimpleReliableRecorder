"""Main GUI for SimpleReliableRecorder.

Simple by design: pick devices, balance levels, hit RECORD. Everything else
(crash-safe writing, resilience, the gold alert, screen capture, combine) hangs
off that core flow.
"""

import os
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

from recorder import (alerts, combine, ffmpeg_tools, hotkeys, library, paths,
                      screen as screenmod, tray, watchdog)
from recorder.audio import (AudioRecorder, CaptureSource, LevelMonitor,
                            default_devices, list_devices, resolve_selection)
from recorder.config import ConfigManager
from recorder.logging_setup import get_logger, install_inapp_handler
from recorder.screen import ScreenRecorder, list_monitors
from ui.widgets import (COLORS, DeviceRow, GoldBanner, ScrollFrame,
                        SegmentedControl, StatusLight, ToggleSwitch,
                        apply_dark_theme)

log = get_logger("gui")


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
        self._log_queue = queue.Queue()
        self._device_rows = []
        self.level_monitor = None
        self._save_job = None
        self._combine_busy = False

        self.settings_win = None
        self.tray = None
        self.hotkeys = None
        self._make_vars()
        self._build_ui()
        install_inapp_handler(self._enqueue_log)
        self._restore_from_config()
        self._refresh_monitor()
        self._setup_tray()
        self._setup_hotkeys()
        self._poll()
        self._meter_loop()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        log.info("GUI ready. %d devices, encoders=%s", len(self.all_devices),
                 self.encoders)

    # ------------------------------------------------------- tray + hotkeys #
    def _setup_tray(self):
        if not self.tray_var.get():
            return
        self.tray = tray.TrayIcon(
            on_show=lambda: self.after(0, self._show_window),
            on_toggle_record=lambda: self.after(0, self._toggle_record),
            on_quit=lambda: self.after(0, self.on_close),
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
            self.after(0, lambda: self._apply_hotkey_mute(target, muted)))
        self._reconfigure_hotkeys()

    def _reconfigure_hotkeys(self):
        if not self.hotkeys:
            return
        self.hotkeys.configure(
            enabled=self.ptt_enabled_var.get(),
            hotkey=self.ptt_hotkey_var.get().strip(),
            mode=self.ptt_mode_var.get(),
            target=self.ptt_target_var.get())

    def _apply_hotkey_mute(self, target, muted):
        """Mute/unmute the hotkey's target device(s). Empty target = all mics."""
        for row in self._device_rows:
            d = row.get_selection()
            if not d:
                continue
            key = f'{d["name"]}|{d["kind"]}'
            is_target = (target == key) if target else (d["kind"] == "input")
            if is_target:
                row.set_muted(muted, notify=True)

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
        for v in (self.live_levels_var, self.output_mode, self.subtype,
                  self.screen_enabled, self.monitor_var, self.encoder_var,
                  self.container_var, self.codec_var, self.fps_var,
                  self.quality_var, self.reliability_var, self.folder_var,
                  self.ask_var, self.on_stop_var, self.autorestart_var,
                  self.watchdog_var, self.sound_var, self.banner_var,
                  self.taskbar_var, self.msgbox_var, self.tray_var,
                  self.ptt_enabled_var, self.ptt_hotkey_var,
                  self.ptt_target_var, self.ptt_mode_var):
            v.trace_add("write", lambda *a: self._save_settings())
        self.live_levels_var.trace_add("write", lambda *a: self._refresh_monitor())
        for v in (self.ptt_enabled_var, self.ptt_hotkey_var,
                  self.ptt_target_var, self.ptt_mode_var):
            v.trace_add("write", lambda *a: self._reconfigure_hotkeys())

    def _build_ui(self):
        root = ttk.Frame(self, style="TFrame")
        root.pack(fill="both", expand=True, padx=14, pady=12)

        header = ttk.Frame(root, style="TFrame")
        header.pack(fill="x", pady=(0, 10))
        ttk.Label(header, text="SimpleReliableRecorder", style="Title.TLabel").pack(side="left")
        ttk.Button(header, text="Settings", command=self._open_settings).pack(
            side="left", padx=14)
        self.audio_light = StatusLight(header, "Audio: idle")
        self.audio_light.pack(side="right", padx=4)
        self.screen_light = StatusLight(header, "Screen: off")
        self.screen_light.pack(side="right", padx=4)
        self.elapsed_lbl = ttk.Label(header, text="00:00:00", style="Header.TLabel")
        self.elapsed_lbl.pack(side="right", padx=12)

        body = ttk.Frame(root, style="TFrame")
        body.pack(fill="both", expand=True)
        left_scroll = ScrollFrame(body)
        left_scroll.pack(side="left", fill="both", expand=True, padx=(0, 10))
        left = left_scroll.body
        right = ttk.Frame(body, style="TFrame")
        right.pack(side="left", fill="both", expand=True)

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
        inner.pack(fill="x", padx=10, pady=8)
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
        ttk.Button(btns, text="Refresh", command=self._refresh_devices).pack(side="right")
        ToggleSwitch(btns, self.live_levels_var, text="Live meters").pack(
            side="right", padx=8)
        ttk.Label(inner, text="Balance each device with the faders; meters are live.",
                  style="Muted.TLabel").pack(anchor="w", pady=(8, 0))

    def _build_screen(self, parent):
        inner = self._section(parent, "Screen recording  (optional)")
        top = ttk.Frame(inner, style="TFrame")
        top.pack(fill="x")
        ttk.Label(top, text="Record a screen", style="Panel.TLabel").pack(side="left")
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
        ttk.Button(row, text="Identify screens",
                   command=self._identify_screens).pack(side="left", padx=6)
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
        # scrolling. The minimum is enforced so the user cannot shrink it below
        # readable width.
        win.geometry("760x780")
        win.minsize(720, 560)
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
                   command=win.destroy).pack(side="right")

        scroll = ScrollFrame(win)
        scroll.pack(fill="both", expand=True, padx=12, pady=(12, 0))
        p = scroll.body

        # --- Audio output ---
        a = self._section(p, "Audio output")
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
        s = self._section(p, "Screen recording quality")
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
        o = self._section(p, "Output location")
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
        tsec = self._section(p, "System tray")
        ToggleSwitch(tsec, self.tray_var,
                     text="Show a tray icon (turns red while recording)").pack(
            anchor="w", pady=3)
        ttk.Label(tsec, text="Takes effect on next launch.",
                  style="Muted.TLabel").pack(anchor="w", pady=(4, 0))

        # --- Push to talk / push to mute ---
        psec = self._section(p, "Push to talk / push to mute hotkey")
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

        # --- Resilience ---
        rsec = self._section(p, "Resilience")
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
        al = self._section(p, "Alert me if recording stops")
        for text, var in (("Play a sound", self.sound_var),
                          ("Flashing gold banner", self.banner_var),
                          ("Flash the taskbar button", self.taskbar_var),
                          ("Watchdog pop-up message box", self.msgbox_var)):
            ToggleSwitch(al, var, text=text).pack(anchor="w", pady=3)

        def _on_settings_close():
            self._save_settings()
            self.settings_win = None
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", _on_settings_close)

    def _build_record(self, parent):
        inner = self._section(parent, "Record")
        self.record_btn = tk.Button(inner, text="●  RECORD", command=self._toggle_record,
                                    bg=COLORS["green"], fg="#0b0b0b",
                                    font=("Segoe UI", 20, "bold"), relief="flat",
                                    height=2, activebackground="#7fd687")
        self.record_btn.pack(fill="x", pady=4)
        self.status_lbl = ttk.Label(inner, text="Idle.", style="Muted.TLabel")
        self.status_lbl.pack(anchor="w", pady=(6, 0))

    # ----------------------------------------------------- recordings library #
    def _build_library(self, parent):
        inner = self._section(parent, "Recordings library")
        desc = ttk.Label(
            inner, style="Muted.TLabel", justify="left",
            text="Past recordings stay listed here so you can keep recording, "
            "then tick any and merge them into one file with the buttons below. "
            "Right-click a recording to rename its folder (still tracked). "
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
        self.lib_empty = ttk.Label(inner, text="No saved recordings yet.",
                                   style="Muted.TLabel")
        self.lib_empty.pack(anchor="w", pady=(4, 0))

        self.lib_sel_lbl = ttk.Label(inner, text="Nothing selected.",
                                     style="Muted.TLabel")
        self.lib_sel_lbl.pack(anchor="w", pady=(8, 0))

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

        b2 = ttk.Frame(inner, style="TFrame")
        b2.pack(fill="x", pady=(2, 0))
        ttk.Button(b2, text="Open folder", width=12,
                   command=self._open_selected_library).pack(side="left")
        ttk.Button(b2, text="Remove", width=10,
                   command=self._remove_selected_library).pack(side="left", padx=6)
        ttk.Button(b2, text="Refresh", width=10,
                   command=lambda: self._refresh_library(rescan=True)).pack(side="right")
        self._refresh_library()

    def _add_to_library(self, select_new=False):
        audio = [a for a in (self.last_outputs.get("audio") or [])
                 if a and os.path.isfile(a)]
        video = self.last_outputs.get("video") or ""
        if not os.path.isfile(video):
            video = ""
        if not audio and not video:
            return
        self._lib_seq += 1
        entry = library.make_entry(
            entry_id=f"rec{self._lib_seq}-{int(self._record_start_mono)}",
            name=getattr(self, "_session_base", "recording"),
            out_dir=self.last_outputs.get("out_dir", ""),
            audio=audio, video=video,
            created=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
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
        # Prune anything whose files vanished, then rebuild the checklist.
        self._library, pruned = library.prune(self._library)
        if rescan:
            try:
                known = {e.get("out_dir") for e in self._library}
                found = library.scan_folder(self.cfg.resolved_save_folder(),
                                            existing_dirs=known)
                if found:
                    found.sort(key=lambda e: e.get("created", ""))
                    self._library.extend(found)
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
            var = tk.BooleanVar(value=False)
            cb = ToggleSwitch(row, var,
                              text=f'{e["name"]}  ({library.summarize(e)})',
                              command=self._update_library_buttons)
            cb.pack(anchor="w")
            # Right-click a row to rename its folder (tracked automatically).
            for w in (row, cb, getattr(cb, "label", None), getattr(cb, "canvas", None)):
                if w is not None:
                    w.bind("<Button-3>", lambda ev, ent=e: self._rename_entry(ent))
            self._lib_rows.append({"frame": row, "var": var, "entry": e})
        has = bool(self._library)
        self.lib_empty.pack_forget() if has else self.lib_empty.pack(
            anchor="w", pady=(4, 0))
        self._update_library_buttons()

    def _update_library_buttons(self):
        """Light the library action buttons based on what is currently ticked."""
        sel = self._selected_library_entries()
        n = len(sel)
        if n == 0:
            self.lib_sel_lbl.config(text="Tick recordings above to combine them.")
            for btn in (self.lib_btn_video, self.lib_btn_multi, self.lib_btn_mix,
                        self.lib_btn_convert):
                btn.config(state="disabled")
            return
        all_video = all(e.get("video") for e in sel)
        total_audio = sum(len(e.get("audio", [])) for e in sel)
        word = "recording" if n == 1 else "recordings"
        self.lib_sel_lbl.config(text=f"{n} {word} selected.")
        # Video: only when every selected take has a video.
        self.lib_btn_video.config(state=("normal" if all_video else "disabled"))
        # Multitrack: needs at least two audio tracks across the selection.
        self.lib_btn_multi.config(state=("normal" if total_audio >= 2 else "disabled"))
        # Mix: any audio present.
        self.lib_btn_mix.config(state=("normal" if total_audio >= 1 else "disabled"))
        # Convert: one recording at a time (per-take format export).
        self.lib_btn_convert.config(state=("normal" if n == 1 else "disabled"))

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

        if mode == "video":
            if not all(e.get("video") for e in sel):
                messagebox.showinfo(
                    "Need video",
                    "Every ticked recording must have a video for this. "
                    "Untick the audio-only ones, or use an audio option instead.")
                return
            ext = self.container_var.get()
            out = os.path.join(out_dir, f"SRR_merged_{stamp}.{ext}")
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
            out = os.path.join(out_dir, f"SRR_multitrack_{stamp}.wav")
            self._run_combine(
                lambda: combine.merge_audio_to_channels(audio, out), out)
        else:  # mix
            audio = self._all_selected_audio(sel)
            if not audio:
                messagebox.showinfo("No audio", "No audio in the selection.")
                return
            out = os.path.join(out_dir, f"SRR_mixed_{stamp}.wav")
            self._run_combine(
                lambda: combine.mix_audio_to_stereo(audio, out), out)

    def _convert_selected_library(self):
        """Convert one selected recording to another format via a dialog."""
        sel = self._selected_library_entries()
        if len(sel) != 1:
            messagebox.showinfo("Convert",
                                "Select exactly one recording to convert.")
            return
        entry = sel[0]
        n_audio = len([a for a in entry.get("audio", []) if a])
        has_video = bool(entry.get("video"))
        choice = self._convert_dialog(n_audio, has_video)
        if not choice:
            return
        fmt_label, audio_mode = choice
        spec = combine.CONVERT_FORMATS[fmt_label]
        ext = spec[0]
        out_dir = entry.get("out_dir") or self.cfg.resolved_save_folder()
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        out = os.path.join(out_dir, f"{entry['name']}_converted_{stamp}.{ext}")
        self._run_combine(
            lambda: combine.convert(entry, out, fmt_label, audio_mode), out)

    def _convert_dialog(self, n_audio, has_video):
        """Modal dialog to pick an output format and audio handling.
        Returns (fmt_label, audio_mode) or None if cancelled."""
        win = tk.Toplevel(self)
        win.title("Convert recording")
        win.configure(bg=COLORS["bg"])
        win.transient(self)
        win.grab_set()
        try:
            ip = paths.icon_path()
            if ip:
                win.iconbitmap(ip)
        except Exception:
            pass

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
            info.config(text=txt)
        fmt_var.trace_add("write", describe)
        describe()

        btns = ttk.Frame(frm, style="TFrame")
        btns.pack(fill="x")

        def ok():
            result["value"] = (fmt_var.get(), mode_var.get())
            win.destroy()

        ttk.Button(btns, text="Convert", style="Accent.TButton",
                   command=ok).pack(side="right")
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(
            side="right", padx=(0, 8))

        win.update_idletasks()
        # Center over the main window.
        try:
            x = self.winfo_rootx() + (self.winfo_width() - win.winfo_width()) // 2
            y = self.winfo_rooty() + (self.winfo_height() - win.winfo_height()) // 3
            win.geometry(f"+{max(0, x)}+{max(0, y)}")
        except Exception:
            pass
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

    def _rename_entry(self, entry):
        """Rename a recording's folder on disk via right-click, keeping all of
        the library's tracked paths pointing at the new location."""
        from tkinter import simpledialog
        old_dir = entry.get("out_dir") or ""
        if not old_dir or not os.path.isdir(old_dir):
            messagebox.showinfo("Rename", "This recording's folder no longer exists.")
            self._refresh_library()
            return
        parent_dir = os.path.dirname(old_dir)
        current = os.path.basename(old_dir)
        new_name = simpledialog.askstring(
            "Rename recording",
            "New folder name:", initialvalue=current, parent=self)
        if not new_name:
            return
        # Sanitize to a safe folder name.
        safe = "".join(c for c in new_name if c.isalnum() or c in " -_.()").strip()
        if not safe or safe == current:
            return
        new_dir = os.path.join(parent_dir, safe)
        if os.path.exists(new_dir):
            messagebox.showerror("Rename", f"A folder named '{safe}' already exists.")
            return
        try:
            os.rename(old_dir, new_dir)
        except Exception as e:
            messagebox.showerror("Rename failed",
                                 f"Could not rename the folder:\n{e}")
            return
        # Remap every tracked path from old_dir -> new_dir for this entry.
        def remap(p):
            if p and os.path.commonpath([os.path.abspath(p), os.path.abspath(old_dir)]) \
                    == os.path.abspath(old_dir):
                return os.path.join(new_dir, os.path.relpath(p, old_dir))
            return p
        entry["out_dir"] = new_dir
        entry["name"] = safe
        entry["audio"] = [remap(a) for a in entry.get("audio", [])]
        if entry.get("video"):
            entry["video"] = remap(entry["video"])
        self.cfg.set("recordings", self._library)
        self._refresh_library()
        log.info("Renamed recording folder -> %s", new_dir)

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
        for sel in saved:
            if resolve_selection(sel):
                self._add_row(preset=sel)
                any_added = True
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
        sels, gains, mutes = [], {}, {}
        for row in self._device_rows:
            d = row.get_selection()
            if d:
                sels.append({"id": d.get("id", ""), "name": d["name"],
                             "kind": d["kind"], "hostapi": d["hostapi"]})
                key = f'{d["name"]}|{d["kind"]}'
                gains[key] = round(row.get_gain(), 3)
                mutes[key] = row.is_muted()
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
            "screen_framerate": int(self.fps_var.get()),
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
        })

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

        self.session_dir = os.path.join(paths.data_dir(), "session")
        os.makedirs(self.session_dir, exist_ok=True)
        watchdog.clear_alert(self.session_dir)
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
                messagebox.showerror("Audio error", f"Could not start audio:\n{e}")
                self.audio_rec = None
                self._refresh_monitor()
                return

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
                    framerate=int(self.fps_var.get()), quality=self.quality_var.get(),
                    capture_method=self.cfg.get("screen_capture_method"),
                    on_error=self._on_subsystem_error, available=self.encoders,
                    reliability=self.reliability_var.get())
                fam = self.screen_rec.start()
                self.last_outputs["video"] = self.screen_rec.final_path
                log.info("Screen recording via %s", fam)
            except Exception as e:
                log.exception("Screen start failed: %s", e)
                self.screen_rec = None
                self._raise_gold_alert(f"Screen recording failed to start: {e}")

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

        self._record_start_mono = time.monotonic()
        self.recording = True
        self._set_recording_ui(True)
        log.info("RECORDING STARTED -> %s", out_dir)

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
            st["screen_size"] = s["size"]
            # frame_age is seconds since ffmpeg last advanced its frame counter.
            # This is the real liveness signal: it keeps moving even on a static
            # screen (duplicated frames), whereas file size can sit flat for many
            # seconds due to output buffering. -1 = no frame reported yet.
            fa = s.get("frame_age", -1.0)
            st["screen_frame"] = s.get("frame", 0)
            st["screen_frame_age"] = fa
            # Healthy if the process is alive AND it reported a frame recently
            # (or we're still warming up / haven't seen the first frame yet).
            st["screen_progressing"] = bool(
                warming_up or fa < 0 or fa < 8.0)
        else:
            st["screen_enabled"] = False
        return st

    def stop_recording(self):
        if not self.recording:
            return
        log.info("Stopping recording...")
        self.recording = False
        if self.heartbeat:
            self.heartbeat.stop()
        if self.session_dir:
            watchdog.write_stop_flag(self.session_dir)
        if self.wd_proc:
            try:
                self.wd_proc.terminate()
            except Exception:
                pass
        if self.audio_rec:
            try:
                self.last_outputs["audio"] = self.audio_rec.stop()
            except Exception as e:
                log.exception("audio stop error: %s", e)
            self.audio_rec = None
        if self.screen_rec:
            try:
                self.last_outputs["video"] = self.screen_rec.stop()
            except Exception as e:
                log.exception("screen stop error: %s", e)
            self.screen_rec = None

        self._set_recording_ui(False)
        self._add_to_library(select_new=True)
        log.info("RECORDING STOPPED. Outputs: %s", self.last_outputs)
        self._refresh_monitor()

    def _set_recording_ui(self, on):
        if on:
            self.record_btn.config(text="■  STOP", bg=COLORS["red"],
                                   activebackground="#ff7b72")
            self.status_lbl.config(text="Recording...")
        else:
            self.record_btn.config(text="●  RECORD", bg=COLORS["green"],
                                   activebackground="#7fd687")
            self.status_lbl.config(text="Idle.")
            self.audio_light.set_state(COLORS["muted"], ": idle")
            self.screen_light.set_state(COLORS["muted"], ": off")
            self.elapsed_lbl.config(text="00:00:00")
        if self.tray:
            self.tray.set_recording(on)
        self._set_taskbar_recording(on)

    # -------------------------------------------------------- error/alert #
    def _on_subsystem_error(self, label, reason):
        self._log_queue.put((f"SUBSYSTEM ERROR [{label}]: {reason}", 40))
        self.after(0, lambda: self._raise_gold_alert(f"[{label}] {reason}"))

    def _raise_gold_alert(self, reason):
        if self.alerting and self.banner.winfo_manager():
            self.banner.show(reason)
            return
        self.alerting = True
        log.error("GOLD ALERT: %s", reason)
        if self.banner_var.get():
            self.banner.show(reason)
        if self.sound_var.get():
            alerts.beep()
        if self.taskbar_var.get():
            try:
                alerts.flash_taskbar(self.winfo_id())
            except Exception:
                pass
        if self.autorestart_var.get() and self.recording:
            self.after(500, self._auto_restart_failed)

    def _dismiss_alert(self):
        self.alerting = False
        alerts.stop_beep()
        try:
            alerts.stop_flash_taskbar(self.winfo_id())
        except Exception:
            pass
        if self.session_dir:
            watchdog.clear_alert(self.session_dir)

    def _auto_restart_failed(self):
        now = time.monotonic()
        if self.audio_rec is not None:
            a = self.audio_rec.get_status()
            secs = (time.monotonic() - a["last_write"]) if a["last_write"] else 999
            if (not a["any_active"] or secs > 4) and now - self._restart_cooldown.get("audio", 0) > 8:
                self._restart_cooldown["audio"] = now
                log.warning("Auto-restarting AUDIO subsystem...")
                self._restart_audio()
        if self.screen_rec is not None:
            s = self.screen_rec.get_status()
            if not s["alive"] and now - self._restart_cooldown.get("screen", 0) > 8:
                self._restart_cooldown["screen"] = now
                log.warning("Auto-restarting SCREEN subsystem...")
                self._restart_screen()

    def _restart_audio(self):
        try:
            sources = self._gather_sources()
            out_dir = self.last_outputs.get("out_dir")
            if not sources or not out_dir:
                return
            try:
                self.audio_rec.stop()
            except Exception:
                pass
            base = self._combine_base() + "_restart-" + datetime.now().strftime("%H%M%S")
            self._record_start_mono = time.monotonic()  # give the restart grace too
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
            ext = self.container_var.get()
            vpath = os.path.join(
                out_dir,
                f"{self._combine_base()}_screen-restart-{datetime.now():%H%M%S}.{ext}")
            self.screen_rec = ScreenRecorder(
                mon, vpath, encoder_family=self.encoder_var.get(),
                codec=self.codec_var.get(), container=ext,
                framerate=int(self.fps_var.get()), quality=self.quality_var.get(),
                capture_method=self.cfg.get("screen_capture_method"),
                on_error=self._on_subsystem_error, available=self.encoders,
                reliability=self.reliability_var.get())
            self.screen_rec.start()
            log.info("Screen subsystem restarted -> %s", vpath)
        except Exception as e:
            log.exception("Screen restart failed: %s", e)

    def _restart_recording(self):
        self._dismiss_alert()
        if self.recording:
            self.stop_recording()
        self.after(400, self.start_recording)

    # --------------------------------------------------------- poll loops #
    def _poll(self):
        try:
            self._drain_log()
            if self.recording:
                self._update_status_lights()
                self._check_watchdog_alert()
        except Exception as e:
            log.debug("poll error: %s", e)
        self.after(400, self._poll)

    def _meter_loop(self):
        try:
            self._update_meters()
        except Exception as e:
            log.debug("meter error: %s", e)
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
        if alert and not self.alerting:
            self._raise_gold_alert(alert.get("reason", "Recording problem detected."))

    # ----------------------------------------------------------- combine #
    def _combine_base(self):
        return getattr(self, "_session_base", None) or "SRR_recording"

    def _run_combine(self, fn, out):
        if getattr(self, "_combine_busy", False):
            messagebox.showinfo("Please wait",
                                "A combine/merge is already running.")
            return
        self._combine_busy = True
        self.status_lbl.config(text="Combining... (this can take a while for video)")
        log.info("Combine started -> %s", out)

        def work():
            try:
                ok, detail = fn()
            except Exception as e:
                ok, detail = False, str(e)
            self.after(0, lambda: self._combine_done(ok, out, detail))
        threading.Thread(target=work, name="combine", daemon=True).start()

    def _combine_done(self, ok, out, detail):
        self._combine_busy = False
        self.status_lbl.config(text="Idle.")
        if ok and os.path.isfile(out):
            log.info("Combined -> %s", out)
            self._refresh_library()  # the merged file may add a new session folder
            if messagebox.askyesno(
                    "Merge complete",
                    f"Saved:\n{out}\n\nShow it in the folder?"):
                folder = os.path.dirname(out)
                try:
                    if os.path.isdir(folder):
                        os.startfile(folder)
                except Exception as e:
                    log.warning("open folder failed: %s", e)
        else:
            log.error("Combine failed: %s", str(detail)[:800])
            messagebox.showerror(
                "Combine failed",
                "The merge did not complete.\n\n" + str(detail)[-800:])

    # ------------------------------------------------------------- close #
    def on_close(self):
        try:
            if self.recording:
                if not messagebox.askyesno("Quit", "Recording is active. Stop and quit?"):
                    return
                self.stop_recording()
            self._save_settings()
        finally:
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
            self.destroy()


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
