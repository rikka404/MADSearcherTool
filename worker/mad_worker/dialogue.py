"""Independent subtitle units and text-only indexing; no media or vision processing."""
import hashlib
import json
import math
import re
from pathlib import Path

from .errors import UserError
from .query_vectors import cached_embeddings, _vector
from .subtitles import ASS_CLEANUP_VERSION, Cue, parse_subtitles
from .validation import existing_file, finite_number

VERSION = 1
MAX_CUES = 20000
MAX_TEXT = 1600


def fingerprint(path):
    stat = Path(path).stat()
    return [stat.st_size, stat.st_mtime_ns]


def source_config(video, subtitle_path="", offset=0, origin="file", approximate=False):
    config = {"version": VERSION, "source": [video["source_size"], video["source_mtime"]],
              "subtitle_path": str(subtitle_path), "subtitle_fingerprint": fingerprint(subtitle_path) if subtitle_path else None,
              "subtitle_offset": offset, "origin": origin, "approximate": approximate}
    if Path(subtitle_path).suffix.lower() in (".ass", ".ssa"):
        config["subtitle_cleanup_version"] = ASS_CLEANUP_VERSION
    return config


def healthy(video, config):
    try:
        if (Path(config.get("subtitle_path") or "").suffix.lower() in (".ass", ".ssa")
                and config.get("subtitle_cleanup_version") != ASS_CLEANUP_VERSION):
            return False
        return (fingerprint(video["path"]) == config["source"] and config.get("version") == VERSION
                and (not config.get("subtitle_path") or fingerprint(config["subtitle_path"]) == config["subtitle_fingerprint"]))
    except (OSError, KeyError, TypeError):
        return False


def _script(text):
    if re.search(r"[\u3040-\u30ff]", text):
        return "ja"
    if re.search(r"[\uac00-\ud7af]", text):
        return "ko"
    if re.search(r"[\u3400-\u9fff]", text):
        return "han"
    return "other"


def make_units(cues):
    records, seen = [], set()
    for cue in cues:
        if (type(cue.start) not in (int, float) or type(cue.end) not in (int, float)
                or not math.isfinite(cue.start) or not math.isfinite(cue.end) or not 0 <= cue.start <= cue.end):
            raise UserError("缓存字幕时间无效，请选择外挂字幕重新补建。")
        if (not isinstance(cue.text, str) or not isinstance(cue.lane, str)
                or not isinstance(cue.lines, (list, tuple)) or any(not isinstance(line, str) for line in cue.lines)):
            raise UserError("缓存字幕文本无效，请选择外挂字幕重新补建。")
        # Keep wrapped same-language lines together; split bilingual rows by script.
        lines = cue.lines or tuple(cue.text.splitlines())
        groups = {}
        for line in lines:
            text = line.strip()
            if text:
                groups.setdefault(_script(text), []).append(text)
        for language, parts in groups.items():
            text = " ".join(parts)
            for start in range(0, len(text), MAX_TEXT):
                part = text[start:start + MAX_TEXT]
                key = (cue.start, cue.end, part)
                if key in seen:
                    continue
                seen.add(key)
                lane = cue.lane if cue.lane.endswith("|" + language) else cue.lane + "|" + language
                records.append({"start": cue.start, "end": cue.end, "text": part, "lane": lane})
                if len(records) > MAX_CUES:
                    raise UserError(f"清理、分行及去重后仍超过{MAX_CUES}条有效字幕，请拆分字幕后处理。")
    records.sort(key=lambda c: (c["start"], c["end"]))
    units, lanes = [], {}
    for i, cue in enumerate(records):
        units.append({"start": cue["start"], "end": cue["end"], "text": cue["text"], "kind": "single", "cue_ids": [i]})
        lanes.setdefault(cue["lane"], []).append(i)
    for ids in lanes.values():
        for position, index in enumerate(ids):
            selected = [index]
            for following in ids[position + 1:position + 3]:
                before, current, first = records[selected[-1]], records[following], records[index]
                if (current["start"] < before["end"] - 0.1 or current["start"] - before["end"] > 2.5
                        or current["end"] - first["start"] > 20):
                    break
                selected.append(following)
                text = " ".join(records[i]["text"] for i in selected)
                if len(text) > MAX_TEXT:
                    break
                units.append({"start": first["start"], "end": current["end"], "text": text,
                              "kind": "context", "cue_ids": list(selected)})
    return records, units


def prepare(cues, config, ctx, client=None, existing=()):
    with ctx.measure("dialogue.plan"):
        records, units = make_units(cues)
    warnings, totals = [], {"hits": 0, "misses": 0, "database_hits": 0}
    if client and units:
        texts = list(dict.fromkeys(u["text"] for u in units))
        required, vectors = set(texts), {}
        for unit in existing:
            if unit["text"] not in required or unit.get("embedding_model") != client.embedding_model:
                continue
            try:
                vector = _vector(json.loads(unit.get("embedding") or "null"))
            except (ValueError, TypeError):
                vector = None
            if vector is not None:
                vectors[unit["text"]] = vector
        dimensions = {len(vector) for vector in vectors.values()}
        if len(dimensions) > 1:
            vectors, dimensions = {}, set()
        totals["database_hits"] = len(vectors)
        texts = [text for text in texts if text not in vectors]
        ctx.progress(0.76, f"台词索引：{len(records)}条字幕、{len(units)}个单句/短上下文，检查{len(texts)}条去重文本向量缓存。")
        for start in range(0, len(texts), 32):
            with ctx.measure("dialogue.embedding", count=min(32, len(texts) - start)):
                batch, info = cached_embeddings(ctx.workspace, client.embedding_model, texts[start:start + 32], lambda: client,
                                                expected_dimensions=dimensions, purpose="dialogue")
            dimensions.update(len(v) for v in batch.values())
            if len(dimensions) > 1:
                raise UserError("台词向量维度不一致，请检查embedding模型配置。")
            vectors.update(batch)
            for key in ("hits", "misses"):
                totals[key] += info[key]
            if info["cache_write_failed"] and not warnings:
                warnings.append("台词向量已生成，但缓存不可写；重试可能再次请求向量。")
            ctx.progress(0.76 + 0.2 * min(1, (start + 32) / len(texts)), "正在补齐独立台词语义向量…")
        for unit in units:
            unit.update(embedding=vectors[unit["text"]], embedding_model=client.embedding_model)
    config = config | {"cue_count": len(records), "unit_count": len(units), "semantic_count": len(units) if client else 0}
    if ctx.profiler:
        ctx.profiler.event("dialogue_index", **{k: config[k] for k in ("cue_count", "unit_count", "semantic_count")}, cache=totals)
    return {"config": config, "cues": records, "units": units}, warnings


def backfill(store, params, ctx, client_factory):
    video_id = params.get("video_id")
    if not isinstance(video_id, str) or not video_id.strip():
        raise UserError("请选择素材。")
    video = store.video(video_id)
    path = existing_file(video["path"], "原视频", {".mp4"})
    if fingerprint(path) != [video["source_size"], video["source_mtime"]]:
        raise UserError("原视频已变化，请先更新素材索引，再补建台词。")
    semantic = params.get("semantic", True)
    if not isinstance(semantic, bool):
        raise UserError("semantic必须是true或false。")
    client = client_factory() if semantic else None
    saved = store.subtitle_indexes(video["group_id"]).get(video_id)
    previous = json.loads(video.get("index_config") or "{}")
    value = params.get("subtitle_path")
    if value is None:
        value = (saved or previous).get("subtitle_path") or video["subtitle_path"]
    if not isinstance(value, str):
        raise UserError("字幕路径必须为文本。")
    if not value.strip():
        value = (saved or previous).get("subtitle_path") or video["subtitle_path"]
    offset = finite_number(params.get("subtitle_offset", (saved or previous).get("subtitle_offset", 0)), "字幕偏移", -86400, 86400)
    warnings, cues = [], []
    if value.strip():
        subtitle = existing_file(value.strip(), "字幕", {".srt", ".ass", ".ssa", ".vtt"})
        config = source_config(video, subtitle, offset)
        diagnostics = {}
        with ctx.measure("subtitles.parse"):
            try:
                cues, warnings = parse_subtitles(subtitle, offset, video["duration"], diagnostics=diagnostics)
            finally:
                if ctx.profiler and diagnostics:
                    ctx.profiler.event("subtitles.cleanup", **diagnostics)
    elif saved and healthy(video, saved):
        if offset != saved.get("subtitle_offset", 0):
            raise UserError("复用已保存字幕时不能改变偏移，请选择外挂字幕后再调整。")
        cues = [Cue(**cue) for cue in store.subtitle_cues(video_id)]
        config = saved
    else:
        # Recover only the exact previous index checkpoint; never guess another file.
        if previous.get("source") != fingerprint(path):
            raise UserError("没有可复用的字幕，请选择外挂字幕，或先建立带自动转录的素材索引。")
        if offset != previous.get("subtitle_offset", 0):
            raise UserError("复用旧字幕时不能改变偏移，请选择外挂字幕后再调整。")
        signature = hashlib.sha256(json.dumps(previous, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
        checkpoint = ctx.workspace / "cache" / "index" / video_id / signature / "checkpoint.json"
        try:
            if previous.get("transcribe") and checkpoint.stat().st_size <= 128 * 1024 * 1024:
                raw = json.loads(checkpoint.read_text(encoding="utf-8"))
                cues = [Cue(**c) for c in raw.get("cues", [])]
        except (OSError, ValueError, TypeError, AttributeError):
            cues = []
        approximate = not bool(cues)
        if approximate:
            cues = [Cue(r["start"], r["end"], r["subtitle"]) for r in store.segments(video["group_id"])
                    if r["video_id"] == video_id and r["subtitle"].strip()]
            warnings.append("仅能复用旧片段字幕，台词时码采用原片段范围；选择外挂字幕可补建精确时间。")
        config = source_config(video, offset=offset, origin="legacy" if approximate else "asr", approximate=approximate)
    if not cues:
        raise UserError("没有可补建的台词，请选择外挂字幕；此操作不会自动转录或分析画面。")
    prepared, notices = prepare(cues, config, ctx, client, store.subtitle_units(video["group_id"], video_id))
    warnings.extend(notices)
    if not healthy(video, prepared["config"]):
        raise UserError("原视频或字幕在补建过程中发生变化，已保留旧台词索引，请重试。")
    with ctx.measure("database.replace_subtitles"):
        store.replace_subtitles(video_id, prepared)
    ctx.progress(1, "独立台词索引已保存，视觉索引保持原样。")
    return {"video_id": video_id, "subtitle_index": prepared["config"], "warnings": warnings}
