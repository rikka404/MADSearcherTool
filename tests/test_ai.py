import io
import base64
import json
import math
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
from mad_worker.ai import OpenAIClient
from mad_worker.errors import UserError
from mad_worker.images import CUTOUT_IMAGE_POLICY


class Response(io.BytesIO):
    def __init__(self, value):
        super().__init__(json.dumps(value).encode())


class AiTests(unittest.TestCase):
    def test_responses_images_and_schema_wire_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frame.jpg"
            Image.new("RGB", (1920, 1080), "navy").save(path)
            original = path.read_bytes()
            schema = {"type": "object", "properties": {"found": {"type": "boolean"}}, "required": ["found"], "additionalProperties": False}
            response = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"found":true}'}]}]}
            with patch("mad_worker.ai.urllib.request.urlopen", return_value=Response(response)) as opened:
                result = OpenAIClient({"api_key": "test-secret"}).vision_json("Find target", [path], schema)
            self.assertEqual(result, {"found": True})
            request = opened.call_args.args[0]
            body = json.loads(request.data)
            self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
            self.assertEqual(body["input"][0]["content"][1]["type"], "input_image")
            self.assertEqual(body["input"][0]["content"][1]["detail"], "auto")
            self.assertTrue(body["input"][0]["content"][1]["image_url"].startswith("data:image/jpeg;base64,"))
            payload = body["input"][0]["content"][1]["image_url"].split(",", 1)[1]
            with Image.open(io.BytesIO(base64.b64decode(payload))) as sent:
                self.assertEqual(sent.size, (512, 288))
            self.assertEqual(path.read_bytes(), original)
            self.assertTrue(body["text"]["format"]["strict"])
            self.assertFalse(body["store"])

    def test_cutout_uploads_preserve_both_image_dimensions(self):
        # Deferred wire-contract regression: inspect actual encoded request images, not a fake preparer.
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ("first.png", "reference.png")]
            sizes = [(1920, 1080), (800, 1200)]
            for path, size in zip(paths, sizes):
                Image.new("RGB", size, "navy").save(path)
            originals = [path.read_bytes() for path in paths]
            response = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"found":true}'}]}]}
            with patch("mad_worker.ai.urllib.request.urlopen", return_value=Response(response)) as opened:
                OpenAIClient({"api_key": "fixture"}, image_policy=CUTOUT_IMAGE_POLICY).vision_json("Find target", paths, {"type": "object"})
            parts = json.loads(opened.call_args.args[0].data)["input"][0]["content"][1:]
            self.assertEqual(len(parts), 2)
            for part, size in zip(parts, sizes):
                self.assertEqual(part["detail"], "high")
                with Image.open(io.BytesIO(base64.b64decode(part["image_url"].split(",", 1)[1]))) as sent:
                    self.assertEqual(sent.size, size)
            self.assertEqual([path.read_bytes() for path in paths], originals)

    def test_embeddings_order_and_normalization(self):
        response = {"data": [{"index": 1, "embedding": [0, 4]}, {"index": 0, "embedding": [3, 4]}]}
        with patch("mad_worker.ai.urllib.request.urlopen", return_value=Response(response)):
            vectors = OpenAIClient({"api_key": "fixture"}).embed(["电车", "駅"])
        self.assertEqual(vectors, [[0.6, 0.8], [0.0, 1.0]])

    def test_invalid_embedding_cannot_enter_index(self):
        for vector in ([0, 0], [math.nan], [True], ["0.3"]):
            with self.subTest(vector=vector), patch("mad_worker.ai.urllib.request.urlopen", return_value=Response({"data": [{"index": 0, "embedding": vector}]})), self.assertRaises(UserError):
                OpenAIClient({"api_key": "fixture"}).embed(["台词"])

    def test_non_object_api_response_has_user_error(self):
        with patch("mad_worker.ai.urllib.request.urlopen", return_value=Response([])), self.assertRaises(UserError) as raised:
            OpenAIClient({"api_key": "fixture"}).embed(["台词"])
        self.assertEqual(raised.exception.code, "api")

    def test_http_429_retries_and_hides_secret(self):
        error = urllib.error.HTTPError("https://api.openai.com", 429, "fixture-secret", {}, None)
        with patch("mad_worker.ai.urllib.request.urlopen", side_effect=error) as opened, patch("mad_worker.ai.time.sleep"), self.assertRaises(UserError) as raised:
            OpenAIClient({"api_key": "fixture-secret"}).embed(["台词"])
        self.assertEqual(opened.call_count, 3)
        self.assertNotIn("fixture-secret", str(raised.exception))
        self.assertEqual(raised.exception.code, "api")

    def test_unauthorized_not_retried_and_empty_key_blocked(self):
        with self.assertRaises(UserError):
            OpenAIClient({})
        error = urllib.error.HTTPError("https://api.openai.com", 401, "secret", {}, None)
        with patch("mad_worker.ai.urllib.request.urlopen", side_effect=error) as opened, self.assertRaises(UserError):
            OpenAIClient({"api_key": "fixture"}).embed(["台词"])
        self.assertEqual(opened.call_count, 1)


if __name__ == "__main__":
    unittest.main()
