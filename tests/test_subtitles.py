import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
from mad_worker.errors import UserError
from mad_worker.subtitles import parse_subtitles


class SubtitleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="字幕测试 ")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def write(self, text, suffix=".srt", encoding="utf-8-sig"):
        path = self.root / ("中文 日本語" + suffix)
        path.write_text(text, encoding=encoding)
        return path

    def test_srt_unicode_overlap_offset_and_clipping(self):
        path = self.write("1\n00:00:00,500 --> 00:00:02,500\n<b>丰川祥子</b> &amp; 電車\n\n2\n00:00:01,000 --> 00:00:03,000\nこんにちは\n\n3\n00:00:11,000 --> 00:00:12,000\noutside\n")
        cues, warnings = parse_subtitles(path, offset=-1, duration=10)
        self.assertEqual(len(cues), 2)
        self.assertEqual((cues[0].start, cues[0].end), (0, 1.5))
        self.assertEqual(cues[0].text, "丰川祥子 & 電車")
        self.assertEqual(cues[1].text, "こんにちは")
        self.assertEqual(len(warnings), 1)

    def test_vtt_settings_and_note(self):
        path = self.write("WEBVTT\n\nNOTE ignore this\ncomment\n\nscene-one\n00:01.250 --> 00:02.500 align:start position:10%\n<v Sakiko>電車が来た</v>\n", ".vtt")
        cues, warnings = parse_subtitles(path)
        self.assertEqual(cues[0].start, 1.25)
        self.assertEqual(cues[0].text, "電車が来た")
        self.assertFalse(warnings)

    def test_ass_format_commas_and_override(self):
        path = self.write("[Script Info]\nTitle: sample\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\nDialogue: 0,0:00:01.20,0:00:03.40,Default,,0,0,0,,{\\pos(20,30)}你来了吗，祥子\\NYes, hello!\n", ".ass", "utf-16")
        cues, _ = parse_subtitles(path)
        self.assertEqual(cues[0].text, "你来了吗，祥子 Yes, hello!")
        self.assertEqual(cues[0].end, 3.4)

    def test_gb18030_subtitle(self):
        cues, _ = parse_subtitles(self.write("1\n00:00:01,000 --> 00:00:02,000\n中文电车\n", encoding="gb18030"))
        self.assertEqual(cues[0].text, "中文电车")

    def test_zero_duration_cues_keep_timestamp_and_use_half_open_windows(self):
        for suffix, content in (
            (".srt", "1\n00:00:00,000 --> 00:00:00,000\n开头\n\n2\n00:00:02,000 --> 00:00:02,000\n边界"),
            (".vtt", "WEBVTT\n\n00:00.000 --> 00:00.000\n开头\n\n00:02.000 --> 00:02.000\n边界"),
            (".ass", "[Events]\nDialogue: 0,0:00:00.00,0:00:00.00,Default,,0,0,0,,开头\nDialogue: 0,0:00:02.00,0:00:02.00,Default,,0,0,0,,边界"),
        ):
            with self.subTest(suffix=suffix):
                path = self.write(content, suffix)
                cues, warnings = parse_subtitles(path, duration=4)
                self.assertEqual([(c.start, c.end) for c in cues], [(0, 0), (2, 2)])
                self.assertFalse(warnings)
                self.assertTrue(cues[0].overlaps(0, 2))
                self.assertFalse(cues[1].overlaps(0, 2))
                self.assertTrue(cues[1].overlaps(2, 4))
                shifted, warnings = parse_subtitles(path, offset=-2, duration=4)
                self.assertEqual([(c.start, c.end) for c in shifted], [(0, 0)])
                self.assertTrue(warnings)

    def test_invalid_inputs_have_actionable_errors(self):
        for content in ("broken file", "1\n00:00:03,000 --> 00:00:02,000\n文本", "1\n00:60:00,000 --> 01:01:00,000\n文本", "1\n00:00:NaN --> 00:00:02,000\n文本"):
            with self.subTest(content=content), self.assertRaises(UserError):
                parse_subtitles(self.write(content))
        valid = self.write("1\n00:00:01,000 --> 00:00:02,000\n文本")
        with self.assertRaises(UserError):
            parse_subtitles(valid, offset=30, duration=10)
        with self.assertRaises(UserError):
            parse_subtitles(valid, offset=float("nan"))


if __name__ == "__main__":
    unittest.main()
