"""Crash safe WAV writer with automatic 4 GB segment rollover.

A normal WAV (the RIFF/data size fields) is only finalized when the file is
closed cleanly. If the process is killed mid recording, those size fields stay
zero and most players treat the file as empty even though the PCM samples are
sitting on disk.

SafeWavWriter avoids that on two fronts:

1. Crash safety: it rewrites the size fields and fsyncs to disk every few
   seconds, so at any moment the file on disk is a valid, playable WAV holding
   everything written up to the last flush. A power loss or crash costs you at
   most the last couple of seconds, never the whole take.

2. The 4 GB wall: the WAV/RIFF container stores its size in a 32 bit field, so a
   single file cannot exceed 4 GiB (~6.2 h of 48 kHz 16 bit stereo). Past that
   the header would overflow and the file would be corrupt. SafeWavWriter
   transparently rolls over to a new numbered segment (``name.wav`` ->
   ``name_part2.wav`` -> ...) just before the limit, so an unattended multi hour
   recording is never lost or corrupted. Every segment is itself a complete,
   playable, crash safe WAV.

Supports PCM_16 (format code 1) and 32 bit float (format code 3).
"""

import os
import struct
import threading
import time

import numpy as np

from .logging_setup import get_logger

log = get_logger("audio")

# RIFF/data sizes are unsigned 32 bit. Keep a safety margin under 4 GiB and
# round down to the block size so a frame is never split across a boundary.
_MAX_DATA_BYTES = 0xFFFFFFFF - (1 << 20)  # ~4 GiB minus 1 MiB headroom


class SafeWavWriter:
    def __init__(self, path, samplerate, channels, subtype="PCM_16",
                 flush_interval=2.0, on_error=None):
        if subtype not in ("PCM_16", "FLOAT"):
            raise ValueError(
                f"Unsupported WAV subtype {subtype!r} (expected 'PCM_16' or 'FLOAT')")
        self.base_path = path
        self.sr = int(samplerate)
        self.ch = max(1, int(channels))
        self.subtype = subtype
        self.flush_interval = flush_interval
        self.bits = 16 if subtype == "PCM_16" else 32
        self.fmt_code = 1 if subtype == "PCM_16" else 3  # PCM or IEEE float
        self.on_error = on_error
        self._fsync_warned = False

        self._block_align = self.ch * self.bits // 8
        # Largest data payload we allow per segment (block aligned).
        self._roll_at = _MAX_DATA_BYTES - (_MAX_DATA_BYTES % self._block_align)

        self._lock = threading.Lock()
        self._segment = 1
        self.paths = []            # every segment path written, in order
        self._data_bytes = 0
        self._last_flush = time.monotonic()
        self._f = None
        self._closed = False
        self._open_segment(path)

    # -- segment lifecycle ------------------------------------------------- #
    def _segment_path(self, n):
        if n <= 1:
            return self.base_path
        root, ext = os.path.splitext(self.base_path)
        return f"{root}_part{n}{ext}"

    def _open_segment(self, path):
        self._f = open(path, "wb")
        self._data_bytes = 0
        self.paths.append(path)
        self._write_header()
        self._f.flush()
        if self._segment > 1:
            log.info("WAV rollover: started segment %d -> %s", self._segment, path)

    def _roll_over(self):
        # Finalize the current segment, then open the next one.
        self._refresh_locked()
        try:
            self._f.close()
        except Exception:
            pass
        self._segment += 1
        self._open_segment(self._segment_path(self._segment))

    # -- header ------------------------------------------------------------ #
    def _write_header(self):
        byte_rate = self.sr * self._block_align
        # Clamp to the 32 bit field maximum so struct.pack can never overflow,
        # even in a pathological case; rollover keeps us well under this anyway.
        riff_size = min(36 + self._data_bytes, 0xFFFFFFFF)
        data_size = min(self._data_bytes, 0xFFFFFFFF)
        self._f.seek(0)
        self._f.write(b"RIFF")
        self._f.write(struct.pack("<I", riff_size))
        self._f.write(b"WAVE")
        self._f.write(b"fmt ")
        self._f.write(struct.pack("<I", 16))
        self._f.write(struct.pack("<HHIIHH", self.fmt_code, self.ch, self.sr,
                                  byte_rate, self._block_align, self.bits))
        self._f.write(b"data")
        self._f.write(struct.pack("<I", data_size))

    # -- writing ----------------------------------------------------------- #
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
            if self._closed:
                return
            # Roll to a new segment before crossing the 4 GiB boundary.
            if self._data_bytes + len(raw) > self._roll_at:
                self._roll_over()
            try:
                self._f.seek(0, os.SEEK_END)
                self._f.write(raw)
                self._data_bytes += len(raw)
                now = time.monotonic()
                if now - self._last_flush >= self.flush_interval:
                    self._refresh_locked()
                    self._last_flush = now
            except Exception as e:
                # Disk full / IO error: surface loudly rather than dying silent.
                log.exception("WAV write failed for %s: %s", self.base_path, e)
                if self.on_error:
                    try:
                        self.on_error(f"disk write failed: {e}")
                    except Exception:
                        pass
                raise

    def _refresh_locked(self):
        # Update the size headers and force everything to physical disk.
        self._write_header()
        self._f.seek(0, os.SEEK_END)
        self._f.flush()
        try:
            os.fsync(self._f.fileno())
        except Exception as e:
            # Without fsync the crash-safety window widens; say so once rather
            # than spamming every flush or hiding it entirely.
            if not self._fsync_warned:
                self._fsync_warned = True
                log.warning("fsync failed for %s (crash-safety window may be "
                            "wider than flush interval): %s", self.base_path, e)

    def flush(self):
        with self._lock:
            if not self._closed:
                self._refresh_locked()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._refresh_locked()
            finally:
                try:
                    self._f.close()
                except Exception:
                    pass
        log.debug("Closed safe WAV %s (%d segment(s), last=%d data bytes)",
                  self.base_path, len(self.paths), self._data_bytes)
