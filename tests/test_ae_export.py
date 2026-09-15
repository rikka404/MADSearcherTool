"""Deferred AE export regressions. Do not run until functional testing is authorized."""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
from mad_worker import ae_export, exporters
from mad_worker.__main__ import handle
from mad_worker.context import Context
from mad_worker.errors import UserError


class AeExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="AE输出 中文 ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task = self.root / "task"
        self.task.mkdir()
        for name, mode in (("rgba", "RGBA"), ("source_frames", "RGB"), ("masks", "L")):
            directory = self.task / name
            directory.mkdir()
            for index in range(2):
                color = (200, 60, 30, 128) if mode == "RGBA" else (200, 60, 30) if mode == "RGB" else 128
                Image.new(mode, (64, 48), color).save(directory / f"{index:06d}.png")
        self.manifest = self.task / "manifest.json"
        self.manifest.write_text(json.dumps({"schema_version": 2, "status": "complete", "width": 64,
            "height": 48, "fps": 23.976, "frame_count": 2, "source": "missing-original.mp4"}), encoding="utf-8")
        self.ctx = Context(str(self.root / "workspace"))

    def reexport(self, mode="matte"):
        return handle("cutout.export_ae", {"manifest_path": str(self.manifest), "ae_mode": mode}, self.ctx)

    def snapshot(self):
        return {str(p.relative_to(self.task)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.task.rglob("*") if p.is_file() and "ae_exports" not in p.parts}

    def test_default_matte_preserves_pixels_and_does_not_run_media_or_vectorizer(self):
        before = self.snapshot()
        with patch.object(self.ctx.media, "probe", side_effect=AssertionError("no original probe")), \
             patch.object(self.ctx.media, "run", side_effect=AssertionError("no decoding")), \
             patch.object(exporters, "build_ae_payload", side_effect=AssertionError("pixel mode has no contours")):
            result = self.reexport()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(result["ae_actual_mode"], "matte")
        self.assertEqual(result["ae_script_version"], ae_export.SCRIPT_VERSION)
        self.assertEqual(set(result["ae_scripts"]), {"rgba", "matte"})
        script = Path(result["ae_script"]).read_text(encoding="utf-8")
        self.assertIn('"source":"../../source_frames/000000.png"', script)
        self.assertIn("AlphaMode.STRAIGHT", script)
        self.assertIn("TrackMatteType.ALPHA", script)
        self.assertIn("BlendingMode.SILHOUETE_ALPHA", script)
        self.assertIn("base.locked = true", script)
        self.assertNotIn("__PAYLOAD__", script)
        report = json.loads(Path(result["ae_report_path"]).read_text(encoding="utf-8"))
        self.assertEqual(report["script_version"], result["ae_script_version"])

    def test_vector_failure_keeps_pixel_scripts_and_diagnostics(self):
        def fail(*args, diagnostics=None, **kwargs):
            diagnostics.update(failure={"frame_index": 1, "vertices": 5000})
            raise UserError("too many vertices", "export_limit")
        with patch.object(exporters, "build_ae_payload", side_effect=fail):
            result = self.reexport("paths")
        self.assertEqual(result["ae_mode"], "paths")
        self.assertEqual(result["ae_actual_mode"], "matte")
        self.assertEqual(set(result["ae_scripts"]), {"rgba", "matte"})
        self.assertTrue(result["warnings"])
        report = json.loads(Path(result["ae_report_path"]).read_text(encoding="utf-8"))
        self.assertEqual(report["vector_diagnostics"]["failure"]["frame_index"], 1)
        self.assertEqual(report["status"], "complete")
        self.assertFalse((Path(result["ae_export_dir"]) / "INCOMPLETE.txt").exists())

    def test_rgba_needs_no_original_source_and_exports_are_independent(self):
        for p in (self.task / "source_frames").glob("*.png"):
            p.unlink()
        (self.task / "source_frames").rmdir()
        first = self.reexport("rgba")
        original_script = Path(first["ae_script"]).read_bytes()
        second = self.reexport("rgba")
        self.assertNotEqual(first["ae_export_dir"], second["ae_export_dir"])
        self.assertEqual(Path(first["ae_script"]).read_bytes(), original_script)
        self.assertEqual(set(second["ae_scripts"]), {"rgba"})

    def test_missing_frame_fails_without_changing_old_outputs(self):
        first = self.reexport()
        before = Path(first["ae_script"]).read_bytes()
        (self.task / "rgba/000001.png").unlink()
        with self.assertRaisesRegex(UserError, "缺帧"):
            self.reexport()
        self.assertEqual(Path(first["ae_script"]).read_bytes(), before)

    def test_incomplete_task_and_invalid_modes_are_rejected(self):
        (self.task / "INCOMPLETE.txt").write_text("unfinished", encoding="utf-8")
        with self.assertRaisesRegex(UserError, "完整"):
            self.reexport()
        with self.assertRaisesRegex(UserError, "导出方式"):
            self.reexport("unknown")
        self.assertFalse((self.task / "ae_exports").exists())

    def test_changed_dimensions_and_missing_alpha_are_rejected(self):
        Image.new("RGBA", (8, 8)).save(self.task / "rgba/000001.png")
        with self.assertRaisesRegex(UserError, "尺寸或像素格式"):
            self.reexport()
        Image.new("RGB", (64, 48)).save(self.task / "rgba/000001.png")
        with self.assertRaisesRegex(UserError, "尺寸或像素格式"):
            self.reexport()

    def test_invalid_manifest_types_and_single_frame_metadata(self):
        data = json.loads(self.manifest.read_text(encoding="utf-8"))
        data["frame_count"] = True
        self.manifest.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(UserError):
            self.reexport()
        data["frame_count"] = 1
        self.manifest.write_text(json.dumps(data), encoding="utf-8")
        for name in ("rgba", "source_frames", "masks"):
            (self.task / name / "000001.png").unlink()
        result = self.reexport()
        script = Path(result["ae_script"]).read_text(encoding="utf-8")
        self.assertIn('"count":1', script)
        self.assertIn("options.sequence = data.count > 1", script)

    def test_contour_limit_reports_failing_frame(self):
        import numpy as np
        alpha = np.zeros((48, 64), dtype=np.uint8)
        alpha[5:40, 5:50] = 255
        Image.fromarray(alpha).save(self.task / "masks/000000.png")
        stats = {}
        with patch.object(exporters, "MAX_VERTICES_PER_CONTOUR", 3), self.assertRaisesRegex(UserError, "第1帧"):
            exporters.build_ae_payload(self.task / "source_frames", [self.task / "masks/000000.png"], 24, 64, 48, diagnostics=stats)
        self.assertEqual(stats["failure"]["frame_index"], 0)
        self.assertEqual(stats["failure"]["vertices"], 4)


if __name__ == "__main__":
    unittest.main()
