"""Frame-selection contracts; execution deferred until the next acceptance round."""
import sys
import unittest
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
from mad_worker.errors import UserError
from mad_worker.timecode import format_timecode, frame_rate, resolve_range


class TimecodeTests(unittest.TestCase):
    def info(self, rate="24", count=240000):
        fps = Fraction(rate)
        return {"fps": float(fps), "fps_rational": rate, "frame_count": count,
                "duration": float(count / fps)}

    def test_single_frame_and_adjacent_ranges_share_exclusive_boundary(self):
        first = resolve_range({"start": "00:00:23", "end": "00:01:00"}, self.info())
        second = resolve_range({"start_frame": 24, "end_frame": 25}, self.info())
        self.assertEqual((first.start_frame, first.end_frame, first.frame_count), (23, 24, 1))
        self.assertEqual(first.end_frame, second.start_frame)

    def test_fractional_rate_uses_non_drop_numbering_and_exact_frame_duration(self):
        selection = resolve_range({"start": "01:00:00", "end": "01:00:01"}, self.info("24000/1001"))
        self.assertEqual((selection.start_frame, selection.end_frame), (1440, 1441))
        self.assertAlmostEqual(selection.start, 60.06)
        self.assertAlmostEqual(selection.end - selection.start, 1001 / 24000)
        self.assertEqual(selection.as_dict()["fps_rational"], "24000/1001")
        self.assertEqual(format_timecode(1800, Fraction(30000, 1001)), "01:00:00")

    def test_legacy_seconds_snap_to_nearest_frame_without_rejecting_last_frame(self):
        selection = resolve_range({"start": "0.9583333333333334", "end": 1}, self.info(count=24))
        self.assertEqual((selection.start_frame, selection.end_frame), (23, 24))
        half = resolve_range({"start": .0625, "end": .125}, self.info())
        self.assertEqual((half.start_frame, half.end_frame), (2, 3))

    def test_minutes_can_exceed_one_hour_and_high_rates_have_three_frame_digits(self):
        selection = resolve_range({"start": "61:02:23", "end": "61:03:00"}, self.info())
        self.assertEqual(selection.start_frame, 87911)
        self.assertEqual(selection.as_dict()["start_timecode"], "61:02:23")
        self.assertEqual(format_timecode(119, Fraction(120)), "00:00:119")

    def test_invalid_or_mixed_ranges_fail_before_decoding(self):
        for params in ({"start": "00:60:00", "end": "01:01:00"},
                       {"start": "00:00:24", "end": "00:01:01"},
                       {"start": "NaN", "end": 2}, {"start": 0, "end": .001},
                       {"start_frame": True, "end_frame": 2},
                       {"start_frame": 0.5, "end_frame": 2},
                       {"start_frame": 0}, {"start_frame": 0, "end_frame": 2, "start": 0},
                       {"start_frame": 239999, "end_frame": 240001},
                       {"start_frame": 0, "end_frame": 2881}):
            with self.subTest(params=params), self.assertRaises(UserError):
                resolve_range(params, self.info())
        with self.assertRaises(UserError):
            resolve_range({"start_frame": 0, "end_frame": 3601}, self.info("120"))

    def test_missing_metadata_is_explicitly_estimated_and_rate_can_fall_back(self):
        info = {"fps": 24, "fps_rational": "0/0", "duration": 1}
        self.assertEqual(frame_rate(info), Fraction(24))
        selection = resolve_range({"start_frame": 23, "end_frame": 24}, info)
        self.assertEqual(selection.total_frames, 24)
        self.assertTrue(selection.total_frames_estimated)


if __name__ == "__main__":
    unittest.main()
