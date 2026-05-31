"""Audio capture engine.

Captures any number of microphones and/or system-playback devices (true WASAPI
loopback on Windows, no virtual cable needed) and writes them to disk
**continuously** with a crash-safe WAV writer, so a crash or power loss costs at
most a couple of seconds, never the whole take.

Backend: the `soundcard` library. Unlike `sounddevice`, it exposes real loopback
"microphones" for every speaker, so we can record exactly what you hear while
leaving the device free for OBS to capture at the same time (WASAPI shared mode).
`soundcard` uses a blocking pull model (`recorder.record(numframes)`), so each
device gets its own capture thread; that also means a stall on one device can
never block another.

Output modes:
  * "separate"  - one WAV per device (default, safest, no cross-device sync).
  * "channels"  - one N-channel WAV, each device down-mixed to one channel
                  (mic = ch0, playback = ch1, ...). Block-aligned; a lagging
                  device is zero-filled for that block (logged) to hold sync.
  * "mixed"     - a single summed mono mix.

Per-device gain is applied before metering and writing (like OBS faders), and
live peak levels are exposed for the UI meters, both while recording and while
idle (see LevelMonitor) so levels can be balanced before hitting record.
"""

import os
import sys
import threading
import time

import numpy as np
import soundcard as sc

from .logging_setup import get_logger
from .safewav import SafeWavWriter

log = get_logger("audio")

DEFAULT_TARGET_SR = 48000
BLOCK = 1024  # frames per combiner tick in channels/mixed mode


def _hostapi_name():
    if sys.platform == "win32":
        return "WASAPI"
    if sys.platform == "darwin":
        return "CoreAudio"
    return "PulseAudio"


# --------------------------------------------------------------------------- #
# Device enumeration
# --------------------------------------------------------------------------- #
def list_devices():
    """Return (inputs, outputs) lists of dicts describing capturable devices.

    inputs  = real microphones (kind="input").
    outputs = speaker loopbacks (kind="loopback"), i.e. "what you hear".
    """
    inputs, outputs = [], []
    host = _hostapi_name()
    try:
        mics = sc.all_microphones(include_loopback=True)
    except Exception as e:
        log.exception("soundcard enumeration failed: %s", e)
        return inputs, outputs

    for m in mics:
        try:
            ch = int(getattr(m, "channels", 2) or 2)
        except Exception:
            ch = 2
        entry = {
            "id": str(m.id),
            "name": m.name,
            "hostapi": host,
            "channels": ch,
            "samplerate": DEFAULT_TARGET_SR,
        }
        if getattr(m, "isloopback", False):
            entry["kind"] = "loopback"
            outputs.append(entry)
        else:
            entry["kind"] = "input"
            inputs.append(entry)

    log.info("Enumerated %d input device(s), %d loopback device(s)",
             len(inputs), len(outputs))
    return inputs, outputs


def default_devices():
    """Return (default_input_dict, default_output_dict) or (None, None)."""
    inputs, outputs = list_devices()
    di = do = None
    try:
        dm = sc.default_microphone()
        di = next((d for d in inputs if d["id"] == str(dm.id)), None) \
            or next((d for d in inputs if d["name"] == dm.name), None)
    except Exception as e:
        log.warning("No default microphone: %s", e)
    if di is None and inputs:
        di = inputs[0]
    try:
        ds = sc.default_speaker()
        do = next((d for d in outputs if d["id"] == str(ds.id)), None) \
            or next((d for d in outputs if ds.name in d["name"]
                     or d["name"] in ds.name), None)
    except Exception as e:
        log.warning("No default speaker: %s", e)
    if do is None and outputs:
        do = outputs[0]
    return di, do


def resolve_selection(sel):
    """Resolve a saved {name, kind, hostapi} selection to a live device dict."""
    inputs, outputs = list_devices()
    pool = inputs if sel.get("kind") == "input" else outputs
    name = sel.get("name", "")
    sid = sel.get("id", "")
    if sid:
        by_id = [d for d in pool if d["id"] == sid]
        if by_id:
            return by_id[0]
    by_name = [d for d in pool if d["name"] == name]
    return by_name[0] if by_name else None


# --------------------------------------------------------------------------- #
# Capture source
# --------------------------------------------------------------------------- #
class CaptureSource:
    """One device to record, with an adjustable gain (linear multiplier)."""

    def __init__(self, label, device_id, kind, capture_channels,
                 samplerate=DEFAULT_TARGET_SR, hostapi="", gain=1.0, name="",
                 track_name=""):
        self.label = label
        self.device_id = device_id
        self.kind = kind  # "input" | "loopback"
        self.capture_channels = max(1, int(capture_channels))
        self.samplerate = int(samplerate)
        self.hostapi = hostapi
        self.name = name
        self.gain = float(gain)
        # Short, clean, file-safe role name (e.g. "mic-1", "playback-1"). Set by
        # the caller so output files are readable rather than the raw device id.
        self.track_name = track_name

    @classmethod
    def from_device(cls, dev, gain=1.0, track_name=""):
        ch = min(2, int(dev.get("channels", 2) or 2)) or 1
        return cls(
            label=cls.make_label(dev["name"], dev["kind"]),
            device_id=dev["id"],
            kind=dev["kind"],
            capture_channels=ch,
            samplerate=dev.get("samplerate", DEFAULT_TARGET_SR),
            hostapi=dev.get("hostapi", ""),
            gain=gain,
            name=dev["name"],
            track_name=track_name,
        )

    @staticmethod
    def make_label(name, kind):
        return f"{name} [{kind}]"

    def get_microphone(self):
        """Resolve the live soundcard Microphone object for this source."""
        return sc.get_microphone(self.device_id,
                                 include_loopback=(self.kind == "loopback"))

    def safe_filename(self):
        # Prefer the clean role name; fall back to a sanitized device label.
        if self.track_name:
            base = self.track_name
        else:
            base = self.label
        keep = "".join(c if c.isalnum() or c in "-_" else "-" for c in base)
        while "--" in keep:
            keep = keep.replace("--", "-")
        return keep.strip("-_")[:48] or "track"


def _to_mono(block):
    if block.ndim == 1:
        return block.astype(np.float32, copy=False)
    if block.shape[1] == 1:
        return block[:, 0].astype(np.float32, copy=False)
    return block.mean(axis=1).astype(np.float32)


# --------------------------------------------------------------------------- #
# Live level monitor (idle pre-record meters)
# --------------------------------------------------------------------------- #
class LevelMonitor:
    """Opens the selected devices read-only to expose live peak levels.

    One daemon thread per device pulls small blocks and computes a peak. Used
    before recording so levels can be balanced; stopped when recording starts
    (the recorder then provides the levels).
    """

    def __init__(self, sources, target_sr=DEFAULT_TARGET_SR):
        self.sources = list(sources)
        self.target_sr = int(target_sr)
        self._levels = {s.label: 0.0 for s in self.sources}
        self._lock = threading.Lock()
        self._threads = []
        self.running = False

    def start(self):
        self.running = True
        for src in self.sources:
            t = threading.Thread(target=self._loop, args=(src,),
                                 name=f"meter-{src.safe_filename()}", daemon=True)
            t.start()
            self._threads.append(t)
        log.debug("LevelMonitor started for %d source(s)", len(self.sources))

    def _loop(self, src):
        frames = max(256, int(self.target_sr * 0.05))
        try:
            mic = src.get_microphone()
            with mic.recorder(samplerate=self.target_sr,
                              channels=src.capture_channels, blocksize=frames) as rec:
                while self.running:
                    data = rec.record(numframes=frames)
                    if data is None or not len(data):
                        continue
                    mono = _to_mono(np.asarray(data)) * src.gain
                    peak = float(np.max(np.abs(mono))) if mono.size else 0.0
                    with self._lock:
                        self._levels[src.label] = peak
        except Exception as e:
            log.warning("LevelMonitor [%s] stopped: %s", src.label, e)

    def get_levels(self):
        with self._lock:
            return dict(self._levels)

    def set_gain(self, label, gain):
        for s in self.sources:
            if s.label == label:
                s.gain = float(gain)

    def stop(self):
        self.running = False
        for t in self._threads:
            t.join(timeout=1.0)
        self._threads = []
        log.debug("LevelMonitor stopped")


# --------------------------------------------------------------------------- #
# Recorder
# --------------------------------------------------------------------------- #
class AudioRecorder:
    """Records the given sources. Thread-safe status for the watchdog + meters."""

    def __init__(self, sources, output_mode, out_dir, base_name,
                 target_samplerate=DEFAULT_TARGET_SR, subtype="PCM_16",
                 on_error=None, flush_seconds=2.0):
        self.sources = list(sources)
        self.output_mode = output_mode if sources else "separate"
        self.out_dir = out_dir
        self.base_name = base_name
        self.target_sr = int(target_samplerate)
        self.subtype = subtype
        self.on_error = on_error
        self.flush_seconds = float(flush_seconds)

        self.recording = False
        self._threads = []
        self.output_files = []

        self._status_lock = threading.Lock()
        self._stats = {s.label: {
            "frames": 0, "last_callback": 0.0, "last_write": 0.0,
            "xruns": 0, "active": False, "error": None, "peak": 0.0,
        } for s in self.sources}
        self._start_time = 0.0

        self._buffers = {}
        self._buf_lock = threading.Lock()

    # -- public status ----------------------------------------------------- #
    def get_status(self):
        with self._status_lock:
            snap = {k: dict(v) for k, v in self._stats.items()}
        any_active = any(v["active"] for v in snap.values()) if snap else False
        last_write = max((v["last_write"] for v in snap.values()), default=0.0)
        return {
            "recording": self.recording,
            "any_active": any_active,
            "sources": snap,
            "last_write": last_write,
            "elapsed": (time.monotonic() - self._start_time) if self._start_time else 0.0,
        }

    def get_levels(self):
        with self._status_lock:
            return {k: v["peak"] for k, v in self._stats.items()}

    def set_gain(self, label, gain):
        for s in self.sources:
            if s.label == label:
                s.gain = float(gain)

    def _set_error(self, label, reason):
        with self._status_lock:
            if label in self._stats:
                self._stats[label]["error"] = reason
                self._stats[label]["active"] = False
        log.error("Audio source error [%s]: %s", label, reason)
        if self.on_error:
            try:
                self.on_error(label, reason)
            except Exception:
                log.exception("on_error callback raised")

    def _apply_gain(self, data, gain):
        if gain == 1.0:
            return data
        return np.clip(data * gain, -1.0, 1.0)

    def _record_peak(self, label, mono):
        peak = float(np.max(np.abs(mono))) if mono.size else 0.0
        with self._status_lock:
            self._stats[label]["peak"] = peak

    def _note_progress(self, label, frames):
        now = time.monotonic()
        with self._status_lock:
            st = self._stats[label]
            st["frames"] += frames
            st["last_callback"] = now
            st["active"] = True

    # -- start/stop -------------------------------------------------------- #
    def start(self):
        if not self.sources:
            raise ValueError("No audio sources selected.")
        os.makedirs(self.out_dir, exist_ok=True)
        self.recording = True
        self._start_time = time.monotonic()
        log.info("Starting audio recording: mode=%s, %d source(s), target_sr=%d, subtype=%s",
                 self.output_mode, len(self.sources), self.target_sr, self.subtype)
        if self.output_mode == "separate":
            self._start_separate()
        else:
            self._start_combined()
        # Give capture threads a moment to open their streams so an immediate
        # failure surfaces (and the watchdog sees activity) rather than racing.
        time.sleep(0.2)

    # ---- separate-files mode -------------------------------------------- #
    def _start_separate(self):
        for src in self.sources:
            path = os.path.join(
                self.out_dir, f"{self.base_name}_{src.safe_filename()}.wav")
            self.output_files.append(path)
            t = threading.Thread(target=self._capture_separate, args=(src, path),
                                 name=f"cap-{src.safe_filename()}", daemon=True)
            t.start()
            self._threads.append(t)

    def _capture_separate(self, src, path):
        frames = max(256, int(self.target_sr * 0.05))
        writer = None
        try:
            mic = src.get_microphone()
            with mic.recorder(samplerate=self.target_sr,
                              channels=src.capture_channels, blocksize=frames) as rec:
                writer = SafeWavWriter(path, self.target_sr, src.capture_channels,
                                       subtype=self.subtype,
                                       flush_interval=self.flush_seconds)
                log.info("Writing %s (%d ch @ %d Hz, %s, crash-safe)", path,
                         src.capture_channels, self.target_sr, self.subtype)
                with self._status_lock:
                    self._stats[src.label]["active"] = True
                while self.recording:
                    data = np.asarray(rec.record(numframes=frames))
                    if not len(data):
                        continue
                    data = self._apply_gain(data, src.gain)
                    self._record_peak(src.label, _to_mono(data))
                    writer.write(data)
                    self._note_progress(src.label, len(data))
                    with self._status_lock:
                        self._stats[src.label]["last_write"] = time.monotonic()
        except Exception as e:
            self._set_error(src.label, f"capture/writer failed: {e}")
        finally:
            if writer is not None:
                writer.close()
            with self._status_lock:
                if src.label in self._stats:
                    self._stats[src.label]["active"] = False

    # ---- channels / mixed mode ------------------------------------------ #
    def _start_combined(self):
        with self._buf_lock:
            for src in self.sources:
                self._buffers[src.label] = np.zeros(0, dtype=np.float32)
        for src in self.sources:
            t = threading.Thread(target=self._capture_buffer, args=(src,),
                                 name=f"cap-{src.safe_filename()}", daemon=True)
            t.start()
            self._threads.append(t)

        n_ch = len(self.sources) if self.output_mode == "channels" else 1
        suffix = "channels" if self.output_mode == "channels" else "mix"
        path = os.path.join(self.out_dir, f"{self.base_name}_{suffix}.wav")
        self.output_files.append(path)
        t = threading.Thread(target=self._combiner, args=(path, n_ch),
                             name="combiner", daemon=True)
        t.start()
        self._threads.append(t)

    def _capture_buffer(self, src):
        frames = max(256, int(self.target_sr * 0.05))
        try:
            mic = src.get_microphone()
            with mic.recorder(samplerate=self.target_sr,
                              channels=src.capture_channels, blocksize=frames) as rec:
                with self._status_lock:
                    self._stats[src.label]["active"] = True
                while self.recording:
                    data = np.asarray(rec.record(numframes=frames))
                    if not len(data):
                        continue
                    mono = _to_mono(data) * src.gain
                    if src.gain != 1.0:
                        mono = np.clip(mono, -1.0, 1.0)
                    self._record_peak(src.label, mono)
                    with self._buf_lock:
                        self._buffers[src.label] = np.concatenate(
                            (self._buffers[src.label], mono))
                    self._note_progress(src.label, len(data))
        except Exception as e:
            self._set_error(src.label, f"capture failed: {e}")
        finally:
            with self._status_lock:
                if src.label in self._stats:
                    self._stats[src.label]["active"] = False

    def _combiner(self, path, n_ch):
        labels = [s.label for s in self.sources]
        max_wait = 0.25
        f = None
        try:
            f = SafeWavWriter(path, self.target_sr, n_ch, subtype=self.subtype,
                              flush_interval=self.flush_seconds)
            log.info("Writing combined %s (%d ch @ %d Hz, %s, crash-safe)", path,
                     n_ch, self.target_sr, self.subtype)
            last_progress = time.monotonic()
            while True:
                with self._buf_lock:
                    avail = {lb: len(self._buffers[lb]) for lb in labels}
                have_all = all(v >= BLOCK for v in avail.values())
                waited = time.monotonic() - last_progress
                if not have_all and self.recording and waited < max_wait:
                    time.sleep(0.005)
                    continue
                if not have_all and not self.recording and max(avail.values(), default=0) == 0:
                    break

                cols = []
                for lb in labels:
                    with self._buf_lock:
                        buf = self._buffers[lb]
                        take = min(BLOCK, len(buf))
                        chunk = buf[:take]
                        self._buffers[lb] = buf[take:]
                    if take < BLOCK:
                        if self.recording and waited >= max_wait:
                            log.warning("Zero-filling %d frame(s) for [%s]",
                                        BLOCK - take, lb)
                        chunk = np.concatenate(
                            (chunk, np.zeros(BLOCK - take, dtype=np.float32)))
                    cols.append(chunk)
                last_progress = time.monotonic()

                if self.output_mode == "channels":
                    out = np.stack(cols, axis=1)
                else:
                    out = np.clip(np.sum(cols, axis=0), -1.0, 1.0).reshape(-1, 1)
                f.write(out)
                with self._status_lock:
                    now = time.monotonic()
                    for lb in labels:
                        self._stats[lb]["last_write"] = now

                if not self.recording:
                    with self._buf_lock:
                        remaining = max((len(self._buffers[lb]) for lb in labels),
                                        default=0)
                    if remaining == 0:
                        break
        except Exception as e:
            self._set_error(labels[0] if labels else "combiner", f"combiner failed: {e}")
        finally:
            if f is not None:
                f.close()

    def stop(self):
        if not self.recording:
            return self.output_files
        log.info("Stopping audio recording...")
        self.recording = False
        for t in self._threads:
            t.join(timeout=6.0)
        log.info("Audio recording stopped. Files: %s", self.output_files)
        self._start_time = 0.0
        return self.output_files
