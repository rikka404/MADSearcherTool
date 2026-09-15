"""Deferred regression sources. Do not execute until functional testing is authorized."""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
from mad_worker import dialogue, dialogue_search
from mad_worker.context import Context
from mad_worker.errors import UserError
from mad_worker.search import handle
from mad_worker.store import Store, SCHEMA
from mad_worker.subtitles import Cue


class DialogueUnitsTests(unittest.TestCase):
    def test_effective_cue_limit_counts_after_deduplication(self):
        records, _ = dialogue.make_units([Cue(1, 2, "同一行")] * (dialogue.MAX_CUES + 1))
        self.assertEqual(len(records), 1)
        with self.assertRaisesRegex(UserError, "有效字幕"):
            dialogue.make_units([Cue(i, i + 1, "不同行") for i in range(dialogue.MAX_CUES + 1)])

    def test_bilingual_rows_and_short_contexts_preserve_source_times(self):
        cues = [Cue(1, 2, "不要离开", "zh"), Cue(1, 2, "行かないで", "ja"),
                Cue(2.2, 3, "听我说完", "zh"), Cue(20, 21, "再见", "zh")]
        records, units = dialogue.make_units(cues)
        self.assertEqual(len(records), 4)
        contexts = [u for u in units if u["kind"] == "context"]
        self.assertEqual([(u["start"], u["end"], u["text"]) for u in contexts], [(1, 3, "不要离开 听我说完")])

    def test_wrapped_bilingual_cue_keeps_legacy_text_but_splits_units(self):
        cue = Cue(1, 3, "别走 行かないで", "Default", ("别走", "行かないで"))
        records, units = dialogue.make_units([cue])
        self.assertEqual({r["text"] for r in records}, {"别走", "行かないで"})
        self.assertTrue(all(u["start"] == 1 and u["end"] == 3 for u in units))
        again, _ = dialogue.make_units([Cue(**r) for r in records])
        self.assertEqual(records, again)

    def test_semantic_candidate_survives_zero_lexical_overlap(self):
        unit = {"id": 1, "start": 1, "end": 2, "text": "你不管什么事 都只想着自己啊", "kind": "single", "cue_ids": [0]}
        query = "你这个人满脑子都想的是自己呢"
        selected = dialogue_search.candidates([unit], query, {1: 0.8}, 60)
        self.assertEqual(selected[0]["dialogue_detail"]["lexical"], 0)
        self.assertGreater(selected[0]["score"], 0)

    def test_exact_quote_beats_high_semantic_candidate(self):
        units = [{"id": 1, "start": 1, "end": 2, "text": "你只想着自己", "kind": "single", "cue_ids": [0]},
                 {"id": 2, "start": 8, "end": 9, "text": "毫不相关的句子", "kind": "single", "cue_ids": [1]}]
        selected = dialogue_search.candidates(units, "你只想着自己", {1: 0.5, 2: 0.99}, 60)
        self.assertEqual(selected[0]["id"], 1)

    def test_same_time_translations_and_context_share_one_hit(self):
        units = [{"id": 1, "video_id": "v", "start": 1, "end": 2, "text": "你只想着自己", "kind": "single", "cue_ids": [0]},
                 {"id": 2, "video_id": "v", "start": 1, "end": 2, "text": "自分のことばかり", "kind": "single", "cue_ids": [1]},
                 {"id": 3, "video_id": "v", "start": 1, "end": 4, "text": "你只想着自己 听我说完", "kind": "context", "cue_ids": [0, 2]}]
        selected = dialogue_search.candidates(units, "你只想着自己", {1: 0.9, 2: 0.9, 3: 0.9}, 60)
        hits = dialogue_search.as_hits(selected, {"v": {"path": "source.mp4", "name": "source", "duration": 10}}, {"v": {}}, [], {})
        self.assertEqual(len(hits), 1)
        self.assertEqual((hits[0]["start"], hits[0]["end"]), (1, 2))
        self.assertEqual(len(hits[0]["dialogue_matches"]), 3)


class DialogueStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ctx = Context(str(self.root / "workspace"))
        self.source = self.root / "video.mp4"
        self.source.write_bytes(b"not-decoded-by-subtitle-backfill")
        self.subtitle = self.root / "subtitle.srt"
        self.subtitle.write_text("1\n00:00:01,000 --> 00:00:03,000\n你不管什么事 都只想着自己啊\n", encoding="utf-8")
        with Store(self.ctx.workspace) as store:
            self.group = store.save_group("group", "")
            size, mtime = dialogue.fingerprint(self.source)
            store.add_videos(self.group["id"], [{"id": "video", "path": str(self.source), "path_key": str(self.source),
                "name": "video.mp4", "duration": 10, "width": 64, "height": 48, "fps": 24, "source_size": size, "source_mtime": mtime}])

    def backfill(self, semantic=False):
        return handle("index.dialogue", {"video_id": "video", "subtitle_path": str(self.subtitle), "semantic": semantic}, self.ctx)

    def test_ass_manifest_requires_cleanup_version_without_invalidating_srt(self):
        with Store(self.ctx.workspace) as store:
            video = store.video("video")
        srt = dialogue.source_config(video, self.subtitle)
        self.assertTrue(dialogue.healthy(video, srt))
        self.assertNotIn("subtitle_cleanup_version", srt)
        for extension in (".ass", ".ssa"):
            subtitle = self.root / ("subtitle" + extension)
            subtitle.write_text("[Events]\n", encoding="utf-8")
            config = dialogue.source_config(video, subtitle)
            self.assertTrue(dialogue.healthy(video, config))
            del config["subtitle_cleanup_version"]
            self.assertFalse(dialogue.healthy(video, config))

    def test_backfill_does_not_decode_and_survives_character_background_change(self):
        with patch.object(self.ctx.media, "probe", side_effect=AssertionError("must not probe media")), \
             patch("mad_worker.search.OpenAIClient", side_effect=AssertionError("local backfill must not call AI")):
            self.backfill()
        handle("group.update", {"group_id": self.group["id"], "name": "group", "description": "changed"}, self.ctx)
        for mode in ("dialogue", "combined"):
            hits = handle("search.run", {"group_id": self.group["id"], "query": "你不管什么事都只想着自己啊", "mode": mode}, self.ctx)["results"]
            self.assertEqual((hits[0]["start"], hits[0]["end"]), (1, 3))
            self.assertEqual(hits[0]["character_matches"], [])
        with Store(self.ctx.workspace) as store:
            self.assertEqual(store.segments(self.group["id"]), [])

    def test_database_vectors_reused_and_failed_update_preserves_previous_rows(self):
        class Client:
            embedding_model = "fixture"
            def embed(self, texts):
                return [[1, 0] for _ in texts]
        self.ctx.settings.update(api_key="fixture", embedding_model="fixture")
        with patch("mad_worker.search.OpenAIClient", return_value=Client()):
            self.backfill(semantic=True)
        with patch.object(Client, "embed", side_effect=AssertionError("already persisted")), \
             patch("mad_worker.search.OpenAIClient", return_value=Client()):
            self.backfill(semantic=True)
        self.subtitle.write_text("1\n00:00:04,000 --> 00:00:05,000\n新的台词\n", encoding="utf-8")
        with patch.object(Client, "embed", side_effect=UserError("unavailable")), \
             patch("mad_worker.search.OpenAIClient", return_value=Client()), self.assertRaises(UserError):
            self.backfill(semantic=True)
        with Store(self.ctx.workspace) as store:
            self.assertEqual(store.subtitle_cues("video")[0]["start"], 1)

    def test_same_query_vector_shared_by_combined_search(self):
        calls = []
        class Client:
            embedding_model = "fixture"
            def embed(self, texts):
                calls.append(list(texts))
                return [[1, 0] for _ in texts]
        self.ctx.settings.update(api_key="fixture", embedding_model="fixture")
        with patch("mad_worker.search.OpenAIClient", return_value=Client()):
            self.backfill(semantic=True)
            with Store(self.ctx.workspace) as store:
                video = store.video("video")
                store.replace_index("video", video, dialogue.fingerprint(self.source), {"context": "", "visual": False}, "",
                    [{"start": 0, "end": 5, "subtitle": "", "caption": "夜晚的站台", "thumbnail": "",
                      "embedding": [1, 0], "embedding_model": "fixture"}])
            calls.clear()
            result = handle("search.run", {"group_id": self.group["id"], "query": "你这个人满脑子都想的是自己呢", "semantic": True}, self.ctx)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0]), 1)
        self.assertTrue(result["results"])
        self.assertIn("scene", result["results"][0]["ranking_details"])

    def test_v2_migration_preserves_existing_rows_and_creates_backup(self):
        directory = self.root / "v2"
        directory.mkdir()
        with sqlite3.connect(str(directory / "library.sqlite3")) as db:
            db.executescript(SCHEMA + "ALTER TABLE segments ADD COLUMN shot_id TEXT NOT NULL DEFAULT '';"
                "ALTER TABLE segments ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'; PRAGMA user_version=2;")
            db.execute("INSERT INTO groups VALUES ('kept','kept','original',0)")
        with Store(directory) as store:
            self.assertEqual(store.group("kept")["description"], "original")
            self.assertEqual(store.connection.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(store.subtitle_units("kept"), [])
        backups = list((directory / "backups").glob("library_before_v3_*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(str(backups[0])) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
