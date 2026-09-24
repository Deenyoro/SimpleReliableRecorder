"""Logic-level tests for ui.app.App that need no window and no audio device.

The methods under test are plain functions on App, so they are exercised on a
small stand-in object that carries only the state they touch. This keeps the
tests independent of real sound hardware (CI runners have none).

Regression covered: Mute/Volume changes made while a recording is still
starting (devices opening on a worker, audio_rec not set yet) used to change
only the config, so the take ran with the OLD mute state while the card showed
the new one - a privacy problem.
"""

import sys
import threading
import time
import types
import unittest

try:  # soundcard needs a native audio client library; not on every host
    import soundcard  # noqa: F401
except (ImportError, OSError, RuntimeError, AssertionError):  # no libpulse
    sys.modules["soundcard"] = types.ModuleType("soundcard")

try:
    from recorder.audio import CaptureSource
    from ui.app import App
    _IMPORT_ERR = None
except (ImportError, OSError, RuntimeError) as e:  # pragma: no cover
    App = None
    _IMPORT_ERR = e


class FakeRow:
    def __init__(self, name, kind, gain=1.0, muted=False, dev_id=None):
        self._dev = {"id": dev_id or name, "name": name, "kind": kind,
                     "hostapi": "WASAPI", "channels": 2}
        self.gain, self.muted = gain, muted

    def get_selection(self):
        return self._dev

    def current_source_label(self):
        return f'{self._dev["name"]} [{self._dev["kind"]}]'

    def get_gain(self):
        return self.gain

    def is_muted(self):
        return self.muted


class FakeRecorder:
    """Stands in for AudioRecorder: same set_gain/set_muted contract, and a
    start() that takes a while, like opening a real WASAPI device."""

    def __init__(self, sources, delay=0.3):
        self.sources = list(sources)
        self.delay = delay

    def start(self):
        time.sleep(self.delay)

    def set_gain(self, label, gain):
        for s in self.sources:
            if s.label == label:
                s.gain = float(gain)

    def set_muted(self, label, muted):
        for s in self.sources:
            if s.label == label:
                s.muted = bool(muted)


def _stand_in(rows):
    class Stub:
        _on_row_change = App._on_row_change
        _set_pending_level = App._set_pending_level
        _drop_pending = App._drop_pending
        _apply_row_levels = App._apply_row_levels
        _row_source_key = App._row_source_key
        _set_row_level = App._set_row_level

        def _request_save(self):
            pass

        def _save_settings(self):
            pass
    st = Stub()
    st.recording = False
    st._starting = True
    st.audio_rec = None
    st.level_monitor = None
    st._pending_sources = []
    st._device_rows = rows
    return st


def _sources_for(rows):
    """Like App._gather_sources: the first card per (id, kind) wins."""
    out, seen = [], set()
    for r in rows:
        d = r.get_selection()
        if (d["id"], d["kind"]) in seen:
            continue
        seen.add((d["id"], d["kind"]))
        out.append(CaptureSource.from_device(d, gain=r.get_gain(),
                                             muted=r.is_muted(),
                                             track_name="t"))
    return out


@unittest.skipIf(App is None, f"ui.app not importable: {_IMPORT_ERR}")
class MuteWhileStartingTests(unittest.TestCase):
    def test_mute_during_slow_start_reaches_the_recorder(self):
        mic = FakeRow("Microphone", "input")
        app = _stand_in([mic])
        sources = _sources_for([mic])
        app._pending_sources.append(sources)   # as start_recording does
        rec = FakeRecorder(sources)
        t = threading.Thread(target=rec.start)
        t.start()
        # The user mutes while the device is still opening.
        mic.muted = True
        app._on_row_change("mute", mic)
        # It applies to the very objects the recorder captures from ...
        self.assertTrue(rec.sources[0].muted)
        t.join()
        # ... and once the recorder is handed over, the cards win again.
        app._drop_pending(sources)
        app._apply_row_levels(rec)
        self.assertTrue(rec.sources[0].muted)
        self.assertEqual(app._pending_sources, [])

    def test_levels_changed_after_the_worker_copied_them_are_reapplied(self):
        mic = FakeRow("Microphone", "input", gain=1.0, muted=False)
        spk = FakeRow("Speakers", "loopback", gain=1.0, muted=True)
        app = _stand_in([mic, spk])
        rec = FakeRecorder(_sources_for([mic, spk]))
        # Changed with no pending list registered (e.g. a restart path that
        # built its sources earlier): only the hand-over can fix it.
        mic.muted, mic.gain, spk.muted = True, 0.5, False
        app._apply_row_levels(rec)
        self.assertTrue(rec.sources[0].muted)
        self.assertAlmostEqual(rec.sources[0].gain, 0.5)
        self.assertFalse(rec.sources[1].muted)

    def test_gain_during_start_is_applied_to_pending_sources(self):
        mic = FakeRow("Microphone", "input")
        app = _stand_in([mic])
        sources = _sources_for([mic])
        app._pending_sources.append(sources)
        mic.gain = 1.8
        app._on_row_change("gain", mic)
        self.assertAlmostEqual(sources[0].gain, 1.8)



@unittest.skipIf(App is None, f"ui.app not importable: {_IMPORT_ERR}")
class LevelsFollowTheRecordedCardTests(unittest.TestCase):
    """Mute/Volume are matched per device, never by the "name [kind]" label:
    a skipped duplicate card, or another device with the same name, must not
    change what the recorded card captures."""

    def test_muted_duplicate_card_does_not_silence_the_take(self):
        first = FakeRow("Mic A", "input", muted=False)
        dup = FakeRow("Mic A", "input", muted=True, gain=0.2)
        app = _stand_in([first, dup])
        sources = _sources_for([first, dup])
        self.assertEqual(len(sources), 1)
        app._pending_sources.append(sources)
        rec = FakeRecorder(sources)
        app._apply_row_levels(rec)
        self.assertFalse(rec.sources[0].muted)
        self.assertAlmostEqual(rec.sources[0].gain, 1.0)
        # A click on the skipped card changes nothing that is recorded.
        app._on_row_change("mute", dup)
        app._on_row_change("gain", dup)
        app.recording, app._starting, app.audio_rec = True, False, rec
        app._on_row_change("mute", dup)
        self.assertFalse(rec.sources[0].muted)
        self.assertAlmostEqual(rec.sources[0].gain, 1.0)

    def test_same_name_devices_keep_their_own_mute(self):
        for first_muted in (True, False):
            a = FakeRow("USB Mic", "input", muted=first_muted, dev_id="id-1")
            b = FakeRow("USB Mic", "input", muted=not first_muted,
                        dev_id="id-2")
            app = _stand_in([a, b])
            sources = _sources_for([a, b])
            self.assertEqual(len(sources), 2)
            rec = FakeRecorder(sources)
            app._apply_row_levels(rec)
            self.assertEqual(rec.sources[0].muted, first_muted)
            self.assertEqual(rec.sources[1].muted, not first_muted)
            # Live toggle during the take touches only that device.
            app.recording, app._starting, app.audio_rec = True, False, rec
            a.muted = not first_muted
            app._on_row_change("mute", a)
            self.assertEqual(rec.sources[0].muted, not first_muted)
            self.assertEqual(rec.sources[1].muted, not first_muted)
            b.muted = first_muted
            app._on_row_change("mute", b)
            self.assertEqual(rec.sources[0].muted, not first_muted)
            self.assertEqual(rec.sources[1].muted, first_muted)


if __name__ == "__main__":
    unittest.main()
