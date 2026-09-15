"""Purpose-specific upload policies; source files and local SAM pixels stay intact."""
import base64
import io
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .errors import UserError
from .profiling import measure

IMAGE_POLICY = "long_edge_512_jpeg85_exif_gray_v1"
MAX_EDGE = 512
MIB = 1024 * 1024


@dataclass(frozen=True)
class ImagePolicy:
    name: str
    max_edge: int | None
    jpeg_quality: int
    max_source_bytes: int
    max_encoded_bytes: int
    detail: str


# Keep the index policy identifier and encoded image settings unchanged for checkpoints.
INDEX_IMAGE_POLICY = ImagePolicy(IMAGE_POLICY, MAX_EDGE, 85, 20 * MIB, MIB, "auto")
CUTOUT_IMAGE_POLICY = ImagePolicy("original_dimensions_jpeg95_exif_gray_v1", None, 95, 100 * MIB, 32 * MIB, "high")


class ImagePreparer:
    def __init__(self, profile=None, *, policy=INDEX_IMAGE_POLICY):
        self.profile = profile
        self.policy = policy
        self.cache = OrderedDict()
        self.cache_bytes = 0

    def prepare(self, value):
        with measure(self.profile, "image.prepare"):
            path = Path(value).expanduser().resolve()
            try:
                stat = path.stat()
                if not path.is_file() or stat.st_size > self.policy.max_source_bytes:
                    raise UserError(f"视觉分析图像不存在或超过{self.policy.max_source_bytes // MIB}MiB，请检查文件。")
                key = (self.policy, str(path), stat.st_size, stat.st_mtime_ns)
                if key in self.cache:
                    item = self.cache.pop(key)
                    self.cache[key] = item
                    if self.profile:
                        self.profile.count("image_cache_hits")
                    self._record(item, True)
                    return item["data_url"]
                with Image.open(path) as original:
                    if original.format not in ("JPEG", "PNG", "WEBP") or getattr(original, "n_frames", 1) != 1:
                        raise UserError("视觉分析图像必须为静态JPEG、PNG或WebP。")
                    if original.width * original.height > 25_000_000:
                        raise UserError("视觉分析图像最多2500万像素，请先缩小。")
                    original_size = original.size
                    image = ImageOps.exif_transpose(original)
                    if self.policy.max_edge is not None:
                        image.thumbnail((self.policy.max_edge, self.policy.max_edge), Image.Resampling.LANCZOS)
                    rgba = image.convert("RGBA")
                    background = Image.new("RGBA", rgba.size, (127, 127, 127, 255))
                    rgb = Image.alpha_composite(background, rgba).convert("RGB")
                    output = io.BytesIO()
                    rgb.save(output, format="JPEG", quality=self.policy.jpeg_quality, optimize=True)
                    encoded = output.getvalue()
                if self.policy.max_edge is not None and max(rgb.size) > self.policy.max_edge:
                    raise UserError("上传图像未满足当前策略的尺寸限制。")
                if len(encoded) > self.policy.max_encoded_bytes:
                    raise UserError(f"上传图像编码后超过{self.policy.max_encoded_bytes // MIB}MiB，请使用体积更小的图片；程序不会因此自动降低分辨率。")
                item = {"source_width": original_size[0], "source_height": original_size[1],
                        "width": rgb.width, "height": rgb.height, "source_bytes": stat.st_size,
                        "encoded_bytes": len(encoded),
                        "data_url": "data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii")}
                size = len(item["data_url"])
                if size <= 64 * MIB:
                    self.cache[key] = item
                    self.cache_bytes += size
                    while len(self.cache) > 128 or self.cache_bytes > 64 * MIB:
                        _, evicted = self.cache.popitem(last=False)
                        self.cache_bytes -= len(evicted["data_url"])
                self._record(item, False)
                return item["data_url"]
            except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
                raise UserError("无法读取或缩放视觉图像，请检查文件并转换为标准JPEG/PNG/WebP。")

    def _record(self, item, cached):
        if self.profile:
            self.profile.event("image_prepared", cached=cached, policy=self.policy.name, detail=self.policy.detail,
                               **{k: v for k, v in item.items() if k != "data_url"})
