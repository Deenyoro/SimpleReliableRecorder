"""Tk-level tests for App wiring that runs on a real Tk event loop.

The App methods under test are bound to a small stand-in that owns a real Tk
root (for after(), the progress bar and the queue pump), so no sound device,
tray or hotkey library is needed.

Regressions covered:
* The second of two queued combine/convert jobs showed a frozen progress bar
  (the marquee was only started when the bar first appeared), which looked
  like a hang for any job of unknown length.
* Record must hand device opening to a worker thread and come back to the Tk
  thread for the hand-over - a slow device open must never freeze the window.

Skipped automatically where Tk cannot open a display (headless Linux CI).
"""

import os
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
import types
import unittest
from tkinter import ttk

try:  # soundcard needs a native audio client library; not on every host
    import soundcard  # noqa: F401
except (ImportError, OSError, RuntimeError, AssertionError):  # no libpulse
    sys.modules["soundcard"] = types.ModuleType("soundcard")

try:
    from ui.app import App
    _IMPORT_ERR = None
except (ImportError, OSError, RuntimeError) as e:  # pragma: no cover
    App = None
    _IMPORT_ERR = e


def _root_or_skip(test):
    try:
        root = tk.Tk()
    except tk.TclError as e:
        test.skipTest(f"no display for Tk: {e}")
    root.withdraw()
    test.addCleanup(root.destroy)
    return root


class _Stand:
    """Carries only the state the methods under test touch."""

    _safe_after = App._safe_after if App else None
    _pump_ui_calls = App._pump_ui_calls if App else None
    _set_busy = App._set_busy if App else None
    _run_combine = App._run_combine if App else None
    _combine_done = App._combine_done if App else None
    _combine_progress = App._combine_progress if App else None
    _combine_progress_text = App._combine_progress_text if App else None
    start_recording = App.start_recording if App else None

    def __init__(self, root):
        self.root = root
        self._closing = False
        self.recording = self._starting = self._finalizing = False
        self._quitting = False
        self._ui_calls = queue.Queue()
        self._poll_err_ts = {}

    def after(self, ms, fn=None):
        return self.root.after(ms, fn)

    def _poll_err(self, key, e):
        raise AssertionError(f"{key}: {e}")

    def _restore_status(self):
        pass


def _pump_until(root, cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        root.update()
        if cond():
            return True
        time.sleep(0.01)
    return False


@unittest.skipIf(App is None, f"ui.app not importable: {_IMPORT_ERR}")
class QueuedJobProgressTests(unittest.TestCase):
    def test_second_queued_job_has_a_moving_bar(self):
        root = _root_or_skip(self)
        st = _Stand(root)
        foot = ttk.Frame(root)
        foot.pack()
        st.busy_lbl = ttk.Label(foot)
        st.busy_bar = ttk.Progressbar(foot, mode="indeterminate", length=100)
        st.busy_cancel = ttk.Button(foot, text="Cancel remaining")
        st._transcribe_busy = False
        st._combine_busy = False
        st._combine_queue, st._combine_results = [], []
        st._combine_total = 0
        st._pending_out_paths = set()
        st._pump_ui_calls()

        tmp = tempfile.mkdtemp(prefix="srr-test-")
        out1, out2 = os.path.join(tmp, "o1.wav"), os.path.join(tmp, "o2.wav")
        release = threading.Event()
        self.addCleanup(release.set)
        second_started = threading.Event()

        def job1():
            from recorder import combine
            cb = combine._progress_local.cb
            for f in (0.3, 0.6):  # a known-length job: real percentages
                cb(f)
                time.sleep(0.05)
            open(out1, "wb").close()
            return True, "ok"

        def job2():  # unknown length: reports no progress at all
            second_started.set()
            release.wait(10)
            return False, "released"

        st._run_combine(job1, out1)
        st._run_combine(job2, out2)
        self.assertTrue(_pump_until(root, second_started.is_set),
                        "second job never started")
        root.update()
        self.assertEqual(str(st.busy_bar.cget("mode")), "indeterminate")
        self.assertIn("2 of 2", st.busy_lbl.cget("text"))
        v0 = float(st.busy_bar.cget("value"))
        _pump_until(root, lambda: False, timeout=0.4)
        v1 = float(st.busy_bar.cget("value"))
        self.assertNotEqual(v0, v1, "marquee frozen on the 2nd job")
        st._closing = True


@unittest.skipIf(App is None, f"ui.app not importable: {_IMPORT_ERR}")
class StartOffTheTkThreadTests(unittest.TestCase):
    def test_slow_device_open_runs_on_a_worker(self):
        root = _root_or_skip(self)
        st = _Stand(root)
        st._pending_sources = []
        seen = {}
        plan = {"sources": ["mic"]}

        st._prepare_start = lambda: plan
        st._set_starting_ui = lambda on: seen.setdefault("starting_ui", on)

        def slow_worker(p):  # like opening a real WASAPI device
            seen["worker_thread"] = threading.current_thread().name
            time.sleep(0.4)
            return {"audio": "rec"}

        def finish(p, res):
            seen["finish_on_main"] = (threading.current_thread()
                                      is threading.main_thread())
            seen["res"] = res
            st._starting = False
        st._start_worker = slow_worker
        st._finish_start = finish
        st._pump_ui_calls()

        t0 = time.monotonic()
        st.start_recording()
        self.assertLess(time.monotonic() - t0, 0.2,
                        "Record blocked the Tk thread")
        self.assertTrue(st._starting)
        self.assertTrue(seen["starting_ui"])
        self.assertEqual(st._pending_sources, [["mic"]])
        st.start_recording()  # a second click while starting is ignored
        self.assertTrue(_pump_until(root, lambda: "res" in seen))
        self.assertEqual(seen["worker_thread"], "start")
        self.assertTrue(seen["finish_on_main"])
        self.assertEqual(seen["res"], {"audio": "rec"})
        st._closing = True


if __name__ == "__main__":
    unittest.main()
