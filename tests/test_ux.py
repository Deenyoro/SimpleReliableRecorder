"""Tests for ui.ux: the plain-language helpers behind the main window.

These guard the user-facing contracts of the UI rework without needing a
display: settings labels must round-trip to the exact config tokens, '+ Add
device' must never pick a device that is already added, captured hotkeys must
be strings the `keyboard` library understands, and a saved window position
must never put the window off-screen.
"""

import os
import tempfile
import unittest

from ui import ux


class RecordingNameTests(unittest.TestCase):
    def test_stamp_names_become_readable(self):
        self.assertEqual(ux.friendly_recording_name("SRR_2026-09-23_05-49-48"),
                         "Recording 23 Sep 2026, 05:49")

    def test_user_names_are_left_alone(self):
        for name in ("Weekly standup", "SRR_notastamp", "SRR_2026-13-01_00-00-00"):
            self.assertEqual(ux.friendly_recording_name(name), name)

    def test_created_is_shortened(self):
        self.assertEqual(ux.friendly_created("2026-09-22 16:40:51"),
                         "22 Sep 2026, 16:40")
        self.assertEqual(ux.friendly_created(""), "")


class DeviceTests(unittest.TestCase):
    DEVS = (
        {"id": "m1", "name": "Microphone (Realtek)", "kind": "input", "hostapi": "WASAPI"},
        {"id": "m2", "name": "Headset", "kind": "input", "hostapi": "WASAPI"},
        {"id": "s1", "name": "Speakers (Realtek)", "kind": "loopback", "hostapi": "WASAPI"},
    )

    def test_labels_are_plain_and_unique(self):
        labels = [lbl for lbl, _ in ux.device_labels(self.DEVS)]
        self.assertEqual(labels[0], "Mic: Microphone (Realtek)")
        self.assertEqual(labels[2], "Sound from: Speakers (Realtek)")
        self.assertNotIn("WASAPI", " ".join(labels))
        dup = [*self.DEVS, dict(self.DEVS[0], id="m1b", hostapi="MME")]
        labels = [lbl for lbl, _ in ux.device_labels(dup)]
        self.assertEqual(len(labels), len(set(labels)))

    def test_add_device_skips_devices_already_added(self):
        # Regression: '+ Add device' used to add a duplicate of row 1 that the
        # recorder then silently dropped.
        d = ux.next_unused_device(self.DEVS, {"m1"})
        self.assertEqual(d["id"], "m2")
        d = ux.next_unused_device(self.DEVS, {"m1", "m2"}, prefer_kind="input")
        self.assertEqual(d["id"], "s1")
        self.assertIsNone(ux.next_unused_device(self.DEVS, {"m1", "m2", "s1"}))

    def test_alert_text_has_no_internal_brackets(self):
        msg = ux.humanize_subsystem_error(
            "Microphone (Realtek(R) Audio) [input]", "stopped delivering audio")
        self.assertEqual(msg, "Microphone 'Microphone (Realtek(R) Audio)': "
                              "stopped delivering audio")
        self.assertTrue(ux.humanize_subsystem_error("screen", "x")
                        .startswith("Screen recording"))


class ChoiceTests(unittest.TestCase):
    def test_every_label_round_trips_to_its_config_token(self):
        for key, pairs in ux.CHOICES.items():
            labels = [lbl for _, lbl in pairs]
            self.assertEqual(len(labels), len(set(labels)), key)
            for value, label in pairs:
                self.assertEqual(ux.choice_label(key, value), label)
                self.assertEqual(ux.choice_value(key, label), value)

    def test_config_tokens_match_defaults(self):
        from recorder.config import DEFAULTS
        for key, pairs in ux.CHOICES.items():
            self.assertIn(DEFAULTS[key], [v for v, _ in pairs], key)

    def test_unknown_value_is_shown_not_blank(self):
        self.assertEqual(ux.choice_label("screen_quality", "ultra"), "ultra")


class HotkeyCaptureTests(unittest.TestCase):
    def test_function_key(self):
        self.assertEqual(ux.hotkey_from_event("F8", 0), "f8")

    def test_modifiers(self):
        self.assertEqual(ux.hotkey_from_event("space", ux.STATE_CONTROL),
                         "ctrl+space")
        self.assertEqual(
            ux.hotkey_from_event("m", ux.STATE_CONTROL | ux.STATE_SHIFT
                                 | ux.STATE_ALT_WIN), "ctrl+alt+shift+m")
        self.assertEqual(ux.hotkey_from_event("M", ux.STATE_SHIFT), "shift+m")

    def test_bare_modifier_waits(self):
        self.assertIsNone(ux.hotkey_from_event("Control_L", ux.STATE_CONTROL))


class FormatTests(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(ux.fmt_bytes(512), "512 B")
        self.assertEqual(ux.fmt_bytes(41 * 1024 * 1024), "41 MB")
        self.assertEqual(ux.fmt_bytes(1.5 * 1024 ** 3), "1.5 GB")

    def test_durations(self):
        self.assertEqual(ux.fmt_duration(192), "3:12")
        self.assertEqual(ux.fmt_duration(3723), "1:02:03")
        self.assertEqual(ux.fmt_duration(None), "")

    def test_take_summary(self):
        self.assertEqual(ux.take_summary(2, 1, 192, 41 * 1024 * 1024),
                         "2 tracks + screen  \u00b7  3:12  \u00b7  41 MB")
        self.assertEqual(ux.contents_text(0, 1), "screen only")

    def test_total_size_ignores_missing(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.bin")
            with open(p, "wb") as fh:
                fh.write(b"x" * 10)
            self.assertEqual(ux.total_size([p, os.path.join(d, "gone")]), 10)

    def test_middle_ellipsize_keeps_both_ends(self):
        text = "C:\\Users\\someone\\Videos\\SimpleReliableRecorder"
        out = ux.middle_ellipsize(text, 20, len)
        self.assertLessEqual(len(out), 20)
        self.assertTrue(out.startswith("C:\\"))
        self.assertTrue(out.endswith("Recorder"))
        self.assertEqual(ux.middle_ellipsize("short", 20, len), "short")


class FriendlyErrorTests(unittest.TestCase):
    def test_known_errors(self):
        self.assertIn("disk is full",
                      ux.friendly_error("OSError: [Errno 28] No space left")[0])
        self.assertIn("exclusively",
                      ux.friendly_error("Error 0x8889000a")[0])
        self.assertIn("ffmpeg", ux.friendly_error("ffmpeg not found")[0])

    def test_unknown_error(self):
        self.assertEqual(ux.friendly_error("weird"), (None, None))


class GeometryTests(unittest.TestCase):
    def test_valid_geometry_is_kept(self):
        self.assertEqual(ux.sane_geometry("1200x800+40+30", 1920, 1080),
                         "1200x800+40+30")

    def test_off_screen_window_is_brought_back(self):
        # Saved on a second monitor that is no longer connected.
        g = ux.sane_geometry("1200x800+2500+100", 1920, 1080)
        _w, rest = g.split("x")
        x = int(rest.split("+")[1])
        self.assertLess(x, 1920 - 120)

    def test_too_big_is_clamped_and_garbage_rejected(self):
        self.assertEqual(ux.sane_geometry("3000x2000+0+0", 1366, 768),
                         "1366x768+0+0")
        self.assertIsNone(ux.sane_geometry("banana", 1920, 1080))
        self.assertIsNone(ux.sane_geometry("10x10+0+0", 1920, 1080))


COL_W = {"created": 150, "length": 70, "contents": 140, "size": 70}


class LibraryColumnTests(unittest.TestCase):
    W = COL_W

    def test_wide_list_shows_everything_in_order(self):
        self.assertEqual(ux.library_columns(1000, self.W, 180),
                         ["name", "created", "length", "contents", "size"])

    def test_snapped_list_drops_contents_before_size(self):
        # 180 + 150 + 70 + 70 = 470 fits; + contents would not.
        self.assertEqual(ux.library_columns(500, self.W, 180),
                         ["name", "created", "length", "size"])

    def test_tiny_list_keeps_a_readable_name(self):
        self.assertEqual(ux.library_columns(200, self.W, 180), ["name"])


class EllipsizeTests(unittest.TestCase):
    def test_end_ellipsize_keeps_the_start(self):
        out = ux.end_ellipsize("Weekly standup with the design team", 10, len)
        self.assertTrue(out.startswith("Weekly"))
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(len(out), 10)

    def test_short_text_untouched(self):
        self.assertEqual(ux.end_ellipsize("Short", 10, len), "Short")


class GeometryBoundsTests(unittest.TestCase):
    def test_big_window_on_4k_is_kept(self):
        # Round 1 halved screens >= 2000 px tall: 2400x1600 came back
        # as 2400x1080 on a 3840x2160 display.
        self.assertEqual(ux.sane_geometry("2400x1600+100+50", 3840, 2160),
                         "2400x1600+100+50")

    def test_window_is_moved_fully_on_screen(self):
        self.assertEqual(ux.sane_geometry("1920x1080+200+100", 1920, 1080),
                         "1920x1080+0+0")
        g = ux.parse_geometry(ux.sane_geometry("1200x800+900+500",
                                               1920, 1080))
        self.assertLessEqual(g[2] + g[0], 1920)
        self.assertLessEqual(g[3] + g[1], 1080)

    def test_secondary_monitor_left_of_primary_is_kept(self):
        self.assertEqual(ux.parse_geometry("1200x800+-1700+100"),
                         (1200, 800, -1700, 100))
        self.assertEqual(
            ux.sane_geometry("1200x800+-1700+100", 1920, 1080,
                             bounds=(-1920, 0, 0, 1040)),
            "1200x800+-1700+100")

    def test_gone_monitor_is_centred_on_the_given_area(self):
        g = ux.sane_geometry("1200x800+2500+100", 1920, 1080,
                             bounds=(0, 0, 1920, 1040))
        self.assertEqual(g, "1200x800+360+80")


if __name__ == "__main__":
    unittest.main()
