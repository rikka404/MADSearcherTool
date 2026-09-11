import contextlib
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
from mad_worker import cutout, exporters
from mad_worker.context import Context
from mad_worker.errors import UserError
from mad_worker.media import Media


class CutoutInputTests(unittest.TestCase):
    def test_boxes_reject_nonfinite_inverted_and_outside(self):
        for box in (None, [], [0, 0, 0, 10], [10, 0, 9, 10], [-1, 0, 10, 10],
                    [0, 0, 101, 40], [0, math.nan, 10, 40], [0, 0, True, 40]):
            with self.subTest(box=box), self.assertRaises(UserError):
                cutout.validate_box(box, 100, 80)
        self.assertEqual(cutout.validate_box([0, 0, 100, 80], 100, 80), [0., 0., 100., 80.])

    def test_mask_preserves_alpha_and_rejects_invalid_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            alpha = np.zeros((30, 40), dtype=np.uint8)
            alpha[5:20, 8:30] = 255
            alpha[4, 8:30] = 90
            image = Image.new("RGBA", (40, 30), (200, 30, 10, 255))
            image.putalpha(Image.fromarray(alpha))
            image.save(directory / "valid.png")
            np.testing.assert_array_equal(cutout.load_mask(directory / "valid.png", 40, 30), alpha)
            with self.assertRaisesRegex(UserError, "同尺寸"):
                cutout.load_mask(directory / "valid.png", 41, 30)
            for name, image in [("empty", Image.new("L", (40, 30), 0)),
                                ("full", Image.new("L", (40, 30), 255)),
                                ("colour", Image.new("RGB", (40, 30), (220, 50, 30)))]:
                image.save(directory / f"{name}.png")
                with self.subTest(name=name), self.assertRaises(UserError):
                    cutout.load_mask(directory / f"{name}.png", 40, 30)
            Image.fromarray(alpha).save(directory / "gray.png")
            np.testing.assert_array_equal(cutout.load_mask(directory / "gray.png", 40, 30), alpha)

    def test_block_ranges_cover_every_frame_with_one_overlap(self):
        for count in (1, 2, 31, 32, 33, 63, 64, 3600):
            ranges = list(cutout.block_ranges(count))
            self.assertEqual(ranges[0][0], 0)
            self.assertEqual(ranges[-1][1], count)
            self.assertEqual(set(range(count)), {i for start, end in ranges for i in range(start, end)})
            for (start, end), (next_start, _) in zip(ranges, ranges[1:]):
                self.assertLessEqual(end - start, 32)
                self.assertEqual(next_start, end - 1)

    def test_cloud_localizer_maps_normalized_coordinates_and_rejects_absent(self):
        # Match the shared AI interface without a network request or API key.
        with patch("mad_worker.ai.OpenAIClient") as client:
            client.return_value.vision_json.return_value = {"found": True, "bbox": [100, 200, 500, 900], "reason": "目标"}
            self.assertEqual(cutout.locate_target(Path("frame.png"), "人物", None, 200, 100, {}), [20., 20., 100., 90.])
            client.return_value.vision_json.return_value = {"found": False, "bbox": [0, 0, 0, 0], "reason": "不存在"}
            with self.assertRaisesRegex(UserError, "未能"):
                cutout.locate_target(Path("frame.png"), "人物", None, 200, 100, {})


class ExportTests(unittest.TestCase):
    def test_rgba_preserves_straight_colour_and_partial_alpha(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            Image.new("RGB", (20, 16), (202, 13, 91)).save(directory / "source.png")
            alpha = np.zeros((16, 20), dtype=np.uint8)
            alpha[2:14, 3:17] = 128
            exporters.write_rgba_frame(directory / "source.png", alpha, directory / "rgba.png", directory / "mask.png")
            with Image.open(directory / "rgba.png") as output:
                self.assertEqual(output.mode, "RGBA")
                pixels = np.asarray(output)
            self.assertTrue(np.all(pixels[:, :, :3] == [202, 13, 91]))
            np.testing.assert_array_equal(pixels[:, :, 3], alpha)

    def test_contour_tree_keeps_hole_and_inner_island_and_empty_frames(self):
        alpha = np.zeros((80, 80), dtype=np.uint8)
        alpha[5:75, 5:75] = 255
        alpha[20:60, 20:60] = 0
        alpha[30:50, 30:50] = 255
        contours = exporters.mask_contours(alpha)
        self.assertEqual([entry["depth"] for entry in contours], [0, 1, 2])
        self.assertEqual(exporters.mask_contours(np.zeros_like(alpha)), [])
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            Image.fromarray(alpha).save(directory / "000000.png")
            Image.fromarray(np.zeros_like(alpha)).save(directory / "000001.png")
            payload = exporters.build_ae_payload(directory, [directory / "000000.png", directory / "000001.png"], 24, 80, 80)
            self.assertEqual(payload["slots"], [1, 1, 1])
            self.assertEqual(payload["frames"][1], [])
            script = exporters.export_ae(directory, [directory / "000000.png", directory / "000001.png"], 24, 80, 80, directory / "out.jsx")
            contents = Path(script).read_text(encoding="utf-8")
            self.assertIn("KeyframeInterpolationType.HOLD", contents)
            self.assertIn("MaskMode.SUBTRACT : MaskMode.ADD", contents)
            self.assertIn("[[-4,-4],[-3,-4],[-3,-3]]", contents)
            self.assertIn("FrameBlendingType.NO_FRAME_BLEND", contents)
            self.assertNotIn("eval(", contents)

    def test_single_pixel_is_retained_and_complexity_is_explicit(self):
        alpha = np.zeros((80, 80), dtype=np.uint8)
        alpha[8, 8] = 255
        contours = exporters.mask_contours(alpha)
        self.assertEqual(len(contours), 1)
        self.assertEqual(len(contours[0]["vertices"]), 4)
        alpha[::4, ::4] = 255
        with self.assertRaises(UserError) as error:
            exporters.mask_contours(alpha)
        self.assertEqual(error.exception.code, "export_limit")

    def test_real_ffmpeg_prores_alpha_preview_and_decode(self):
        media = Media()
        with tempfile.TemporaryDirectory(prefix="MAD透明 中文 ") as directory:
            directory = Path(directory)
            rgba_dir = directory / "rgba"
            rgba_dir.mkdir()
            for frame in range(4):
                image = Image.new("RGBA", (64, 48), (180, 30, 90, 0))
                alpha = np.zeros((48, 64), dtype=np.uint8)
                alpha[10:30, 10 + frame:35 + frame] = 255
                image.putalpha(Image.fromarray(alpha))
                image.save(rgba_dir / f"{frame:06d}.png")
            video = exporters.export_prores(media, rgba_dir, 8., 4, directory / "alpha.mov")
            self.assertEqual(media.probe(video)["codec"], "prores")
            self.assertAlmostEqual(media.probe(video)["duration"], .5, places=2)
            # Decode the actual MOV alpha plane, not only a container metadata flag.
            media.run(["-i", video, "-vf", "alphaextract", "-frames:v", "1", "-update", "1", directory / "decoded.png"])
            with Image.open(directory / "decoded.png") as image:
                recovered = np.asarray(image.convert("L"))
            self.assertLessEqual(int(recovered[0, 0]), 1)
            self.assertGreaterEqual(int(recovered[15, 15]), 250)
            preview = exporters.export_preview(media, rgba_dir, 64, 48, 8., 4, directory / "preview.mp4")
            self.assertEqual(media.probe(preview)["codec"], "h264")
            self.assertAlmostEqual(media.probe(preview)["duration"], .5, places=2)


class FakeTensor:
    def __init__(self, values): self.values = values
    def __getitem__(self, index): return FakeTensor(self.values[index])
    def __gt__(self, value): return FakeTensor(self.values > value)
    def detach(self): return self
    def to(self, device): return self
    def numpy(self): return self.values


class FakeTorch:
    inference_mode = staticmethod(contextlib.nullcontext)


class RecordingPredictor:
    def __init__(self, width, height):
        self.width, self.height, self.seeds = width, height, []

    def init_state(self, path, **kwargs):
        assert kwargs == {"offload_video_to_cpu": True, "offload_state_to_cpu": True, "async_loading_frames": False}
        return {"count": len(list(Path(path).glob("*.jpg")))}

    def add_new_mask(self, state, frame_idx, obj_id, mask):
        self.seeds.append(mask.copy())
        state["mask"] = mask.copy()

    def propagate_in_video(self, state):
        for frame in range(state["count"]):
            mask = np.roll(state["mask"], frame, axis=1)
            yield frame, [1], FakeTensor(np.where(mask[None, None], 1., -1.))


class TrackingContractTests(unittest.TestCase):
    def test_chunk_seed_and_output_indices_keep_overlap_once(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            frames = []
            for i in range(7):
                path = directory / f"{i:06d}.png"
                Image.new("RGB", (20, 16), (70, 80, 90)).save(path)
                frames.append(path)
            for name in ("work", "rgba", "masks"):
                (directory / name).mkdir()
            alpha = np.zeros((16, 20), dtype=np.uint8)
            alpha[4:12, 2:5] = 255
            predictor = RecordingPredictor(20, 16)
            ctx = Context(str(directory / "workspace"))
            with patch.object(cutout, "BLOCK_FRAMES", 4):
                missing = cutout._propagate(predictor, FakeTorch(), "cpu", frames, alpha, None,
                                            directory / "work", directory / "rgba", directory / "masks", ctx)
            self.assertEqual(missing, [])
            self.assertEqual(len(predictor.seeds), 2)
            np.testing.assert_array_equal(predictor.seeds[1], np.roll(alpha >= 128, 3, axis=1))
            self.assertEqual(len(list((directory / "rgba").glob("*.png"))), 7)
            with Image.open(directory / "masks" / "000006.png") as image:
                np.testing.assert_array_equal(np.asarray(image), np.roll(alpha, 6, axis=1))
            self.assertEqual(list((directory / "work").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
