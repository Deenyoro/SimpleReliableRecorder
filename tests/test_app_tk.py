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
    # Checked before creating Tk: on a desktop-less macOS runner tk.Tk() aborts
    # the whole process instead of raising.
    if os.environ.get("SRR_SKIP_GUI_TESTS"):
        test.skipTest("SRR_SKIP_GUI_TESTS is set (no interactive desktop)")
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
    _update_cancel_button = App._update_cancel_button if App else None
    _cancel_queued_jobs = App._cancel_queued_jobs if App else None
    start_recording = App.start_recording if App else None
    _build_strip = App._build_strip if App else None
    _layout_strip = App._layout_strip if App else None
    _show_strip = App._show_strip if App else None
    _hide_strip = App._hide_strip if App else None

    def __init__(self, root):
        self.root = root
        self._closing = False
        self.recording = self._starting = self._finalizing = False
        self._quitting = False
        self._ui_calls = queue.Queue()
        self._poll_err_ts = {}

    def after(self, ms, fn=None):
        return self.root.after(ms, fn)

    def after_cancel(self, job):
        return self.root.after_cancel(job)

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


@unittest.skipIf(App is None, f"ui.app not importable: {_IMPORT_ERR}")
class MnemonicTests(unittest.TestCase):
    def test_access_keys_ignore_caps_lock_and_skip_ok(self):
        root = _root_or_skip(self)
        win = tk.Toplevel(root)
        pressed = []
        stop = ttk.Button(win, text="Stop and quit",
                          command=lambda: pressed.append("stop"))
        keep = ttk.Button(win, text="Keep recording",
                          command=lambda: pressed.append("keep"))
        ok = ttk.Button(win, text="OK", command=lambda: pressed.append("ok"))
        for b in (stop, keep, ok):
            b.pack()
        App._add_mnemonics(win)
        self.assertEqual(int(stop.cget("underline")), 0)
        self.assertEqual(int(keep.cget("underline")), 0)
        # Newer Tk 8.6 reports an unset underline as "" rather than -1.
        self.assertEqual(int(ok.cget("underline") or -1), -1)
        for seq in ("<Alt-KeyPress-s>", "<Alt-KeyPress-S>",
                    "<Alt-KeyPress-k>", "<Alt-KeyPress-K>"):
            self.assertTrue(win.bind(seq), seq)
        self.assertFalse(win.bind("<Alt-KeyPress-o>"))
        # Caps Lock on: Tk reports the uppercase keysym.
        ev = types.SimpleNamespace(state=0)
        App._mnemonic(ev, stop)
        self.assertEqual(pressed, ["stop"])


@unittest.skipIf(App is None, f"ui.app not importable: {_IMPORT_ERR}")
class SavedStripTests(unittest.TestCase):
    """The done moment in a narrow window (Snap half of a laptop at 125% /
    150%): Open folder, Rename and Play must stay whole; the summary gives
    way instead."""

    def _strip(self, width):
        from ui import widgets
        root = _root_or_skip(self)
        root.deiconify()
        widgets._ui_scale = None
        widgets.apply_dark_theme(root)
        st = _Stand(root)
        st._s = 1.0
        st._strip_job = None
        outer = ttk.Frame(root, width=width, height=200)
        outer.grid_propagate(False)
        outer.columnconfigure(0, weight=1)
        outer.pack()
        st._build_strip(outer)
        self._root, self._st, self._outer = root, st, outer
        return root, st

    def _check(self, width, expect_stacked):
        root, st = self._strip(width)
        summary = "2 tracks  ·  1:02:03  ·  841 MB"
        st._show_strip("ok", "✓ Saved",
                       (summary + "  ·  Recording 23 Sep 2026, 17:44",
                        summary),
                       [("Open folder", None), ("Rename...", None),
                        ("Play", None)])
        root.update()
        st._layout_strip()
        root.update()
        right = st.strip.winfo_rootx() + st.strip.winfo_width()
        for b in st._strip_actions.winfo_children():
            self.assertTrue(b.winfo_ismapped(), b.cget("text"))
            self.assertGreaterEqual(b.winfo_width(), b.winfo_reqwidth(),
                                    b.cget("text"))
            self.assertLessEqual(b.winfo_rootx() + b.winfo_width(), right,
                                 b.cget("text"))
        self.assertEqual(st._strip_stacked[1], expect_stacked)
        text = st._strip_text.cget("text")
        self.assertLessEqual(st._strip_font.measure(text),
                             st._strip_text.winfo_width() + 1)
        return text

    def test_wide_window_keeps_one_line_and_the_name(self):
        text = self._check(1400, expect_stacked=False)
        self.assertIn("Recording 23 Sep", text)

    def test_snapped_laptop_moves_buttons_under_the_summary(self):
        text = self._check(560, expect_stacked=True)
        self.assertTrue(text.startswith("2 tracks"))

    def test_widening_again_puts_buttons_back_beside_the_close_button(self):
        self._check(560, expect_stacked=True)
        root = self._root
        self._outer.configure(width=1400)
        root.update()
        self._st._layout_strip()
        root.update()
        close = self._st._strip_close
        for b in self._st._strip_actions.winfo_children():
            self.assertLessEqual(b.winfo_rootx() + b.winfo_width(),
                                 close.winfo_rootx(), b.cget("text"))
        info = self._st._strip_actions.grid_info()
        self.assertEqual((int(info["row"]), int(info["columnspan"])), (0, 1))


@unittest.skipIf(App is None, f"ui.app not importable: {_IMPORT_ERR}")
class CancelRunningJobTests(unittest.TestCase):
    def test_cancel_stops_the_running_job(self):
        root = _root_or_skip(self)
        st = _Stand(root)
        foot = ttk.Frame(root)
        foot.pack()
        st.busy_lbl = ttk.Label(foot)
        st.busy_bar = ttk.Progressbar(foot, mode="indeterminate", length=100)
        st.busy_cancel = ttk.Button(foot, text="Cancel",
                                    command=st._cancel_queued_jobs)
        st._transcribe_busy = False
        st._combine_busy = False
        st._combine_queue, st._combine_results = [], []
        st._combine_total = 0
        st._pending_out_paths = set()
        st._refresh_library = lambda *a, **k: None
        strips = []
        st._show_strip = lambda *a, **k: strips.append(a)
        st._error = lambda *a, **k: strips.append(("ERROR",) + a)
        st._pump_ui_calls()
        started = threading.Event()

        def job():  # stands in for ffmpeg: runs until the token says stop
            from recorder import combine
            started.set()
            token = combine._progress_local.token
            end = time.monotonic() + 10
            while not token.cancelled and time.monotonic() < end:
                time.sleep(0.02)
            return False, "killed"

        out = os.path.join(tempfile.mkdtemp(prefix="srr-test-"), "o.mp4")
        st._run_combine(job, out)
        self.assertTrue(_pump_until(root, started.is_set))
        root.update()
        self.assertEqual(str(st.busy_cancel.cget("state")), "normal",
                         "the running job could not be cancelled")
        self.assertEqual(st.busy_cancel.cget("text"), "Cancel")
        st._run_combine(job, out + "2")  # a second one waits in the queue
        self.assertEqual(st.busy_cancel.cget("text"), "Cancel all")
        st.busy_cancel.invoke()
        self.assertTrue(_pump_until(root, lambda: not st._combine_busy
                                    and strips))
        self.assertEqual(strips[0][0], "info")  # 'Cancelled', not an error
        self.assertEqual(strips[0][1], "Cancelled")
        st._closing = True


@unittest.skipIf(App is None, f"ui.app not importable: {_IMPORT_ERR}")
class PumpKeepsRunningTests(unittest.TestCase):
    def test_modal_dialog_from_a_worker_callback_does_not_stall_others(self):
        """An error dialog opened from a queued callback waits in a nested
        event loop; callbacks queued behind it (Stop finishing, alerts)
        must still run while it is open."""
        root = _root_or_skip(self)
        st = _Stand(root)
        st._pump_ui_calls()
        closed = tk.BooleanVar(root, False)
        ran = []

        def modal():  # like _error(): waits until the user closes it
            ran.append("dialog")
            root.after(3000, lambda: closed.set(True))  # safety net
            root.wait_variable(closed)
            ran.append("dialog closed")

        def later():
            ran.append("later")
            closed.set(True)  # 'the user closes the dialog' afterwards
        st._ui_calls.put(modal)
        threading.Thread(target=lambda: (time.sleep(0.2),
                                         st._ui_calls.put(later))).start()
        self.assertTrue(_pump_until(root, lambda: "dialog closed" in ran))
        self.assertEqual(ran, ["dialog", "later", "dialog closed"])
        st._closing = True


if __name__ == "__main__":
    unittest.main()
