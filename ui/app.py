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

from recorder import alerts, combine, ffmpeg_tools, paths, screen as screenmod, watchdog
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
            w = min(1280, int(sw * 0.8))
            h = min(900, int(sh * 0.85))
            x = max(0, (sw - w) // 2)
            y = max(0, (sh - h) // 3)
            self.geometry(f"{w}x{h}+{x}+{y}")
            self.minsize(min(900, sw - 40), min(640, sh - 80))
        except Exception:
            self.geometry("1100x820")
            self.minsize(900, 640)
        try:
            self.state("zoomed")  # start maximized on Windows
        except Exception:
            pass
        ip = paths.icon_path()
        if ip:
            try:
                self.iconbitmap(ip)
            except Exception:
                pass

        self.cfg = ConfigManager()
        self.inputs, self.outputs = list_devices()
        self.all_devices = self.inputs + self.outputs
        self.encoders = ffmpeg_tools.probe_encoders()

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

        self.settings_win = None
        self._make_vars()
        self._build_ui()
        install_inapp_handler(self._enqueue_log)
        self._restore_from_config()
        self._refresh_monitor()
        self._poll()
        self._meter_loop()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        log.info("GUI ready. %d devices, encoders=%s", len(self.all_devices),
                 self.encoders)

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
        for v in (self.live_levels_var, self.output_mode, self.subtype,
                  self.screen_enabled, self.monitor_var, self.encoder_var,
                  self.container_var, self.codec_var, self.fps_var,
                  self.quality_var, self.reliability_var, self.folder_var,
                  self.ask_var, self.on_stop_var, self.autorestart_var,
                  self.watchdog_var, self.sound_var, self.banner_var,
                  self.taskbar_var, self.msgbox_var):
            v.trace_add("write", lambda *a: self._save_settings())
        self.live_levels_var.trace_add("write", lambda *a: self._refresh_monitor())

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
        self._build_combine(right)
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

    def _build_combine(self, parent):
        inner = self._section(parent, "After recording")
        ttk.Label(inner,
                  text="Every device is saved as its own track first (safest). "
                  "Optionally combine them below - originals are always kept.",
                  style="Muted.TLabel", wraplength=360, justify="left").pack(
            anchor="w")
        self.combine_info = ttk.Label(inner, text="No recording yet.",
                                      style="Muted.TLabel")
        self.combine_info.pack(anchor="w", pady=(6, 0))

        b = ttk.Frame(inner, style="TFrame")
        b.pack(fill="x", pady=(8, 0))
        self.btn_av = ttk.Button(b, text="Make one video with sound",
                                 command=self._combine_av, state="disabled")
        self.btn_av.pack(fill="x", pady=2)
        self.btn_merge = ttk.Button(
            b, text="Combine audio into one multitrack file",
            command=self._combine_merge, state="disabled")
        self.btn_merge.pack(fill="x", pady=2)
        self.btn_mix = ttk.Button(b, text="Mix all audio into one stereo file",
                                  command=self._combine_mix, state="disabled")
        self.btn_mix.pack(fill="x", pady=2)
        ttk.Button(inner, text="Open recording folder",
                   command=self._open_output).pack(anchor="w", pady=(8, 0))

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
        sels, gains = [], {}
        for row in self._device_rows:
            d = row.get_selection()
            if d:
                sels.append({"id": d.get("id", ""), "name": d["name"],
                             "kind": d["kind"], "hostapi": d["hostapi"]})
                gains[f'{d["name"]}|{d["kind"]}'] = round(row.get_gain(), 3)
        self.cfg.update({
            "audio_sources": sels,
            "audio_gains": gains,
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
        })

    # ------------------------------------------------------------- devices #
    def _gain_for(self, preset):
        if not preset:
            return 1.0
        gains = self.cfg.get("audio_gains") or {}
        return float(gains.get(f'{preset.get("name")}|{preset.get("kind")}', 1.0))

    def _add_row(self, preset=None):
        row = DeviceRow(self.rows_frame, self.all_devices, self._remove_row,
                        on_change=self._on_row_change, preset=preset,
                        gain=self._gain_for(preset))
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
                d, gain=row.get_gain(), track_name=track_name))
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
        self._update_combine_panel()
        log.info("RECORDING STOPPED. Outputs: %s", self.last_outputs)
        self._refresh_monitor()

        video = self.last_outputs.get("video")
        audio = self.last_outputs.get("audio") or []
        if video and audio:
            action = self.on_stop_var.get()
            if action == "combine":
                self._combine_av(auto=True)
            elif action == "ask":
                self.after(300, self._prompt_combine)

    def _prompt_combine(self):
        video = self.last_outputs.get("video")
        audio = self.last_outputs.get("audio") or []
        if not (video and audio):
            return
        yes = messagebox.askyesno(
            "Make one video with sound?",
            "Your recording is saved safely as separate tracks:\n\n"
            f"  - {os.path.basename(video)}  (screen)\n"
            f"  - {len(audio)} audio track" + ("s" if len(audio) != 1 else "")
            + "\n\n"
            "Make a single video file with the sound mixed in now?\n"
            "Your original separate tracks are always kept.")
        if yes:
            self._combine_av(auto=True)

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
    def _update_combine_panel(self):
        audio = [a for a in (self.last_outputs.get("audio") or []) if a]
        video = self.last_outputs.get("video")
        parts = []
        if audio:
            parts.append(f"{len(audio)} audio track" + ("s" if len(audio) != 1 else ""))
        if video:
            parts.append("1 screen video")
        if parts:
            self.combine_info.config(text="Last recording: " + " + ".join(parts) + ".")
        else:
            self.combine_info.config(text="No recording yet.")
        self.btn_av.config(state=("normal" if (video and audio) else "disabled"))
        self.btn_merge.config(state=("normal" if len(audio) >= 2 else "disabled"))
        self.btn_mix.config(state=("normal" if audio else "disabled"))

    def _combine_base(self):
        return getattr(self, "_session_base", None) or "SRR_recording"

    def _combine_av(self, auto=False):
        video = self.last_outputs.get("video")
        audio = self.last_outputs.get("audio") or []
        out_dir = self.last_outputs.get("out_dir")
        if not video or not audio:
            if not auto:
                messagebox.showinfo("Combine", "Need both video and audio.")
            return
        out = os.path.join(out_dir,
                           f"{self._combine_base()}_video-with-audio."
                           + self.container_var.get())
        self._run_combine(lambda: combine.combine_av(video, audio, out, "mix"), out)

    def _combine_merge(self):
        audio = self.last_outputs.get("audio") or []
        out_dir = self.last_outputs.get("out_dir")
        out = os.path.join(out_dir, f"{self._combine_base()}_audio-multitrack.wav")
        self._run_combine(lambda: combine.merge_audio_to_channels(audio, out), out)

    def _combine_mix(self):
        audio = self.last_outputs.get("audio") or []
        out_dir = self.last_outputs.get("out_dir")
        out = os.path.join(out_dir, f"{self._combine_base()}_audio-mixed.wav")
        self._run_combine(lambda: combine.mix_audio_to_stereo(audio, out), out)

    def _run_combine(self, fn, out):
        self.status_lbl.config(text="Combining...")

        def work():
            ok, detail = fn()
            self.after(0, lambda: self._combine_done(ok, out, detail))
        threading.Thread(target=work, daemon=True).start()

    def _combine_done(self, ok, out, detail):
        self.status_lbl.config(text="Idle.")
        if ok:
            log.info("Combined -> %s", out)
            if messagebox.askyesno(
                    "Done",
                    f"Saved:\n{os.path.basename(out)}\n\nShow it in the folder?"):
                self._open_output()
        else:
            messagebox.showerror("Combine failed", str(detail)[:800])

    def _open_output(self):
        d = self.last_outputs.get("out_dir") or self.cfg.resolved_save_folder()
        try:
            os.startfile(d)
        except Exception as e:
            log.warning("open folder failed: %s", e)

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
