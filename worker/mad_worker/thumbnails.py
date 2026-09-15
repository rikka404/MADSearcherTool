"""Durable index thumbnails, copied byte-for-byte before committing DB references."""
import hashlib
import io
import os
import uuid
from pathlib import Path

from PIL import Image

from .errors import UserError
from .owned_paths import checked, fingerprint

MAX_BYTES = 20 * 1024 * 1024


def promote(workspace, source):
    source = Path(source)
    checked(source.parent, source)
    before = fingerprint(source)
    if not 0 < before[0] <= MAX_BYTES:
        raise UserError("索引缩略图为空或超过20MiB，请保留原缓存并检查图片。", "storage")
    with source.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) != before[0] or fingerprint(source) != before:
        raise UserError("缩略图在读取时发生变化，请重试。", "storage")
    try:
        with Image.open(io.BytesIO(raw)) as picture:
            extension = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get(picture.format)
            if not extension or picture.width * picture.height > 25_000_000 or getattr(picture, "n_frames", 1) != 1:
                raise ValueError()
            picture.verify()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise UserError("索引缩略图损坏或格式不支持；未删除原缓存。", "storage")
    directory = checked(workspace, workspace / "thumbnails")
    directory.mkdir(parents=True, exist_ok=True)
    target = checked(directory, directory / (hashlib.sha256(raw).hexdigest() + extension))
    if target.is_file() and target.stat().st_size == len(raw) and target.read_bytes() == raw:
        return str(target)
    temporary = checked(directory, directory / (uuid.uuid4().hex + ".tmp"))
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        checked(directory, target)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return str(target)
