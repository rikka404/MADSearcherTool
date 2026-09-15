"""Deferred storage contracts. These tests do not run during the current delivery."""
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
from mad_worker import storage
from mad_worker.activity import workspace_activity
from mad_worker.context import Context
from mad_worker.errors import UserError
from mad_worker.store import Store
from mad_worker.thumbnails import promote


class StorageTests(unittest.TestCase):
    def setUp(self):
        directory = ROOT / "artifacts" / "storage-tests"
        directory.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=directory)
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name).resolve()
        self.ctx = Context(str(self.project / "workspace"))
        # Never let a maintenance test include the actual installation package cache.
        override = patch.object(storage, "PROJECT", self.project)
        override.start()
        self.addCleanup(override.stop)
        self.workspace = self.ctx.workspace

    def file(self, relative, data=b"cached"):
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def picture(self, relative, color="red"):
        path = self.file(relative)
        Image.new("RGB", (16, 12), color).save(path, format="PNG")
        return path

    def library(self, thumbnail, source=None):
        source = source or self.file("source.mp4", b"source")
        with Store(self.workspace) as store:
            db = store.connection
            with db:
                db.execute("INSERT INTO groups VALUES ('group','动画','背景',0)")
                db.execute("INSERT INTO videos (id,group_id,path,path_key,name,duration,width,height,fps,source_size,source_mtime,status) "
                           "VALUES ('video','group',?,?,'第1集',1,16,12,24,6,0,'indexed')", (str(source), str(source)))
                db.execute("INSERT INTO segments (video_id,start,end,subtitle,caption,thumbnail,embedding,embedding_model) "
                           "VALUES ('video',0,1,'台词','描述',?,'[1,0]','model')", (str(thumbnail),))
        return source

    def rows(self):
        with sqlite3.connect(self.workspace / "library.sqlite3") as connection:
            return connection.execute("SELECT id,thumbnail,subtitle,caption,embedding FROM segments ORDER BY id").fetchall()

    def clean(self, **extra):
        plan = storage.scan(extra, self.ctx)
        return storage.clean(dict(extra, plan_token=plan["plan_token"]), self.ctx)

    def test_migrate_old_image_preserves_index_and_reclaims_caches(self):
        old = self.picture("workspace/cache/index/video/old/thumbnail.jpg")
        raw = old.read_bytes()
        self.library(old)
        before = self.rows()
        paths = [self.file(name) for name in ["workspace/cache/query_vectors/a.json",
                 "workspace/cache/dialogue_vectors/a.json", "workspace/logs/index/old.jsonl",
                 "workspace/backups/library_before_v3_old.sqlite3", "artifacts/wheels/install.whl",
                 "models/pip-cache/download"]]
        result = self.clean()
        after = self.rows()
        self.assertEqual(after[0][0], before[0][0])
        self.assertEqual(after[0][2:], before[0][2:])
        target = Path(after[0][1])
        self.assertEqual(target.parent, self.workspace / "thumbnails")
        self.assertEqual(target.read_bytes(), raw)
        self.assertFalse(old.exists())
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertEqual(result["migrated_thumbnails"], 1)
        self.assertEqual(result["failed_files"], 0)
        # Cleaning again must preserve both the DB reference and its exact pixels.
        self.clean()
        self.assertEqual(target.read_bytes(), raw)

    def test_new_store_index_owns_thumbnail_without_changing_cache_record(self):
        sample = self.picture("workspace/cache/samples/frame.png")
        self.library(sample)
        segment = {"start": 0, "end": 1, "subtitle": "台词", "caption": "描述", "thumbnail": str(sample)}
        with Store(self.workspace) as store:
            video = store.video("video")
            store.replace_index("video", video, (6, 0), {"context": "背景", "visual": False}, "", [segment])
        self.assertEqual(segment["thumbnail"], str(sample))
        target = Path(self.rows()[0][1])
        self.assertNotEqual(target, sample)
        self.assertEqual(target.read_bytes(), sample.read_bytes())

    def test_results_inputs_models_and_settings_are_protected(self):
        source = self.file("workspace/cache/selected.mp4", b"source")
        thumbnail = self.picture("workspace/cache/samples/frame.png")
        self.library(thumbnail, source)
        keep = [self.file(name) for name in ["workspace/exports/cutout/task/rgba/000000.png",
            "workspace/cache/failed/INCOMPLETE.txt", "workspace/cache/failed/work/000000.jpg",
            "workspace/cache/complete/manifest.json", "workspace/cache/complete/notes.log",
            "workspace/cache/subworkspace/library.sqlite3", "workspace/cache/subworkspace/cache/a.json",
            "workspace/cache/clip_0.00_1.00_abcdef.mp4", "workspace/models/whisper/model.bin",
            "workspace/settings.json", "workspace/characters/references/card.png",
            "models/sam.pt", "workspace/cache/current-mask.png"]]
        self.clean(protected_paths=[str(keep[-1])])
        self.assertTrue(source.exists())
        self.assertTrue(all(path.exists() for path in keep))

    def test_stale_scan_cannot_delete_or_migrate(self):
        old = self.picture("workspace/cache/samples/frame.png")
        self.library(old)
        plan = storage.scan({}, self.ctx)
        self.file("workspace/cache/new.json")
        with self.assertRaisesRegex(UserError, "变化"):
            storage.clean({"plan_token": plan["plan_token"]}, self.ctx)
        self.assertEqual(self.rows()[0][1], str(old))
        self.assertTrue(old.exists())

    def test_failed_image_copy_stops_before_any_deletion(self):
        old = self.picture("workspace/cache/samples/frame.png")
        self.library(old)
        cache = self.file("workspace/cache/query_vectors/value.json")
        with patch.object(storage, "promote", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.clean()
        self.assertEqual(self.rows()[0][1], str(old))
        self.assertTrue(old.exists())
        self.assertTrue(cache.exists())

    def test_missing_thumbnail_is_reported_without_destroying_index(self):
        missing = self.workspace / "cache" / "missing.png"
        self.library(missing)
        cache = self.file("workspace/cache/unrelated.json")
        plan = storage.scan({}, self.ctx)
        self.assertEqual(plan["missing_thumbnail_count"], 1)
        self.clean()
        self.assertEqual(self.rows()[0][1], str(missing))
        self.assertFalse(cache.exists())

    def test_corrupt_database_fails_closed(self):
        self.file("workspace/library.sqlite3", b"not a database")
        cache = self.file("workspace/cache/keep.json")
        with self.assertRaises(UserError):
            storage.scan({}, self.ctx)
        self.assertTrue(cache.exists())

    def test_orphan_thumbnail_cleanup_preserves_referenced_and_unknown_images(self):
        source = self.picture("workspace/cache/source.png")
        target = Path(promote(self.workspace, source))
        self.library(target)
        orphan_source = self.picture("other.png", "blue")
        orphan = Path(promote(self.workspace, orphan_source))
        unknown = self.picture("workspace/thumbnails/user-picture.png", "green")
        self.clean()
        self.assertTrue(target.exists())
        self.assertFalse(orphan.exists())
        self.assertTrue(unknown.exists())

    def test_links_are_not_followed(self):
        outside = self.file("outside/important.json")
        cache = self.workspace / "cache"
        cache.mkdir()
        try:
            (cache / "linked").symlink_to(outside.parent, target_is_directory=True)
        except OSError:
            self.skipTest("Directory symlink privilege is unavailable")
        self.clean()
        self.assertTrue(outside.exists())

    def test_no_library_scan_and_cleanup_do_not_create_database(self):
        disposable = self.file("workspace/cache/previews/old.mp4")
        self.clean()
        self.assertFalse(disposable.exists())
        self.assertFalse((self.workspace / "library.sqlite3").exists())

    def test_activity_lock_allows_readers_and_rejects_maintenance_overlap(self):
        with workspace_activity(self.workspace), workspace_activity(self.workspace):
            with self.assertRaises(UserError):
                with workspace_activity(self.workspace, exclusive=True):
                    self.fail("Exclusive cleanup must not enter")
        with workspace_activity(self.workspace, exclusive=True):
            with self.assertRaises(UserError):
                with workspace_activity(self.workspace):
                    self.fail("New worker must not enter during cleanup")

    def test_migration_reuses_existing_content_without_deleting_it_as_orphan(self):
        old = self.picture("workspace/cache/samples/frame.png")
        raw = old.read_bytes()
        target = Path(promote(self.workspace, old))
        self.assertEqual(target.stem, hashlib.sha256(raw).hexdigest())
        self.library(old)
        result = self.clean()
        self.assertEqual(result["thumbnail_added_bytes"], 0)
        self.assertTrue(target.exists())
        self.assertEqual(self.rows()[0][1], str(target))

    def test_reference_images_inside_cache_are_preserved(self):
        old = self.picture("workspace/cache/samples/frame.png")
        self.library(old)
        reference = self.picture("workspace/cache/my-reference.png")
        with sqlite3.connect(self.workspace / "library.sqlite3") as connection:
            connection.execute("INSERT INTO characters VALUES ('role','group','角色','[]','','','',?,0)", (json.dumps([str(reference)]),))
        self.clean()
        self.assertTrue(reference.exists())


if __name__ == "__main__":
    unittest.main()
