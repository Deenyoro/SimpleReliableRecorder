"""Tests for recorder.safewav.SafeWavWriter: the on-disk file must be a valid
WAV while recording (crash safety) and across 4 GiB segment rollover."""

import builtins
import os
import struct
import tempfile
import unittest
import wave
from unittest import mock

import numpy as np

from recorder.safewav import SafeWavWriter


def _read_header(path):
    with open(path, "rb") as fh:
        hdr = fh.read(44)
    riff, riff_size, wave_id = struct.unpack("<4sI4s", hdr[:12])
    data_id, data_size = struct.unpack("<4sI", hdr[36:44])
    return riff, riff_size, wave_id, data_id, data_size


class SafeWavWriterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "take.wav")

    def tearDown(self):
        self._tmp.cleanup()

    def test_rejects_unknown_subtype(self):
        with self.assertRaises(ValueError):
            SafeWavWriter(self.path, 48000, 2, subtype="PCM_24")

    def test_pcm16_roundtrip_readable_by_wave_module(self):
        w = SafeWavWriter(self.path, 48000, 2)
        block = np.array([[0.5, -0.5], [2.0, -2.0]], dtype=np.float32)
        w.write(block)
        w.close()
        with wave.open(self.path, "rb") as r:
            self.assertEqual(r.getnchannels(), 2)
            self.assertEqual(r.getframerate(), 48000)
            self.assertEqual(r.getsampwidth(), 2)
            self.assertEqual(r.getnframes(), 2)
            samples = np.frombuffer(r.readframes(2), dtype="<i2")
        # Out-of-range input is clipped rather than wrapping around.
        self.assertEqual(samples.tolist(), [16383, -16383, 32767, -32767])

    def test_mono_1d_block_and_float_subtype(self):
        w = SafeWavWriter(self.path, 16000, 1, subtype="FLOAT")
        w.write(np.array([0.25, -0.25, 1.5], dtype=np.float32))
        w.close()
        riff, riff_size, wave_id, data_id, data_size = _read_header(self.path)
        self.assertEqual((riff, wave_id, data_id), (b"RIFF", b"WAVE", b"data"))
        self.assertEqual(data_size, 3 * 4)
        self.assertEqual(riff_size, 36 + data_size)
        with open(self.path, "rb") as fh:
            fmt_code = struct.unpack("<H", fh.read(22)[20:22])[0]
        self.assertEqual(fmt_code, 3)  # IEEE float

    def test_header_is_valid_before_close(self):
        # Crash safety: after a periodic flush the header already describes
        # every byte written, without close() ever being called.
        w = SafeWavWriter(self.path, 48000, 1, flush_interval=0.0)
        try:
            w.write(np.zeros(100, dtype=np.float32))
            _, riff_size, _, _, data_size = _read_header(self.path)
            self.assertEqual(data_size, 200)
            self.assertEqual(riff_size, 236)
            self.assertEqual(os.path.getsize(self.path), 44 + 200)
        finally:
            w.close()

    def test_write_after_close_is_ignored(self):
        w = SafeWavWriter(self.path, 48000, 1)
        w.close()
        w.write(np.zeros(10, dtype=np.float32))
        w.close()  # idempotent
        self.assertEqual(_read_header(self.path)[4], 0)

    def test_rollover_creates_valid_numbered_segments(self):
        w = SafeWavWriter(self.path, 48000, 1)
        w._roll_at = 8  # 4 mono PCM_16 frames per segment
        for _ in range(3):
            w.write(np.zeros(3, dtype=np.float32))  # 6 bytes per block
        w.close()
        root = os.path.join(self._tmp.name, "take")
        self.assertEqual(w.paths, [self.path, root + "_part2.wav",
                                   root + "_part3.wav"])
        for p in w.paths:
            with wave.open(p, "rb") as r:
                self.assertEqual(r.getnframes(), 3)

    def test_failed_rollover_open_recovers_on_next_write(self):
        w = SafeWavWriter(self.path, 48000, 1)
        w._roll_at = 8
        w.write(np.zeros(3, dtype=np.float32))
        real_open = builtins.open
        calls = {"n": 0}

        def flaky_open(path, *a, **kw):
            if str(path).endswith("_part2.wav") and calls["n"] == 0:
                calls["n"] += 1
                raise PermissionError("locked by antivirus")
            return real_open(path, *a, **kw)

        with mock.patch("builtins.open", flaky_open), \
                self.assertRaises(PermissionError):
            w.write(np.zeros(3, dtype=np.float32))
        # The writer must not be wedged on the closed first segment.
        w.write(np.zeros(3, dtype=np.float32))
        w.close()
        part2 = os.path.join(self._tmp.name, "take_part2.wav")
        self.assertEqual(w.paths, [self.path, part2])
        with wave.open(part2, "rb") as r:
            self.assertEqual(r.getnframes(), 3)
        with wave.open(self.path, "rb") as r:
            self.assertEqual(r.getnframes(), 3)


if __name__ == "__main__":
    unittest.main()
