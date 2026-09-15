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

    # Regression sources below are deferred along with functional acceptance.
    def ass(self, *events):
        return self.write("[Events]\n" + "\n".join(events), ".ass")

    def test_ass_drawing_state_preserves_visible_text_and_vector_clips(self):
        path = self.ass(
            r"Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\p1}m 0 0 l 5 5\N{\bord2}l 9 9{\p0}保留\N台词",
            r"Dialogue: 0,0:00:04.00,0:00:05.00,Default,,0,0,0,,{\p1}m 0 0{\rDefault}重置后的文字",
            r"Dialogue: 0,0:00:06.00,0:00:07.00,Default,,0,0,0,,{\pos(20,30)\clip(m 0 0 l 5 5)\pbo2\t(0,50,\clip(1,2,3,4)\p1)}裁剪后的文字",
            r"Dialogue: 0,0:00:08.00,0:00:09.00,Default,,0,0,0,,{\p1\p0}同块恢复",
            r"Dialogue: 0,0:00:10.00,0:00:11.00,Default,,0,0,0,,{\p0\p1}m 0 0 l 1 1",
            "Comment: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,karaoke,不恢复模板")
        stats = {}
        cues, warnings = parse_subtitles(path, diagnostics=stats)
        self.assertEqual([c.text for c in cues], ["保留 台词", "重置后的文字", "裁剪后的文字", "同块恢复"])
        self.assertEqual(cues[0].lines, ("保留", "台词"))
        self.assertEqual(stats["raw_events"], 5)
        self.assertEqual(stats["drawing_only_removed"], 1)
        self.assertEqual(stats["ignored_comments"], 1)
        self.assertEqual(stats["effective_cues"], 4)
        self.assertTrue(warnings)
        self.assertNotIn("裁剪后的文字", str(stats))

    def test_ass_fx_layers_merge_but_later_occurrence_is_separate(self):
        path = self.ass(
            r"Dialogue: 0,0:00:01.00,0:00:01.04,OP_CH,,0,0,0,fx,{\bord2}重复歌词",
            r"Dialogue: 1,0:00:01.00,0:00:01.04,OP_CH,,0,0,0,fx,{\bord4}重复歌词",
            "Dialogue: 0,0:00:01.04,0:00:01.08,OP_CH,,0,0,0,fx,重复歌词",
            "Dialogue: 0,0:00:01.08,0:00:01.12,OP_CH,,0,0,0,fx,重复歌词",
            "Dialogue: 0,0:00:10.00,0:00:10.04,OP_CH,,0,0,0,fx,重复歌词",
            "Dialogue: 0,0:00:10.04,0:00:10.08,OP_CH,,0,0,0,fx,重复歌词")
        stats = {}
        cues, _ = parse_subtitles(path, diagnostics=stats)
        self.assertEqual([(c.start, c.end) for c in cues], [(1, 1.12), (10, 10.08)])
        self.assertEqual(stats["duplicates_removed"], 1)
        self.assertEqual(stats["effects_merged"], 3)

    def test_ass_unmarked_frame_runs_merge_without_collapsing_normal_dialogue(self):
        path = self.ass(
            "Dialogue: 0,0:00:00.00,0:00:00.04,Phone,,0,0,0,,妈妈",
            "Dialogue: 0,0:00:00.04,0:00:00.08,Phone,,0,0,0,,妈妈",
            "Dialogue: 0,0:00:00.08,0:00:00.12,Phone,,0,0,0,,妈妈",
            "Dialogue: 0,0:00:01.00,0:00:02.00,Dial,,0,0,0,,妈妈",
            "Dialogue: 0,0:00:02.00,0:00:03.00,Dial,,0,0,0,,妈妈",
            "Dialogue: 0,0:00:03.00,0:00:03.00,Dial,,0,0,0,,妈妈",
            "Dialogue: 0,0:00:03.00,0:00:03.04,Dial,,0,0,0,,妈妈",
            "Dialogue: 0,0:00:03.04,0:00:03.08,Dial,,0,0,0,,妈妈")
        cues, _ = parse_subtitles(path)
        self.assertEqual([(c.start, c.end) for c in cues], [(0, .12), (1, 2), (2, 3), (3, 3), (3, 3.04), (3.04, 3.08)])

    def test_ass_normal_base_layer_deduplicates_after_fx_compaction(self):
        path = self.ass(
            "Dialogue: 0,0:00:00.00,0:00:00.12,OP,,0,0,0,,歌词",
            "Dialogue: 1,0:00:00.00,0:00:00.04,OP,,0,0,0,fx,歌词",
            "Dialogue: 1,0:00:00.04,0:00:00.08,OP,,0,0,0,fx,歌词",
            "Dialogue: 1,0:00:00.08,0:00:00.12,OP,,0,0,0,fx,歌词")
        cues, _ = parse_subtitles(path)
        self.assertEqual([(c.start, c.end) for c in cues], [(0, .12)])

    def test_ass_merge_keeps_style_actor_and_line_boundaries(self):
        path = self.ass(
            "Dialogue: 0,0:00:01.00,0:00:01.04,Dial,A,0,0,0,fx,相同文本",
            "Dialogue: 0,0:00:01.04,0:00:01.08,Dial,B,0,0,0,fx,相同文本",
            "Dialogue: 0,0:00:01.08,0:00:01.12,Other,B,0,0,0,fx,相同文本",
            r"Dialogue: 0,0:00:02.00,0:00:02.04,Dial,,0,0,0,fx,别走\N行かないで",
            "Dialogue: 0,0:00:02.04,0:00:02.08,Dial,,0,0,0,fx,别走 行かないで")
        cues, warnings = parse_subtitles(path)
        self.assertEqual(len(cues), 5)
        self.assertFalse(warnings)

    def test_ass_short_frame_detection_uses_unclipped_duration(self):
        path = self.ass(
            "Dialogue: 0,0:00:00.00,0:00:01.04,Dial,,0,0,0,,重复",
            "Dialogue: 0,0:00:00.01,0:00:01.06,Dial,,0,0,0,,重复",
            "Dialogue: 0,0:00:00.02,0:00:01.08,Dial,,0,0,0,,重复")
        cues, warnings = parse_subtitles(path, offset=-1)
        self.assertEqual(len(cues), 3)
        self.assertFalse(warnings)

    def test_ass_large_render_file_is_compacted_before_unit_limit(self):
        from mad_worker.dialogue import make_units
        # Many display layers are not many subtitle sentences.
        events = [f"Dialogue: {layer},0:00:01.00,0:00:03.00,OP_CH,,0,0,0,fx,重复歌词" for layer in range(20001)]
        events.append("Dialogue: 0,0:21:22.47,0:21:25.71,Dial_CH,,0,0,0,,你不管什么事 都只想着自己啊")
        cues, _ = parse_subtitles(self.ass(*events))
        records, _ = make_units(cues)
        self.assertEqual(len(records), 2)
        self.assertEqual((cues[1].start, cues[1].end, cues[1].text), (1282.47, 1285.71, "你不管什么事 都只想着自己啊"))

    def test_ass_only_drawing_and_comments_cannot_be_indexed(self):
        path = self.ass(
            r"Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\p1}m 0 0 l 5 5",
            "Comment: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,隐藏对白")
        with self.assertRaisesRegex(UserError, "可见文本"):
            parse_subtitles(path)

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
