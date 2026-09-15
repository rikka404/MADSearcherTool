import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
from mad_worker.context import Context
from mad_worker.errors import UserError
from mad_worker.search import handle, _transcribe
from mad_worker.store import Store


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="MAD 检索测试 ")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.ctx = Context(str(self.root / "workspace"))
        self.source = self.root / "第1集 中文.mp4"
        self.ctx.media.run(["-f", "lavfi", "-i", "testsrc2=size=64x48:rate=5:duration=8", "-c:v", "libx264", "-pix_fmt", "yuv420p", self.source])
        self.subtitle = self.root / "第1集.srt"
        self.subtitle.write_text("1\n00:00:00,500 --> 00:00:01,500\n丰川祥子坐在电车里\n\n2\n00:00:02,500 --> 00:00:03,500\n电车驶入车站\n\n3\n00:00:06,500 --> 00:00:07,500\nこんにちは友達\n", encoding="utf-8")
        self.group = self.call("group.create", name="动画A", description="蓝发女孩是丰川祥子")
        self.video = self.call("video.add", group_id=self.group["id"], paths=[str(self.source)])["videos"][0]

    def call(self, command, **params):
        return handle(command, params, self.ctx)

    def index(self, **params):
        return self.call("index.run", **({"video_id": self.video["id"], "subtitle_path": str(self.subtitle), "segment_seconds": 2} | params))

    def search(self, query, **params):
        # Keep these historical scene-window contracts explicit; dialogue/default modes
        # have their own deferred cases in test_dialogue.py.
        return self.call("search.run", **({"group_id": self.group["id"], "query": query, "mode": "scene"} | params))

    def test_real_subtitles_thumbnails_temporal_merge_and_group_isolation(self):
        indexed = self.index()
        self.assertEqual(indexed["segment_count"], 3)
        self.assertEqual(indexed["mode"], "subtitle")
        result = self.search("电车")["results"]
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]["start"], result[0]["end"]), (0, 4))
        self.assertEqual(result[0]["match_type"], "字幕关键词")
        self.assertTrue(Path(result[0]["thumbnail"]).is_file())
        self.assertEqual(self.search("こんにちは")["results"][0]["start"], 6)
        self.assertFalse(self.search("海底鲸鱼")["results"])
        other = self.call("group.create", name="动画B", description="")
        self.assertFalse(self.search("电车", group_id=other["id"])["results"])
        self.assertEqual(self.call("group.list")["groups"][0]["segment_count"], 3)

    def test_group_validation_unique_and_cascade_preserve_sources(self):
        self.index()
        for name in ("", "  ", "动画A"):
            with self.assertRaises(UserError):
                self.call("group.create", name=name)
        updated = self.call("group.update", group_id=self.group["id"], name="改名", description=self.group["description"])
        self.assertEqual(updated["video_count"], 1)
        self.call("group.delete", group_id=self.group["id"])
        self.assertTrue(self.source.is_file())
        self.assertTrue(self.subtitle.is_file())
        with Store(self.ctx.workspace) as store:
            self.assertEqual(store.connection.execute("SELECT count(*) FROM segments").fetchone()[0], 0)
            self.assertEqual(store.connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_zero_duration_subtitles_are_indexed_once_at_their_timestamp(self):
        self.subtitle.write_text("1\n00:00:00,000 --> 00:00:00,000\n片头\n\n2\n00:00:02,000 --> 00:00:02,000\n边界人物\n\n3\n00:00:08,000 --> 00:00:08,000\n视频之外\n", encoding="utf-8")
        self.assertEqual(self.index()["segment_count"], 2)
        with Store(self.ctx.workspace) as store:
            segments = store.segments(self.group["id"])
        self.assertEqual([(s["start"], s["end"], s["subtitle"]) for s in segments], [(0, 2, "片头"), (2, 4, "边界人物")])
        result = self.search("边界人物")["results"]
        self.assertEqual([(r["start"], r["end"]) for r in result], [(2, 4)])

    def test_duplicate_import_and_invalid_batch_are_atomic(self):
        result = self.call("video.add", group_id=self.group["id"], paths=[str(self.source), str(self.source)])
        self.assertEqual(len(result["videos"]), 1)
        self.assertEqual(len(result["warnings"]), 2)
        second = self.root / "第二集.mp4"
        second.write_bytes(self.source.read_bytes())
        with self.assertRaises(UserError):
            self.call("video.add", group_id=self.group["id"], paths=[str(second), str(self.root / "missing.mp4")])
        self.assertEqual(len(self.call("video.list", group_id=self.group["id"])["videos"]), 1)

    def test_subtitle_offset_and_changes_are_detected(self):
        self.index(subtitle_offset=2)
        self.assertEqual(self.search("丰川祥子")["results"][0]["start"], 2)
        self.assertEqual(self.call("video.list", group_id=self.group["id"])["videos"][0]["subtitle_offset"], 2)
        self.subtitle.write_text(self.subtitle.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        listing = self.call("video.list", group_id=self.group["id"])
        self.assertEqual(listing["videos"][0]["status"], "changed")
        result = self.search("电车")
        self.assertFalse(result["results"])
        self.assertIn("变化", result["warnings"][0])

    def test_source_and_group_context_change_detected(self):
        self.index()
        self.call("group.update", group_id=self.group["id"], name="动画A", description="改变身份说明")
        self.assertFalse(self.search("电车")["results"])
        self.call("group.update", group_id=self.group["id"], name="动画A", description=self.group["description"])
        self.assertTrue(self.search("电车")["results"])
        stat = self.source.stat()
        os.utime(self.source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        self.assertEqual(self.call("video.list", group_id=self.group["id"])["videos"][0]["status"], "changed")
        self.assertFalse(self.search("电车")["results"])

    def test_failed_reindex_keeps_old_index_and_retry_reuses_caption(self):
        self.index()
        self.ctx.settings["api_key"] = "test-never-sent"
        client = types.SimpleNamespace(vision_model="fixture-vision", embedding_model="fixture-embed")
        calls = []
        def fail_second(images, subtitle, context, characters, **sampling):
            calls.append(subtitle)
            if len(calls) == 2:
                raise UserError("模拟外部服务中断", "network")
            return {"caption": "车站蓝发女孩", "character_matches": []}
        client.analyze_clip = fail_second
        client.embed = lambda texts: [[1.0, 0.0] for _ in texts]
        with patch("mad_worker.search.OpenAIClient", return_value=client), self.assertRaises(UserError):
            self.index(visual=True)
        self.assertEqual(len(self.search("电车")["results"]), 1)
        with Store(self.ctx.workspace) as store:
            self.assertEqual(store.connection.execute("SELECT count(*) FROM segments").fetchone()[0], 3)
        recovered_calls = []
        client.analyze_clip = lambda images, subtitle, context, characters, **sampling: recovered_calls.append(subtitle) or {"caption": "车站蓝发女孩", "character_matches": []}
        with patch("mad_worker.search.OpenAIClient", return_value=client):
            result = self.index(visual=True)
        self.assertEqual(result["segment_count"], 4)
        self.assertEqual(len(recovered_calls), 3, "first successful caption should be reused")
        with Store(self.ctx.workspace) as store:
            self.assertEqual(store.connection.execute("SELECT count(*) FROM segments").fetchone()[0], 4)

    def test_semantic_query_and_model_mismatch_fallback(self):
        self.ctx.settings.update(api_key="fixture", embedding_model="fixture-embed")
        client = types.SimpleNamespace(vision_model="fixture-vision", embedding_model="fixture-embed",
                                      embed=lambda texts: [[1.0, 0.0] for _ in texts])
        with patch("mad_worker.search.OpenAIClient", return_value=client):
            self.index(semantic=True)
            result = self.search("train", semantic=True)
        self.assertTrue(result["results"])
        self.assertEqual(result["results"][0]["match_type"], "语义")
        self.ctx.settings["embedding_model"] = " fixture-embed "
        with patch("mad_worker.search.OpenAIClient", side_effect=AssertionError("cached query should not need a second request")):
            self.assertEqual(self.search("train", semantic=True)["results"][0]["match_type"], "语义")
        self.ctx.settings["embedding_model"] = "different-model"
        with patch("mad_worker.search.OpenAIClient", side_effect=AssertionError("must not invoke mismatched model")):
            result = self.search("电车", semantic=True)
        self.assertTrue(result["results"])
        self.assertIn("匹配", result["warnings"][0])

    def test_filled_key_without_ai_opt_in_never_sends_subtitles(self):
        self.ctx.settings["api_key"] = "configured-but-not-authorized-for-this-index"
        with patch("mad_worker.search.OpenAIClient", side_effect=AssertionError("Local indexing must never initialize an API client")):
            result = self.index(visual=False, semantic=False)
        self.assertEqual(result["mode"], "subtitle")
        with Store(self.ctx.workspace) as store:
            self.assertEqual(store.connection.execute("SELECT count(*) FROM segments WHERE embedding IS NOT NULL").fetchone()[0], 0)
        self.assertTrue(self.search("电车")["results"])

    def test_invalid_operations_stop_before_indexing(self):
        for values in ({"visual": True}, {"segment_seconds": float("nan")}, {"segment_seconds": 0}, {"visual": "false"}):
            with self.subTest(values=values), self.assertRaises(UserError):
                self.index(**values)
        with self.assertRaises(UserError):
            self.call("index.run", video_id=self.video["id"])
        with self.assertRaises(UserError):
            self.search(" ")
        with self.assertRaises(UserError):
            self.search("电车", limit=2.5)

    def test_asr_optional_library_contract_and_no_audio_guard(self):
        with self.assertRaises(UserError):
            _transcribe(self.source, {"duration": 8, "has_audio": False}, {}, self.ctx)
        class WhisperModel:
            def __init__(self, model, **kwargs):
                self.model = model
            def transcribe(self, path, **kwargs):
                return iter([types.SimpleNamespace(start=1, end=2, text="こんにちは")]), types.SimpleNamespace(language="ja")
        with patch.dict(sys.modules, {"faster_whisper": types.SimpleNamespace(WhisperModel=WhisperModel)}):
            cues, warnings = _transcribe(self.source, {"duration": 8, "has_audio": True}, {"language": "ja"}, self.ctx)
        self.assertEqual(cues[0].text, "こんにちは")
        self.assertEqual(cues[0].start, 1)
        self.assertIn("ja", warnings[0])

    def test_ndjson_command_chain_and_cancel_preserves_previous_index(self):
        self.index()
        def request(command, params):
            return json.dumps({"command": command, "params": params, "settings": {}, "workspace": str(self.ctx.workspace)}, ensure_ascii=False) + "\n"
        listing = subprocess.run([sys.executable, "-u", "-m", "mad_worker"],
            input=request("video.list", {"group_id": self.group["id"]}), capture_output=True,
            text=True, encoding="utf-8", cwd=ROOT / "worker", timeout=30)
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertEqual(json.loads(listing.stdout)["data"]["videos"][0]["status"], "indexed")
        # Terminate only this owned worker after its first complete segment, before commit.
        process = subprocess.Popen([sys.executable, "-u", "-m", "mad_worker"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", cwd=ROOT / "worker")
        try:
            process.stdin.write(request("index.run", {"video_id": self.video["id"], "subtitle_path": str(self.subtitle), "segment_seconds": 3}))
            process.stdin.close()
            event = json.loads(process.stdout.readline())
            self.assertEqual(event["type"], "progress")
            process.terminate()
            process.wait(timeout=15)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=15)
            process.stdout.close()
            process.stderr.close()
        self.assertEqual(self.search("电车")["results"][0]["end"], 4)
        finished = subprocess.run([sys.executable, "-u", "-m", "mad_worker"],
            input=request("index.run", {"video_id": self.video["id"], "subtitle_path": str(self.subtitle), "segment_seconds": 3}),
            capture_output=True, text=True, encoding="utf-8", cwd=ROOT / "worker", timeout=30)
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        events = [json.loads(line) for line in finished.stdout.splitlines()]
        self.assertEqual(events[-1]["type"], "result")
        self.assertEqual(events[-1]["data"]["segment_count"], 3)


    def test_shot_results_keep_cuts_and_include_silent_neighbours(self):
        detection = {"key": "fixture-shots", "pts": [i / 5 for i in range(40)],
                     "cuts": [0, 10, 20, 30], "change": [0] * 40, "light": [128] * 40, "filtered_flash_cuts": 0}
        with patch("mad_worker.segmentation.detect", return_value=detection):
            self.index(segmentation="shot")
        hits = self.search("电车")["results"]
        self.assertEqual(len(hits), 2, "adjacent true shots must remain separate")
        self.assertEqual({(h["start"], h["end"]) for h in hits}, {(0, 2), (2, 4)})
        second = next(h for h in hits if h["start"] == 2)
        self.assertEqual((second["context_start"], second["context_end"]), (0, 6))
        with Store(self.ctx.workspace) as store:
            self.assertEqual(len(store.shot_ranges(self.group["id"])[self.video["id"]]), 4)

    def test_unchanged_shots_reuse_analysis_after_maximum_length_changes(self):
        detection = {"key": "fixture-shots", "pts": [i / 5 for i in range(40)],
                     "cuts": [0, 10, 20, 30], "change": [0] * 40, "light": [128] * 40, "filtered_flash_cuts": 0}
        client = types.SimpleNamespace(vision_model="fixture-vision", embedding_model="fixture-embed",
            analyze_clip=lambda *args, **kwargs: {"caption": "车站蓝发女孩", "character_matches": []},
            embed=lambda texts: [[1.0, 0.0] for _ in texts])
        with patch("mad_worker.segmentation.detect", return_value=detection), patch("mad_worker.search.OpenAIClient", return_value=client):
            self.index(segmentation="shot", shot_max_seconds=20, visual=True)
            client.analyze_clip = lambda *args, **kwargs: self.fail("unchanged shot must use its content cache")
            client.embed = lambda *args, **kwargs: self.fail("cached vector must be reused")
            result = self.index(segmentation="shot", shot_max_seconds=10, visual=True)
        self.assertEqual(result["image_estimate"]["pending_visual_requests"], 0)


if __name__ == "__main__":
    unittest.main()
