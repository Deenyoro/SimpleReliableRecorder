"""Crash safe WAV writer.

A normal WAV (the RIFF/data size fields) is only finalized when the file is
closed cleanly. If the process is killed mid recording, those size fields stay
zero and most players treat the file as empty even though the PCM samples are
sitting on disk.

SafeWavWriter avoids that: it rewrites the size fields and fsyncs to disk every
few seconds, so at any moment the file on disk is a valid, playable WAV holding
everything written up to the last flush. This is the audio equivalent of OBS
fragmented recording: a power loss or crash costs you at most the last couple of
seconds, never the whole take.

Supports PCM_16 (format code 1) and 32 bit float (format code 3).
"""

import os
import struct
import threading
import time

import numpy as np

from .logging_setup import get_logger

log = get_logger("audio")


class SafeWavWriter:
    def __init__(self, path, samplerate, channels, subtype="PCM_16",
                 flush_interval=2.0):
        self.path = path
        self.sr = int(samplerate)
        self.ch = int(channels)
        self.subtype = subtype
        self.flush_interval = flush_interval
        self.bits = 16 if subtype == "PCM_16" else 32
        self.fmt_code = 1 if subtype == "PCM_16" else 3  # PCM or IEEE float
        self._data_bytes = 0
        self._lock = threading.Lock()
        self._last_flush = time.monotonic()
        self._f = open(path, "wb")
        self._write_header()
        self._f.flush()

    def _write_header(self):
        byte_rate = self.sr * self.ch * self.bits // 8
        block_align = self.ch * self.bits // 8
        self._f.seek(0)
        self._f.write(b"RIFF")
        self._f.write(struct.pack("<I", 36 + self._data_bytes))
        self._f.write(b"WAVE")
        self._f.write(b"fmt ")
        self._f.write(struct.pack("<I", 16))
        self._f.write(struct.pack("<HHIIHH", self.fmt_code, self.ch, self.sr,
                                  byte_rate, block_align, self.bits))
        self._f.write(b"data")
        self._f.write(struct.pack("<I", self._data_bytes))

    def write(self, float_block):
        """float_block: numpy array (frames, channels) or (frames,) float32."""
        if float_block.ndim == 1:
            float_block = float_block.reshape(-1, 1)
        if self.subtype == "PCM_16":
            clipped = np.clip(float_block, -1.0, 1.0)
            raw = (clipped * 32767.0).astype("<i2").tobytes()
        else:
            raw = float_block.astype("<f4").tobytes()
        with self._lock:
            self._f.seek(0, os.SEEK_END)
            self._f.write(raw)
            self._data_bytes += len(raw)
            now = time.monotonic()
            if now - self._last_flush >= self.flush_interval:
                self._refresh_locked()
                self._last_flush = now

    def _refresh_locked(self):
        # Update the size headers and force everything to physical disk.
        self._write_header()
        self._f.seek(0, os.SEEK_END)
        self._f.flush()
        try:
            os.fsync(self._f.fileno())
        except Exception:
            pass

    def flush(self):
        with self._lock:
            self._refresh_locked()

    def close(self):
        with self._lock:
            try:
                self._refresh_locked()
            finally:
                try:
                    self._f.close()
                except Exception:
                    pass
        log.debug("Closed safe WAV %s (%d data bytes)", self.path, self._data_bytes)
