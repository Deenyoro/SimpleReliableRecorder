"""Tests for recorder.library: pruning must never drop a take that still has
files on disk, and must never keep pointing at files that are gone."""

import os
import tempfile
import unittest

from recorder import library


class PruneTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _touch(self, name):
        p = os.path.join(self.dir, name)
        with open(p, "wb") as fh:
            fh.write(b"x")
        return p

    def _missing(self, name):
        return os.path.join(self.dir, name)

    def test_keeps_entry_with_existing_files(self):
        a = self._touch("t_mic.wav")
        v = self._touch("t_screen.mkv")
        e = library.make_entry("1", "t", self.dir, [a], v, "2026-01-01")
        kept, removed = library.prune([e])
        self.assertEqual(removed, 0)
        self.assertEqual(kept[0]["audio"], [a])
        self.assertEqual(kept[0]["video"], v)

    def test_drops_missing_files_from_kept_entry(self):
        a = self._touch("t_mic.wav")
        gone = self._missing("t_loop.wav")
        e = library.make_entry("1", "t", self.dir, [a, gone], "", "x")
        kept, removed = library.prune([e])
        self.assertEqual(removed, 0)
        self.assertEqual(kept[0]["audio"], [a])

    def test_prunes_entry_whose_files_are_gone_but_folder_exists(self):
        e = library.make_entry("1", "t", self.dir,
                               [self._missing("t_mic.wav")], "", "x")
        kept, removed = library.prune([e])
        self.assertEqual((kept, removed), ([], 1))

    def test_keeps_entry_when_volume_is_offline(self):
        offline = os.path.join(self.dir, "unplugged", "session")
        e = library.make_entry("1", "t", offline,
                               [os.path.join(offline, "t_mic.wav")], "", "x")
        kept, removed = library.prune([e])
        self.assertEqual(removed, 0)
        self.assertEqual(kept, [e])

    def test_video_only_take_survives_when_first_segment_is_gone(self):
        # Regression: prune used to look only at "video" (the first segment),
        # so a take whose original capture was deleted but whose restart
        # segments remain was removed from the library.
        first = self._missing("t_screen.mkv")
        restart = self._touch("t_screen-restart-120000.mkv")
        e = library.make_entry("1", "t", self.dir, [], first, "x",
                               video_segments=[first, restart])
        kept, removed = library.prune([e])
        self.assertEqual(removed, 0)
        self.assertEqual(kept[0]["video"], restart)
        self.assertEqual(kept[0]["video_segments"], [restart])

    def test_missing_segments_are_dropped(self):
        first = self._touch("t_screen.mkv")
        gone = self._missing("t_screen-restart-120000.mkv")
        e = library.make_entry("1", "t", self.dir, [], first, "x",
                               video_segments=[first, gone])
        kept, _ = library.prune([e])
        self.assertEqual(kept[0]["video"], first)
        self.assertEqual(kept[0]["video_segments"], [first])

    def test_legacy_entry_without_segments_key_is_unchanged_in_shape(self):
        v = self._touch("t_screen.mkv")
        e = {"id": "1", "name": "t", "out_dir": self.dir, "audio": [],
             "video": v, "created": "x"}
        kept, _ = library.prune([e])
        self.assertEqual(kept, [e])


class OrderAndExportTests(unittest.TestCase):
    def test_order_video_segments_original_first_then_by_stamp(self):
        vids = ["/r/t_screen-restart-130000.mkv",
                "/r/t_screen-restart-120500.mkv",
                "/r/t_screen.mkv"]
        self.assertEqual(library.order_video_segments(vids),
                         ["/r/t_screen.mkv",
                          "/r/t_screen-restart-120500.mkv",
                          "/r/t_screen-restart-130000.mkv"])

    def test_is_export_file(self):
        self.assertTrue(library._is_export_file("SRR_merged_20260101.mkv"))
        self.assertTrue(library._is_export_file("audio-mixed.wav"))
        self.assertTrue(library._is_export_file("combined.mp4"))
        self.assertFalse(library._is_export_file("remixed.wav"))
        self.assertFalse(library._is_export_file("uncombined_take.wav"))


class ScanCreatedTests(unittest.TestCase):
    """The Recorded column must agree with the automatic name, which is
    the moment the take started - not when its folder was last written."""

    def test_stamped_folder_uses_its_start_time(self):
        with tempfile.TemporaryDirectory() as root:
            sub = os.path.join(root, "SRR_2026-09-23_17-44-02")
            os.makedirs(sub)
            with open(os.path.join(sub, "SRR_2026-09-23_17-44-02_mic.wav"),
                      "wb") as fh:
                fh.write(b"x")
            found = library.scan_folder(root)
            self.assertEqual(found[0]["created"], "2026-09-23 17:44:02")

    def test_other_folders_fall_back_to_modified_time(self):
        with tempfile.TemporaryDirectory() as root:
            sub = os.path.join(root, "Weekly standup")
            os.makedirs(sub)
            with open(os.path.join(sub, "mic.wav"), "wb") as fh:
                fh.write(b"x")
            found = library.scan_folder(root)
            self.assertRegex(found[0]["created"],
                             r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


if __name__ == "__main__":
    unittest.main()
