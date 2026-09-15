"""Deferred regression cases for feedback 11; do not run until acceptance resumes."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
from mad_worker.segmentation import options, shot_segments, choose_frames, _valid_detection
from mad_worker.errors import UserError


class SegmentationTests(unittest.TestCase):
    def test_variable_pts_define_boundaries_and_short_shot_is_preserved(self):
        data = {"pts": [0, 0.04, 0.08, 0.50, 0.55, 0.90], "cuts": [0, 3, 4]}
        segments = shot_segments(data, 1.0, 20)
        self.assertEqual([(s["start"], s["end"]) for s in segments], [(0, 0.5), (0.5, 0.55), (0.55, 1.0)])
        self.assertEqual([(s["start_frame"], s["end_frame"]) for s in segments], [(0, 3), (3, 4), (4, 6)])
        self.assertEqual(len({s["shot_id"] for s in segments}), 3)

    def test_long_shot_subdivision_is_contiguous_and_samples_stay_inside(self):
        data = {"pts": [i / 25 for i in range(1250)], "cuts": [0],
                "change": [0.0] * 1250, "light": [128.0] * 1250}
        segments = shot_segments(data, 50, 20)
        self.assertEqual(segments[0]["start"], 0)
        self.assertEqual(segments[-1]["end"], 50)
        self.assertEqual(len({s["shot_id"] for s in segments}), 1)
        for left, right in zip(segments, segments[1:]):
            self.assertEqual(left["end_frame"], right["start_frame"])
            self.assertEqual(left["end"], right["start"])
        for segment in segments:
            samples = choose_frames(segment, data)
            self.assertTrue(1 <= len(samples) <= 6)
            self.assertTrue(all(segment["start_frame"] <= i < segment["end_frame"] for i in samples))

    def test_legacy_explicit_interval_and_new_default(self):
        self.assertEqual(options({})["mode"], "shot")
        self.assertEqual(options({"segment_seconds": 8})["mode"], "fixed")
        self.assertEqual(options({"segmentation": "shot", "segment_seconds": 8})["mode"], "shot")
        for params in ({"scene_sensitivity": []}, {"shot_max_seconds": float("nan")}, {"segmentation": "unknown"}):
            with self.assertRaises(UserError):
                options(params)

    def test_corrupt_detection_cache_is_not_reused(self):
        valid = {"key": "fixture", "pts": [0, 0.04], "change": [0, 1], "light": [128, 128],
                 "cuts": [0], "filtered_flash_cuts": 0}
        self.assertTrue(_valid_detection(valid, "fixture"))
        for changes in ({"pts": [0, 0]}, {"cuts": [0, 2]}, {"change": [0]}, {"key": "other"}, {"light": [128, float("nan")]}):
            self.assertFalse(_valid_detection(valid | changes, "fixture"))


if __name__ == "__main__":
    unittest.main()
