"""Persistent JSON configuration for SimpleReliableRecorder.

Stores device selections (by name so they survive index changes), output mode,
save folder, screen-recording settings, encoder/container choices, and
resilience toggles. Same pattern as the Whisper project's ConfigManager.
"""

import copy
import json
import os
import threading

from . import paths
from .logging_setup import get_logger

log = get_logger("config")

DEFAULTS = {
    # --- audio ---
    # Each selection: {"name": <device name>, "kind": "input"|"loopback", "hostapi": <str>}
    "audio_sources": [],
    # "separate" (one WAV per device, safest) | "channels" (one N-channel WAV) | "mixed"
    "audio_output_mode": "separate",
    "audio_target_samplerate": 48000,
    "audio_subtype": "PCM_16",  # PCM_16 (universal) | FLOAT (lossless, no clipping)
    # Per-device gain, keyed by "<name>|<kind>". 1.0 = unity.
    "audio_gains": {},
    # Per-device mute state, keyed by "<name>|<kind>". True = muted (writes silence).
    "audio_mutes": {},
    # Show live OBS-style level meters on the device rows (idle + recording).
    "live_levels": True,

    # --- system tray ---
    "tray_enabled": True,         # show a tray icon (red while recording)

    # --- push to talk / push to mute hotkey ---
    # hotkey is a single key/combo string understood by the `keyboard` library,
    # e.g. "f8", "ctrl+space". Empty = disabled.
    "ptt_enabled": False,
    "ptt_hotkey": "",
    # which device the hotkey controls, keyed by "<name>|<kind>"; empty = all mics.
    "ptt_target": "",
    # "ptt" (push to talk: held = unmuted, released = muted),
    # "ptm" (push to mute: held = muted, released = unmuted),
    # "toggle" (each press flips mute).
    "ptt_mode": "ptt",

    # --- screen ---
    "screen_enabled": False,
    "screen_monitor": 1,          # 1-based monitor number as shown by "Identify screens"
    "screen_framerate": 30,
    "screen_encoder": "auto",     # auto | nvenc | qsv | amf | cpu
    "screen_container": "mkv",    # mkv (crash-safe, default) | mp4
    "screen_codec": "h264",       # h264 | hevc
    "screen_quality": "balanced", # balanced | high | small
    "screen_capture_method": "gdigrab",  # gdigrab (compatible) | ddagrab (gpu)
    # crash safety for video: standard | fragmented | hybrid (record fragmented,
    # remux to clean mp4 on stop). MKV is always crash resilient regardless.
    "screen_reliability": "hybrid",

    # --- output location ---
    "save_folder": "",            # empty => default Videos\SimpleReliableRecorder
    "ask_every_time": False,

    # --- recordings library ---
    # Persistent list of finished recordings so you can keep recording without
    # closing the app and combine several into one file later. Each entry:
    # {name, out_dir, audio:[paths], video:path|"", created:iso}. Entries whose
    # files were moved/deleted are pruned automatically on load.
    "recordings": [],

    # --- combine ---
    "auto_combine": False,
    # When screen recording is on, what to do with screen + audio at stop:
    # "ask" (prompt every time) | "combine" (mux into one file) | "separate" (keep apart)
    "on_stop_action": "ask",

    # --- resilience ---
    "auto_restart": True,         # watchdog attempts restart on failure
    "watchdog_stale_seconds": 6,
    # --- alert channels (each independently toggleable) ---
    "alert_sound": True,          # play an attention sound
    "alert_banner": True,         # flashing gold bar at the bottom of the window
    "alert_taskbar_flash": True,  # flash the taskbar button (FlashWindowEx)
    "alert_messagebox": True,     # watchdog process pops an OS message box
    "watchdog_enabled": True,     # run the separate watcher process at all
}


class ConfigManager:
    def __init__(self):
        self._lock = threading.RLock()
        self.path = os.path.join(paths.data_dir(), "config.json")
        self.data = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self):
        with self._lock:
            if os.path.isfile(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as fh:
                        loaded = json.load(fh)
                    # Merge so new defaults appear for old config files.
                    merged = copy.deepcopy(DEFAULTS)
                    merged.update({k: v for k, v in loaded.items() if k in DEFAULTS})
                    self.data = merged
                    log.info("Config loaded from %s", self.path)
                except Exception as e:
                    log.exception("Failed to load config (%s); using defaults", e)
                    self.data = copy.deepcopy(DEFAULTS)
            else:
                log.info("No config file; using defaults. Will save to %s", self.path)
            return self.data

    def save(self):
        with self._lock:
            try:
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(self.data, fh, indent=2)
                os.replace(tmp, self.path)
                log.debug("Config saved to %s", self.path)
            except Exception as e:
                log.exception("Failed to save config: %s", e)

    def get(self, key, default=None):
        with self._lock:
            return self.data.get(key, DEFAULTS.get(key, default))

    def set(self, key, value, save=True):
        with self._lock:
            self.data[key] = value
            if save:
                self.save()

    def update(self, mapping, save=True):
        with self._lock:
            self.data.update(mapping)
            if save:
                self.save()

    def resolved_save_folder(self):
        folder = self.get("save_folder") or ""
        if folder and os.path.isdir(os.path.dirname(folder) or folder):
            try:
                os.makedirs(folder, exist_ok=True)
                return folder
            except Exception:
                log.warning("Configured save_folder unusable: %s", folder)
        return paths.default_recordings_dir()
