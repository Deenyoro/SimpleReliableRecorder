"""The hotkey capture dialog refuses key names the global-hotkey library
cannot parse, instead of saving them and then blaming another app."""

import unittest

from recorder import hotkeys


class HotkeyValidationTests(unittest.TestCase):
    def test_empty_or_unknown_availability_is_not_rejected(self):
        self.assertTrue(hotkeys.is_valid_hotkey(""))

    @unittest.skipUnless(hotkeys.available(), "keyboard library missing")
    def test_parse_errors_are_rejected(self):
        orig = hotkeys._kb.parse_hotkey

        def fake(h):
            if h == "adiaeresis":
                raise ValueError("Key 'adiaeresis' is not mapped")
            return [(1,)]
        hotkeys._kb.parse_hotkey = fake
        try:
            self.assertFalse(hotkeys.is_valid_hotkey("adiaeresis"))
            self.assertTrue(hotkeys.is_valid_hotkey("f8"))
        finally:
            hotkeys._kb.parse_hotkey = orig

    @unittest.skipUnless(hotkeys.available(), "keyboard library missing")
    def test_cannot_check_means_allowed(self):
        orig = hotkeys._kb.parse_hotkey

        def fake(h):
            raise ImportError("You must be root to use this library on linux.")
        hotkeys._kb.parse_hotkey = fake
        try:
            self.assertTrue(hotkeys.is_valid_hotkey("f8"))
        finally:
            hotkeys._kb.parse_hotkey = orig


if __name__ == "__main__":
    unittest.main()
