"""Combine/convert progress: ffmpeg's -progress output becomes a percentage.

The streaming path is exercised with a tiny fake 'ffmpeg' script (POSIX only;
the parsing tests run everywhere).
"""

import os
import stat
import sys
import tempfile
import textwrap
import unittest

from recorder import combine
from ui import ux


class ProgressParsingTests(unittest.TestCase):
    def test_out_time_lines(self):
        self.assertEqual(combine.progress_seconds("out_time_us=4200000\n"), 4.2)
        # ffmpeg's out_time_ms is microseconds too.
        self.assertEqual(combine.progress_seconds("out_time_ms=1500000"), 1.5)
        self.assertIsNone(combine.progress_seconds("out_time=00:00:04.20"))
        self.assertIsNone(combine.progress_seconds("progress=continue"))
        self.assertIsNone(combine.progress_seconds("out_time_us=N/A"))

    def test_eta_text(self):
        self.assertEqual(ux.eta_text(1, 0.5), "")      # too early to tell
        self.assertEqual(ux.eta_text(10, 0.5), "less than a minute left")
        self.assertEqual(ux.eta_text(120, 0.5), "about 2 min left")
        self.assertEqual(ux.eta_text(100, 1.0), "")


FAKE_FFMPEG = textwrap.dedent('''\
    #!{python}
    import sys, time
    mode = "{mode}"
    sys.stderr.write("ffmpeg version fake\\n")
    for us in (0, 2500000, 5000000, 7500000):
        print("out_time_us=%d" % us)
        print("progress=continue", flush=True)
        if mode == "slow":
            time.sleep(1.0)
    print("progress=end", flush=True)
    if mode == "fail":
        sys.stderr.write("Error: something broke\\n")
        sys.exit(1)
''')


@unittest.skipUnless(os.name == "posix", "fake ffmpeg is a shebang script")
class StreamingRunTests(unittest.TestCase):
    def _fake(self, mode):
        d = tempfile.mkdtemp(prefix="srr-fake-ffmpeg-")
        path = os.path.join(d, "ffmpeg")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(FAKE_FFMPEG.format(python=sys.executable, mode=mode))
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path

    def test_fractions_reach_one_and_result_is_ok(self):
        seen = []
        with combine.report_progress(seen.append):
            ok, detail = combine._run([self._fake("ok"), "-i", "in.wav",
                                       "out.wav"], timeout=30, expected=10.0)
        self.assertTrue(ok, detail)
        self.assertEqual(seen[0], 0.0)
        self.assertIn(0.5, seen)
        self.assertEqual(seen[-1], 1.0)
        self.assertEqual(seen, sorted(seen))
        self.assertIn("ffmpeg version fake", detail)

    def test_failure_keeps_the_stderr_tail(self):
        with combine.report_progress(lambda f: None):
            ok, detail = combine._run([self._fake("fail")], timeout=30,
                                      expected=10.0)
        self.assertFalse(ok)
        self.assertIn("something broke", detail)

    def test_timeout_still_raises_the_friendly_error(self):
        with combine.report_progress(lambda f: None), \
                self.assertRaises(RuntimeError) as cm:
            combine._run([self._fake("slow")], timeout=1, expected=10.0)
        self.assertIn("did not finish", str(cm.exception))

    def test_without_a_callback_nothing_changes(self):
        ok, _detail = combine._run([self._fake("ok")], timeout=30,
                                   expected=10.0)
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
