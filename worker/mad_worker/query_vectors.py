"""Bounded query/subtitle embedding caches; never persist input text or credentials."""
import hashlib
import json
import math
from pathlib import Path

MAX_ITEMS = 256
MAX_BYTES = 64 * 1024 * 1024


def _vector(value):
    if not isinstance(value, list) or not 0 < len(value) <= 65536:
        return None
    try:
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in value):
            return None
        norm = math.sqrt(sum(v * v for v in value))
    except OverflowError:
        return None
    return [v / norm for v in value] if norm > 0 and math.isfinite(norm) else None


def cached_embeddings(workspace, model, texts, create_client, *, expected_dimensions=(), purpose="query"):
    if purpose not in ("query", "dialogue"):
        raise ValueError("Unknown embedding cache purpose")
    directory = Path(workspace) / "cache" / ("query_vectors" if purpose == "query" else "dialogue_vectors")
    max_items, max_bytes = (MAX_ITEMS, MAX_BYTES) if purpose == "query" else (16384, 256 * 1024 * 1024)
    result, paths, missing = {}, {}, []
    cache_failed = False
    for text in dict.fromkeys(texts):
        key = hashlib.sha256(json.dumps([1, model, text], ensure_ascii=False).encode()).hexdigest()
        path = directory / (key + ".json")
        paths[text] = path
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.stat().st_size <= 2 * 1024 * 1024 else None
            vector = _vector(data.get("vector")) if isinstance(data, dict) and data.get("model") == model and data.get("key") == key else None
        except (OSError, ValueError):
            vector = None
        if vector is None or (expected_dimensions and len(vector) not in expected_dimensions):
            missing.append(text)
        else:
            result[text] = vector
    if missing:
        vectors = create_client().embed(missing)
        if len(vectors) != len(missing) or any(_vector(v) is None for v in vectors):
            from .errors import UserError
            raise UserError("查询向量返回格式无效，请检查模型配置。", "api")
        if expected_dimensions and any(len(v) not in expected_dimensions for v in vectors):
            from .errors import UserError
            raise UserError("查询模型返回的向量维度与现有索引不一致，请确认embedding模型并重建对应索引。", "api")
        result.update(zip(missing, (_vector(v) for v in vectors)))
    try:
        directory.mkdir(parents=True, exist_ok=True)
        for text, vector in result.items():
            path = paths[text]
            if text in missing:
                temporary = path.with_suffix(".tmp")
                temporary.write_text(json.dumps({"model": model, "key": path.stem, "vector": vector}, allow_nan=False), encoding="utf-8")
                temporary.replace(path)
            else:
                path.touch()
        files = sorted(((p.stat().st_mtime_ns, p.stat().st_size, p) for p in directory.glob("*.json")), reverse=True)
        size = 0
        for index, (_, length, path) in enumerate(files):
            size += length
            if index >= max_items or size > max_bytes:
                path.unlink(missing_ok=True)
    except OSError:
        cache_failed = True
    return result, {"hits": len(result) - len(missing), "misses": len(missing), "cache_write_failed": cache_failed}
