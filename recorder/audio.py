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
                  (mic = ch0, playback = ch1, ...). The combiner paces the file
                  at wall-clock rate; a device that falls behind by more than a
                  small jitter window is zero-filled (logged), and late frames
                  are dropped against that padding so channels stay in sync.
  * "mixed"     - a single summed mono mix.

Per-device gain is applied before metering and writing (like OBS faders), and
live peak levels are exposed for the UI meters, both while recording and while
idle (see LevelMonitor) so levels can be balanced before hitting record.
"""

import os
import sys
import threading
import time
from collections import deque

import numpy as np
import soundcard as sc

from .logging_setup import get_logger
from .safewav import SafeWavWriter

log = get_logger("audio")

DEFAULT_TARGET_SR = 48000
BLOCK = 1024  # frames per combiner tick in channels/mixed mode

# Combined-mode pacing: how far a source may lag behind wall clock before the
# combiner zero-fills it, how much audio a source buffer may hold before the
# oldest frames are dropped, and the largest single gap-fill per retry cycle.
COMBINE_JITTER_S = 0.25
COMBINE_BUFFER_CAP_S = 30.0
GAP_FILL_MAX_S = 30.0
# Separate-mode disk-retry queue: how much captured audio we hold in RAM while
# the disk is refusing writes, and how often a disk failure is re-reported.
DISK_RETRY_CAP_S = 60.0
DISK_ERROR_REPORT_S = 10.0


def _hostapi_name():
    if sys.platform == "win32":
        return "WASAPI"
    if sys.platform == "darwin":
        return "CoreAudio"
    return "PulseAudio"


# --------------------------------------------------------------------------- #
# Device enumeration
# --------------------------------------------------------------------------- #
_no_loopback_logged = False


def _warn_no_loopback_once():
    global _no_loopback_logged
    if not _no_loopback_logged:
        _no_loopback_logged = True
        log.info("System-playback (loopback) capture is unavailable on this "
                 "platform; only microphones can be recorded.")


def list_devices():
    """Return (inputs, outputs) lists of dicts describing capturable devices.

    inputs  = real microphones (kind="input").
    outputs = speaker loopbacks (kind="loopback"), i.e. "what you hear".
    """
    inputs, outputs = [], []
    host = _hostapi_name()
    mics = None
    if sys.platform != "darwin":
        try:
            mics = sc.all_microphones(include_loopback=True)
        except Exception as e:
            log.warning("Loopback-aware enumeration failed (%s); "
                        "retrying microphones only", e)
    if mics is None:
        # macOS CoreAudio has no loopback devices; never let that stop plain
        # microphone enumeration.
        try:
            mics = sc.all_microphones()
        except Exception as e:
            log.exception("soundcard enumeration failed: %s", e)
            return inputs, outputs
        _warn_no_loopback_once()

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
                 track_name="", muted=False):
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
        # When muted, the capture loop writes silence for this device so the
        # track stays continuous and in sync; the file keeps growing.
        self.muted = bool(muted)

    @classmethod
    def from_device(cls, dev, gain=1.0, track_name="", muted=False):
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
            muted=muted,
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
                    if src.muted:
                        peak = 0.0
                    else:
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

    def set_muted(self, label, muted):
        for s in self.sources:
            if s.label == label:
                s.muted = bool(muted)

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
        self._writers = []          # live SafeWavWriter refs (for segment paths)
        self._writers_lock = threading.Lock()
        # Set when the combined writer dies (disk full etc.) so capture threads
        # stop buffering audio that can never be written.
        self._write_fatal = False

        # Internal state is keyed by a unique per-source key, not the display
        # label: two identical USB mics share a label but must never share a
        # buffer or stats slot. The label stays attached for display.
        self._source_keys = {}
        seen = {}
        for s in self.sources:
            base = f"{s.device_id}|{s.kind}"
            n = seen.get(base, 0) + 1
            seen[base] = n
            self._source_keys[id(s)] = base if n == 1 else f"{base}#{n}"

        self._status_lock = threading.Lock()
        self._stats = {self._source_keys[id(s)]: {
            "label": s.label,
            "frames": 0, "last_callback": 0.0, "last_write": 0.0,
            "xruns": 0, "active": False, "error": None, "peak": 0.0,
        } for s in self.sources}
        self._start_time = 0.0

        self._buffers = {}
        self._buf_lock = threading.Lock()

    def _skey(self, src):
        """Unique internal key for a source (labels may collide)."""
        return self._source_keys[id(src)]

    # -- public status ----------------------------------------------------- #
    def get_status(self):
        with self._status_lock:
            snap = {k: dict(v) for k, v in self._stats.items()}
        any_active = any(v["active"] for v in snap.values()) if snap else False
        # In combined mode per-source last_write is only stamped when real
        # device frames reach the file, so this max stays honest: it does NOT
        # advance on combiner zero-fill. In separate mode it is the max across
        # sources as before (a single dead source surfaces via on_error).
        last_write = max((v["last_write"] for v in snap.values()), default=0.0)
        # Keep the label-keyed "sources" view for the UI; duplicate labels are
        # merged conservatively (worst error wins, activity/progress combined).
        sources = {}
        for st in snap.values():
            lb = st["label"]
            cur = sources.get(lb)
            if cur is None:
                sources[lb] = dict(st)
            else:
                cur["frames"] += st["frames"]
                cur["xruns"] += st["xruns"]
                cur["last_callback"] = max(cur["last_callback"], st["last_callback"])
                cur["last_write"] = max(cur["last_write"], st["last_write"])
                cur["active"] = cur["active"] or st["active"]
                cur["peak"] = max(cur["peak"], st["peak"])
                cur["error"] = cur["error"] or st["error"]
        per_source = {k: {"label": v["label"], "last_write": v["last_write"],
                          "active": v["active"]} for k, v in snap.items()}
        return {
            "recording": self.recording,
            "any_active": any_active,
            "sources": sources,
            "per_source": per_source,
            "last_write": last_write,
            "elapsed": (time.monotonic() - self._start_time) if self._start_time else 0.0,
        }

    def get_levels(self):
        levels = {}
        with self._status_lock:
            for v in self._stats.values():
                lb = v["label"]
                levels[lb] = max(levels.get(lb, 0.0), v["peak"])
        return levels

    def set_gain(self, label, gain):
        for s in self.sources:
            if s.label == label:
                s.gain = float(gain)

    def set_muted(self, label, muted):
        """Mute/unmute a device live. Muted devices write silence to keep the
        track continuous and in sync."""
        for s in self.sources:
            if s.label == label:
                s.muted = bool(muted)
                log.info("Device %s %s", label, "muted" if muted else "unmuted")

    def _set_error(self, key, reason):
        """Record an error for an internal source key (or a neutral label like
        "combined-writer") and surface it via on_error with the display label."""
        display = key
        with self._status_lock:
            if key in self._stats:
                self._stats[key]["error"] = reason
                self._stats[key]["active"] = False
                display = self._stats[key]["label"]
        log.error("Audio source error [%s]: %s", display, reason)
        if self.on_error:
            try:
                self.on_error(display, reason)
            except Exception:
                log.exception("on_error callback raised")

    def _clear_error(self, key):
        with self._status_lock:
            if key in self._stats:
                self._stats[key]["error"] = None

    def _apply_gain(self, data, gain):
        if gain == 1.0:
            return data
        return np.clip(data * gain, -1.0, 1.0)

    def _record_peak(self, key, mono):
        peak = float(np.max(np.abs(mono))) if mono.size else 0.0
        with self._status_lock:
            self._stats[key]["peak"] = peak

    def _note_progress(self, key, frames):
        now = time.monotonic()
        with self._status_lock:
            st = self._stats[key]
            st["frames"] += frames
            st["last_callback"] = now
            st["active"] = True

    # -- start/stop -------------------------------------------------------- #
    def start(self):
        if not self.sources:
            raise ValueError("No audio sources selected.")
        os.makedirs(self.out_dir, exist_ok=True)
        self.recording = True
        self._write_fatal = False
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
        """Capture one device to its own WAV, surviving transient device errors.

        The writer is opened once and kept alive for the whole take. If the audio
        stream throws (device unplugged/invalidated/format change), we keep the
        file open, fill the gap with exactly the silence the wall clock says is
        missing, and retry opening the device until it returns or recording
        stops. Disk trouble is handled separately from device trouble: a failed
        write never reopens the device; instead the captured audio is held in a
        bounded RAM queue and retried every block until the disk recovers.
        """
        key = self._skey(src)
        frames = max(256, int(self.target_sr * 0.05))
        writer = None
        pending = deque()           # captured blocks awaiting a successful write
        pending_frames = 0
        max_pending = int(self.target_sr * DISK_RETRY_CAP_S)
        frames_on_disk = 0          # frames actually written (audio + gap silence)
        disk_down_since = 0.0
        last_disk_report = 0.0
        last_drop_warn = 0.0
        try:
            writer = SafeWavWriter(
                path, self.target_sr, src.capture_channels, subtype=self.subtype,
                flush_interval=self.flush_seconds)
            with self._writers_lock:
                self._writers.append(writer)
            log.info("Writing %s (%d ch @ %d Hz, %s, crash-safe)", path,
                     src.capture_channels, self.target_sr, self.subtype)
            t0 = time.monotonic()
            attempt = 0
            while self.recording:
                try:
                    mic = src.get_microphone()
                    with mic.recorder(samplerate=self.target_sr,
                                      channels=src.capture_channels,
                                      blocksize=frames) as rec:
                        if attempt > 0:
                            log.info("Device reconnected [%s]", src.label)
                            self._clear_error(key)
                        attempt = 0
                        with self._status_lock:
                            self._stats[key]["active"] = True
                        while self.recording:
                            data = np.asarray(rec.record(numframes=frames))
                            if not len(data):
                                continue
                            if src.muted:
                                data = np.zeros_like(data)
                            else:
                                data = self._apply_gain(data, src.gain)
                            self._record_peak(key, _to_mono(data))
                            self._note_progress(key, len(data))
                            pending.append(data)
                            pending_frames += len(data)
                            # Bound the retry queue so a long disk outage can't
                            # eat RAM; losing the oldest is better than crashing.
                            while pending_frames > max_pending:
                                old = pending.popleft()
                                pending_frames -= len(old)
                                now = time.monotonic()
                                if now - last_drop_warn >= 60.0:
                                    last_drop_warn = now
                                    log.warning(
                                        "Disk retry queue full [%s]: dropping "
                                        "oldest captured audio", src.label)
                            # Flush the queue in order; on disk trouble keep
                            # capturing and retry next block - do NOT reopen
                            # the device for a write failure.
                            try:
                                while pending:
                                    writer.write(pending[0])
                                    blk = pending.popleft()
                                    pending_frames -= len(blk)
                                    frames_on_disk += len(blk)
                                    with self._status_lock:
                                        self._stats[key]["last_write"] = time.monotonic()
                                if disk_down_since:
                                    log.info("Disk writes recovered [%s] after %.1fs",
                                             src.label,
                                             time.monotonic() - disk_down_since)
                                    disk_down_since = 0.0
                                    self._clear_error(key)
                            except Exception as e:
                                now = time.monotonic()
                                if not disk_down_since:
                                    disk_down_since = now
                                if now - last_disk_report >= DISK_ERROR_REPORT_S:
                                    last_disk_report = now
                                    self._set_error(key, f"disk write failed: {e}")
                except Exception as e:
                    if not self.recording:
                        break
                    attempt += 1
                    with self._status_lock:
                        self._stats[key]["active"] = False
                        self._stats[key]["xruns"] += 1
                    self._set_error(key, f"device error (reconnecting): {e}")
                    log.warning("Capture stream lost [%s] attempt %d: %s",
                                src.label, attempt, e)
                    # Keep the timeline aligned: fill exactly what the wall
                    # clock says is missing (driver timeouts often eat far more
                    # than one retry period), capped per iteration.
                    expected = int((time.monotonic() - t0) * self.target_sr)
                    deficit = expected - (frames_on_disk + pending_frames)
                    if deficit > 0:
                        fill = min(deficit, int(self.target_sr * GAP_FILL_MAX_S))
                        frames_on_disk += self._write_silence(writer, src, fill)
                    time.sleep(0.5)
        except Exception as e:
            self._set_error(key, f"writer failed: {e}")
        finally:
            # Last chance: push any still-queued audio to disk before closing.
            try:
                while pending:
                    writer.write(pending[0])
                    pending.popleft()
            except Exception as e:
                log.warning("Could not flush %d queued block(s) [%s] at close: %s",
                            len(pending), src.label, e)
            if writer is not None:
                writer.close()
            with self._status_lock:
                if key in self._stats:
                    self._stats[key]["active"] = False

    def _write_silence(self, writer, src, n):
        """Write n frames of silence. Returns the frames written (0 on failure);
        failures are logged, never swallowed silently."""
        try:
            if n > 0:
                writer.write(np.zeros((n, src.capture_channels), dtype=np.float32))
                return n
        except Exception as e:
            log.warning("Gap-fill silence write failed [%s]: %s", src.label, e)
        return 0

    # ---- channels / mixed mode ------------------------------------------ #
    def _start_combined(self):
        with self._buf_lock:
            for src in self.sources:
                self._buffers[self._skey(src)] = {
                    "label": src.label,
                    "chunks": deque(),  # (float32 mono array, is_real) pairs
                    "len": 0,           # total frames queued
                    "debt": 0,          # frames to drop: zero-fill already wrote them
                    "warn_ts": 0.0,     # last overflow warning
                }
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

    def _buf_append(self, key, mono, real):
        """Queue frames for the combiner. real=True for frames pulled from the
        device (muted blocks count: the device is alive), False for gap silence.
        The buffer is capped; on overflow the oldest frames are dropped."""
        cap = int(self.target_sr * COMBINE_BUFFER_CAP_S)
        with self._buf_lock:
            b = self._buffers[key]
            b["chunks"].append((mono, real))
            b["len"] += len(mono)
            over = b["len"] - cap
            if over > 0:
                dropped = 0
                while dropped < over and b["chunks"]:
                    arr, r = b["chunks"][0]
                    need = over - dropped
                    if len(arr) <= need:
                        b["chunks"].popleft()
                        dropped += len(arr)
                    else:
                        b["chunks"][0] = (arr[need:], r)
                        dropped += need
                b["len"] -= dropped
                now = time.monotonic()
                if now - b["warn_ts"] >= 60.0:
                    b["warn_ts"] = now
                    log.warning("Combine buffer full [%s]: dropped %d frame(s) "
                                "of oldest audio", b["label"], dropped)

    def _buf_take(self, key, n):
        """Pull up to n frames for the combiner. Burns pad debt first (frames
        already covered by zero-fill are dropped so the timeline stays aligned).
        Returns (float32 array, frames_taken, real_frames_taken)."""
        with self._buf_lock:
            b = self._buffers[key]
            while b["debt"] > 0 and b["chunks"]:
                arr, r = b["chunks"][0]
                burn = min(b["debt"], len(arr))
                if burn == len(arr):
                    b["chunks"].popleft()
                else:
                    b["chunks"][0] = (arr[burn:], r)
                b["debt"] -= burn
                b["len"] -= burn
            parts, taken, real = [], 0, 0
            while taken < n and b["chunks"]:
                arr, r = b["chunks"][0]
                grab = min(n - taken, len(arr))
                if grab == len(arr):
                    b["chunks"].popleft()
                    parts.append(arr)
                else:
                    parts.append(arr[:grab])
                    b["chunks"][0] = (arr[grab:], r)
                taken += grab
                b["len"] -= grab
                if r:
                    real += grab
        out = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
        return out, taken, real

    def _buf_available(self):
        """Frames each source can supply right now, net of pad debt."""
        with self._buf_lock:
            return {k: max(0, b["len"] - b["debt"])
                    for k, b in self._buffers.items()}

    def _capture_buffer(self, src):
        key = self._skey(src)
        frames = max(256, int(self.target_sr * 0.05))
        attempt = 0
        t0 = time.monotonic()
        appended = 0  # frames pushed into the combine buffer (real + silence)
        try:
            while self.recording and not self._write_fatal:
                try:
                    mic = src.get_microphone()
                    with mic.recorder(samplerate=self.target_sr,
                                      channels=src.capture_channels,
                                      blocksize=frames) as rec:
                        if attempt > 0:
                            log.info("Device reconnected [%s]", src.label)
                            self._clear_error(key)
                        attempt = 0
                        with self._status_lock:
                            self._stats[key]["active"] = True
                        while self.recording and not self._write_fatal:
                            data = np.asarray(rec.record(numframes=frames))
                            if not len(data):
                                continue
                            if src.muted:
                                mono = np.zeros(len(data), dtype=np.float32)
                            else:
                                mono = _to_mono(data) * src.gain
                                if src.gain != 1.0:
                                    mono = np.clip(mono, -1.0, 1.0)
                            self._record_peak(key, mono)
                            self._buf_append(key, mono, real=True)
                            appended += len(mono)
                            self._note_progress(key, len(data))
                except Exception as e:
                    if not self.recording or self._write_fatal:
                        break
                    attempt += 1
                    with self._status_lock:
                        self._stats[key]["active"] = False
                        self._stats[key]["xruns"] += 1
                    self._set_error(key, f"device error (reconnecting): {e}")
                    log.warning("Capture stream lost [%s] attempt %d: %s",
                                src.label, attempt, e)
                    # Push exactly the missing silence into the buffer (wall
                    # clock, not a fixed 0.5s) so the channel stays aligned; any
                    # zero-fill the combiner already wrote cancels against this
                    # via the pad debt.
                    expected = int((time.monotonic() - t0) * self.target_sr)
                    deficit = expected - appended
                    if deficit > 0:
                        fill = min(deficit, int(self.target_sr * GAP_FILL_MAX_S))
                        self._buf_append(key, np.zeros(fill, dtype=np.float32),
                                         real=False)
                        appended += fill
                    time.sleep(0.5)
        finally:
            with self._status_lock:
                if key in self._stats:
                    self._stats[key]["active"] = False

    def _combiner(self, path, n_ch):
        """Merge the per-source buffers into one file at wall-clock rate.

        Pacing rule: the output must advance at real time no matter what any
        single device does. While all sources have a full block we write real
        audio immediately. When one lags, we wait only until the file itself is
        a jitter window behind the wall clock, then zero-fill the laggard and
        charge the padding to its pad debt so any late frames are dropped
        rather than smeared onto the wrong part of the timeline.
        """
        keys = [self._skey(s) for s in self.sources]
        jitter = int(self.target_sr * COMBINE_JITTER_S)
        zero_warn = {k: 0.0 for k in keys}  # rate-limit pad warnings per source
        f = None
        try:
            f = SafeWavWriter(path, self.target_sr, n_ch, subtype=self.subtype,
                              flush_interval=self.flush_seconds)
            with self._writers_lock:
                self._writers.append(f)
            log.info("Writing combined %s (%d ch @ %d Hz, %s, crash-safe)", path,
                     n_ch, self.target_sr, self.subtype)
            t0 = time.monotonic()
            written = 0  # combined frames written so far
            while True:
                avail = self._buf_available()
                if not self.recording:
                    # Final drain: flush whatever is still buffered (the last
                    # in-flight block) - but never run the file past the wall
                    # clock; a late backlog's timeline slot was already padded.
                    remaining = max(avail.values(), default=0)
                    if remaining == 0:
                        break
                    if written >= int((time.monotonic() - t0) * self.target_sr):
                        log.warning("Discarding %d late backlog frame(s) at "
                                    "stop (past wall-clock end)", remaining)
                        break
                have_all = all(avail[k] >= BLOCK for k in keys)
                if not have_all and self.recording:
                    behind = int((time.monotonic() - t0) * self.target_sr) - written
                    if behind <= jitter:
                        time.sleep(0.005)
                        continue

                cols = []
                real_keys = []
                for k in keys:
                    chunk, taken, real = self._buf_take(k, BLOCK)
                    if taken < BLOCK:
                        pad = BLOCK - taken
                        if self.recording:
                            # Remember the padding so late frames get dropped
                            # instead of arriving out of place.
                            with self._buf_lock:
                                self._buffers[k]["debt"] += pad
                            now = time.monotonic()
                            if now - zero_warn[k] >= 5.0:
                                zero_warn[k] = now
                                log.warning("Zero-filling [%s]: device behind "
                                            "wall clock", self._buffers[k]["label"])
                        chunk = np.concatenate(
                            (chunk, np.zeros(pad, dtype=np.float32)))
                    if real > 0:
                        real_keys.append(k)
                    cols.append(chunk)

                if self.output_mode == "channels":
                    out = np.stack(cols, axis=1)
                else:
                    out = np.clip(np.sum(cols, axis=0), -1.0, 1.0).reshape(-1, 1)
                f.write(out)
                written += BLOCK
                # Stamp last_write only for sources whose real device frames
                # made it into the file - zero-fill must not look like health.
                if real_keys:
                    now = time.monotonic()
                    with self._status_lock:
                        for k in real_keys:
                            if k in self._stats:
                                self._stats[k]["last_write"] = now
        except Exception as e:
            # The combined file is the only output in this mode: tell the
            # capture threads to stop buffering audio that can never be saved.
            self._write_fatal = True
            self._set_error("combined-writer", f"combiner failed: {e}")
        finally:
            if f is not None:
                f.close()

    def all_output_files(self):
        """Every file actually written, including rollover segments (_part2 ...)."""
        files = []
        with self._writers_lock:
            for wf in self._writers:
                for p in getattr(wf, "paths", []):
                    if p not in files:
                        files.append(p)
        # Fall back to the planned base paths if no writer registered yet.
        for p in self.output_files:
            if p not in files:
                files.append(p)
        return files

    def stop(self):
        if not self.recording:
            return self.all_output_files()
        log.info("Stopping audio recording...")
        self.recording = False
        # One shared deadline so stop() can't block 6s per thread.
        deadline = time.monotonic() + 6.0
        for t in self._threads:
            t.join(timeout=max(0.1, deadline - time.monotonic()))
        stuck = [t.name for t in self._threads if t.is_alive()]
        if stuck:
            # A wedged thread would leave its WAV with a stale header and the
            # tail unflushed - force-finalize every writer (their locks make
            # post-close writes drop safely). Done on a helper thread so a
            # writer lock held by the wedged thread can't hang stop() forever.
            log.error("Capture thread(s) did not exit: %s - forcing writer "
                      "flush/close so no audio is silently lost", stuck)
            with self._writers_lock:
                writers = list(self._writers)

            def _force_close():
                for w in writers:
                    try:
                        w.flush()
                        w.close()
                    except Exception:
                        log.exception("Forced close failed for %s",
                                      getattr(w, "base_path", "?"))

            closer = threading.Thread(target=_force_close,
                                      name="writer-force-close", daemon=True)
            closer.start()
            closer.join(timeout=5.0)
            if closer.is_alive():
                log.error("Forced writer close is itself hung; files may have "
                          "stale headers (still recoverable up to last flush)")
        files = self.all_output_files()
        log.info("Audio recording stopped. Files: %s", files)
        self._start_time = 0.0
        return files
