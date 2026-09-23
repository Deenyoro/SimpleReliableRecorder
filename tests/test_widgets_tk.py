"""Tk-level tests for the device card (ui.widgets.DeviceRow).

Regression for the audit's critical issue: while a take is running the device
chooser and Remove must be locked (the recorder keeps the devices it started
with), but Mute and Volume must stay live because they apply mid-recording.

Skipped automatically where Tk cannot open a display (headless Linux CI).
"""

import tkinter as tk
import unittest

from ui import widgets

DEVICES = [
    {"id": "m1", "name": "Microphone (Realtek)", "kind": "input",
     "hostapi": "WASAPI", "channels": 2},
    {"id": "s1", "name": "Speakers (Realtek)", "kind": "loopback",
     "hostapi": "WASAPI", "channels": 2},
]


def _root_or_skip(test):
    try:
        root = tk.Tk()
    except tk.TclError as e:
        test.skipTest(f"no display for Tk: {e}")
    root.withdraw()
    test.addCleanup(root.destroy)
    widgets._ui_scale = None
    widgets.apply_dark_theme(root)
    return root


class DeviceRowTests(unittest.TestCase):
    def setUp(self):
        self.root = _root_or_skip(self)
        self.removed = []
        self.row = widgets.DeviceRow(
            self.root, DEVICES, self.removed.append,
            preset={"id": "s1", "name": "Speakers (Realtek)", "kind": "loopback"})

    def test_preset_is_selected_with_a_plain_label(self):
        self.assertEqual(self.row.get_selection()["id"], "s1")
        self.assertEqual(self.row.var.get(), "Sound from: Speakers (Realtek)")
        # The recorder's internal source label is unchanged.
        self.assertEqual(self.row.current_source_label(),
                         "Speakers (Realtek) [loopback]")

    def test_locked_while_recording_but_mute_and_volume_stay_live(self):
        self.row.set_editable(False)
        self.assertEqual(str(self.row.combo.cget("state")), "disabled")
        self.assertTrue(self.row.remove_btn.instate(["disabled"]))
        self.assertEqual(str(self.row.mute_btn.cget("state")), "normal")
        self.assertFalse(self.row.scale.instate(["disabled"]))
        self.row._toggle_mute()
        self.assertTrue(self.row.is_muted())

        self.row.set_editable(True)
        self.assertEqual(str(self.row.combo.cget("state")), "readonly")
        self.assertFalse(self.row.remove_btn.instate(["disabled"]))

    def test_duplicate_warning_shows_and_clears(self):
        self.row.set_warning("Already added above")
        self.assertEqual(self.row.warn_lbl.winfo_manager(), "grid")
        self.row.set_warning("")
        self.assertEqual(self.row.warn_lbl.winfo_manager(), "")


class ToggleSwitchTests(unittest.TestCase):
    def test_disabled_switch_ignores_clicks(self):
        root = _root_or_skip(self)
        var = tk.BooleanVar(master=root, value=False)
        sw = widgets.ToggleSwitch(root, var, text="Record the screen too")
        sw.set_enabled(False)
        sw._on_click()
        self.assertFalse(var.get())
        sw.set_enabled(True)
        sw._on_click()
        self.assertTrue(var.get())


class WorkAreaTests(unittest.TestCase):
    def test_work_area_is_a_real_rectangle(self):
        root = _root_or_skip(self)
        (left, top, right, bottom), found = widgets.work_area(root)
        self.assertTrue(found)
        self.assertGreater(right - left, 200)
        self.assertGreater(bottom - top, 200)


class ToggleSwitchBindingTests(unittest.TestCase):
    def test_destroy_removes_only_its_parent_binding(self):
        root = _root_or_skip(self)
        parent = tk.Frame(root)
        parent.pack()
        seen = []
        parent.bind("<Configure>", lambda e: seen.append("other"), add="+")
        sw = widgets.ToggleSwitch(parent, tk.BooleanVar(root), text="Switch")
        sw.pack()
        self.assertIn(sw._fit_bind, parent.bind("<Configure>"))
        sw.destroy()
        script = parent.bind("<Configure>")
        self.assertNotIn("_fit_label", script)
        self.assertTrue(script.strip(), "the other binding was removed too")
        parent.event_generate("<Configure>", width=300, height=40)
        self.assertEqual(seen, ["other"])


if __name__ == "__main__":
    unittest.main()
