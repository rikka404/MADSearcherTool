"""Optional character cards, owned reference assets, and index fingerprints."""
import hashlib
import json
import re
import uuid
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .errors import UserError
from .validation import existing_file

MAX_CHARACTERS = 30
MAX_REFERENCES = 3


def optional_text(value, label, maximum):
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise UserError(f"{label}必须是最多{maximum}字的文本，可留空。")
    return value.strip()


def import_reference(value, workspace):
    source = existing_file(value, "角色参考图", {".jpg", ".jpeg", ".png", ".webp"})
    if source.stat().st_size > 20 * 1024 * 1024:
        raise UserError("每张角色参考图最多20MB。")
    raw = source.read_bytes()
    try:
        import io
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in ("JPEG", "PNG", "WEBP") or image.width * image.height > 25_000_000:
                raise UserError("参考图需为JPEG/PNG/WebP，最多2500万像素。")
            if getattr(image, "n_frames", 1) != 1:
                raise UserError("请使用静态角色参考图。")
            image.verify()
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        raise UserError("无法读取角色参考图，请转换为标准JPEG/PNG/WebP。")
    directory = workspace / "characters" / "references"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (hashlib.sha256(raw).hexdigest() + source.suffix.lower())
    if not target.is_file():
        temporary = directory / (uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_bytes(raw)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return str(target)


def normalize_characters(value, workspace):
    if not isinstance(value, list) or len(value) > MAX_CHARACTERS:
        raise UserError(f"角色列表必须是数组，最多{MAX_CHARACTERS}个角色，可为空。")
    result, seen = [], set()
    for card in value:
        if not isinstance(card, dict):
            raise UserError("每个角色必须是一个资料对象。")
        key = card.get("id") or uuid.uuid4().hex
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{32}", key) or key in seen:
            raise UserError("角色ID无效或重复，请重新打开分组编辑。")
        seen.add(key)
        aliases = card.get("aliases") or []
        if not isinstance(aliases, list) or len(aliases) > 20:
            raise UserError("角色别名需为数组，最多20个，可为空。")
        aliases = list(dict.fromkeys(optional_text(v, "别名", 120) for v in aliases))
        refs = card.get("reference_images") or []
        if not isinstance(refs, list) or len(refs) > MAX_REFERENCES:
            raise UserError(f"每个角色最多{MAX_REFERENCES}张参考图，可不添加。")
        result.append({"id": key, "name": optional_text(card.get("name"), "角色姓名", 120),
                       "aliases": [v for v in aliases if v],
                       "work_info": optional_text(card.get("work_info"), "角色作品资料", 2000),
                       "identity": optional_text(card.get("identity"), "角色身份", 1000),
                       "appearance": optional_text(card.get("appearance"), "角色外观", 2000),
                       "reference_images": list(dict.fromkeys(import_reference(p, workspace) for p in refs))})
    return result


def active_characters(cards):
    return [c for c in cards if any(c.get(key) for key in
            ("name", "aliases", "work_info", "identity", "appearance", "reference_images"))]


def signature(cards):
    """Include asset metadata so edited/missing owned references invalidate visual indexes."""
    values = []
    for card in cards:
        refs = []
        for value in card["reference_images"]:
            path = Path(value)
            try:
                stat = path.stat()
                refs.append([str(path), stat.st_size, stat.st_mtime_ns])
            except OSError:
                refs.append([str(path), "missing"])
        values.append({**card, "reference_images": refs})
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def display_name(card):
    return card["name"] or next(iter(card["aliases"]), "") or "未命名角色 · " + card["id"][:6]
