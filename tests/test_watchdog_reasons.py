"""Every alert the watchdog can raise reaches people in everyday words
(the gold banner and the watchdog's own message box); the raw reason is
only for the log."""

import unittest

from recorder import watchdog

INTERNAL = ("heartbeat", "encoder", "process", "AUDIO", "SCREEN")


def _reasons():
    """Drive evaluate_heartbeat into each alert it knows."""
    out = []
    st = watchdog.WatchdogState()
    for i in range(200):
        action, reason = watchdog.evaluate_heartbeat(
            st, None, None, float(i), 6.0, False)
        if action == "alert":
            out.append(reason)
            break
    hb = {"recording": True}
    st = watchdog.WatchdogState()
    for t in (0.0, 10.0, 20.0):
        action, reason = watchdog.evaluate_heartbeat(
            st, b"same", hb, t, 6.0, False)
    out.append(reason)
    for bad in ({"audio_ok": False, "audio_detail": "9.0s since last write"},
                {"screen_enabled": True, "screen_alive": False},
                {"screen_enabled": True, "screen_alive": True,
                 "screen_progressing": False}):
        st = watchdog.WatchdogState()
        for n in range(3):
            action, reason = watchdog.evaluate_heartbeat(
                st, str(n).encode(), dict(hb, **bad), float(n), 6.0, False)
        out.append(reason)
    out.append("The recorder application is no longer running "
               "(process gone).")
    return out


class PlainReasonTests(unittest.TestCase):
    def test_every_alert_has_plain_words(self):
        reasons = _reasons()
        self.assertEqual(len(reasons), 6)
        for raw in reasons:
            self.assertTrue(raw)
            plain = watchdog.plain_reason(raw)
            self.assertNotEqual(plain, raw)
            for word in INTERNAL:
                self.assertNotIn(word, plain, raw)

    def test_unknown_reason_passes_through(self):
        self.assertEqual(watchdog.plain_reason("Microphone unplugged."),
                         "Microphone unplugged.")
        self.assertEqual(watchdog.plain_reason(None), "")


if __name__ == "__main__":
    unittest.main()
