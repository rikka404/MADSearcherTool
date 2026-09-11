import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
from mad_worker.errors import UserError
from mad_worker.media import Media
from mad_worker.validation import time_range


class FoundationTests(unittest.TestCase):
    def test_invalid_times(self):
        for start, end in [(0, 0), (-1, 1), (0, 11), (math.nan, 2), (0, math.inf), (True, 2)]:
            with self.assertRaises(UserError):
                time_range(start, end, 10)

    def test_ndjson_invalid_request(self):
        result = subprocess.run([sys.executable, "-m", "mad_worker"], input='{"command":',
                                text=True, capture_output=True, encoding="utf-8", cwd=ROOT / "worker")
        self.assertEqual(result.returncode, 1)
        event = json.loads(result.stdout)
        self.assertEqual(event["type"], "error")
        self.assertEqual(event["code"], "validation")

    def test_real_video_paths_and_clip(self):
        media = Media()
        with tempfile.TemporaryDirectory(prefix="MAD 中文 空格 ") as directory:
            root = Path(directory)
            source = root / "源素材.mp4"
            media.run(["-f", "lavfi", "-i", "testsrc2=size=128x96:rate=10:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", source])
            info = media.probe(source)
            self.assertAlmostEqual(info["duration"], 2, delta=.1)
            media.extract_frame(source, .4, root / "帧.png")
            frames = media.extract_frames(source, .2, 1.2, 10, root / "frames")
            self.assertEqual(len(frames), 10)
            media.make_preview(source, .2, 1.2, root / "preview.mp4")
            self.assertAlmostEqual(media.probe(root / "preview.mp4")["duration"], 1, delta=.15)
            self.assertTrue(source.exists())


if __name__ == "__main__":
    unittest.main()
