"""Grouped media indexing and honest lexical / semantic retrieval."""
import hashlib
import json
import math
import os
import re
import uuid
from pathlib import Path

from .ai import OpenAIClient
from .errors import UserError
from .store import Store
from .subtitles import Cue, parse_subtitles
from .validation import existing_file, finite_number


def _text(value, label, maximum, required=True):
    if not isinstance(value, str):
        raise UserError(f"{label}必须是文本。")
    value = value.strip()
    if required and not value:
        raise UserError(f"请填写{label}。")
    if len(value) > maximum or "\x00" in value:
        raise UserError(f"{label}最长{maximum}个字符，且不能包含空字符。")
    return value


def _id(params, key):
    return _text(params.get(key, ""), "动画分组" if key == "group_id" else "视频", 128)


def _flag(params, key):
    value = params.get(key, False)
    if not isinstance(value, bool):
        raise UserError(f"{key}必须是 true 或 false。")
    return value


def _fingerprint(path):
    stat = Path(path).stat()
    return stat.st_size, stat.st_mtime_ns


def _file_health(video, group=None):
    path = Path(video["path"])
    if not path.is_file():
        return "missing"
    if _fingerprint(path) != (video["source_size"], video["source_mtime"]):
        return "changed"
    if video.get("index_config"):
        try:
            config = json.loads(video["index_config"])
            subtitle = config.get("subtitle_path")
            if subtitle and (not Path(subtitle).is_file() or list(_fingerprint(subtitle)) != config.get("subtitle_fingerprint")):
                return "changed"
            if group and config.get("context") != group["description"]:
                return "changed"
        except (ValueError, OSError):
            return "changed"
    return video.get("status", "indexed")


def _video_public(video, group=None):
    try:
        config = json.loads(video.get("index_config") or "{}")
        offset = config.get("subtitle_offset", 0)
    except (ValueError, AttributeError):
        offset = 0
    return {key: video[key] for key in ("id", "path", "name", "duration", "width", "height", "fps", "subtitle_path")} | {
        "status": _file_health(video, group), "subtitle_offset": offset}


def _transcribe(path, info, params, ctx):
    if not info.get("has_audio"):
        raise UserError("此视频没有音轨，无法生成字幕；请使用外挂字幕或视觉分析。")
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise UserError("尚未安装 faster-whisper，请运行 scripts/setup.ps1 -WithWhisper 后重试。", "dependency")
    model_name = _text(str(ctx.settings.get("whisper_model") or "small"), "Whisper 模型", 1000)
    device = str(ctx.settings.get("whisper_device") or "cpu")
    if device not in ("cpu", "cuda"):
        raise UserError("Whisper 设备必须为 cpu 或 cuda。")
    language = params.get("language", "auto")
    if not isinstance(language, str) or not re.fullmatch(r"auto|[a-z]{2,3}", language):
        raise UserError("语言应为 auto 或 ja、zh、en 等小写语言代码。")
    ctx.progress(0.05, "正在加载语音模型；首次转录会下载模型到工作区，请保持网络连接…")
    try:
        model = WhisperModel(model_name, device=device, compute_type="int8" if device == "cpu" else "float16",
                             download_root=str(ctx.workspace / "models" / "whisper"))
        segments, detected = model.transcribe(str(path), language=None if language == "auto" else language,
                                             beam_size=5, vad_filter=True)
        cues = []
        for segment in segments:
            start, end, text = float(segment.start), float(segment.end), str(segment.text).strip()
            if text and math.isfinite(start) and math.isfinite(end) and end > max(0, start) and start < info["duration"]:
                cues.append(Cue(max(0, start), min(info["duration"], end), text))
            if math.isfinite(end):
                ctx.progress(0.05 + 0.1 * min(1, max(0, end) / info["duration"]), "正在生成带时间轴的原语字幕…")
    except Exception as exc:
        raise UserError("语音转录失败，请检查模型名称、网络和 CPU/CUDA 配置。可以将已下载模型目录填入 Whisper 模型设置。\n" + str(exc)[:400], "asr")
    if not cues:
        raise UserError("未识别到可用台词，请检查音轨，或改用外挂字幕/视觉分析。", "asr")
    cues.sort(key=lambda cue: (cue.start, cue.end))
    return cues, [f"字幕来自自动语音识别（语言 {getattr(detected, 'language', language)}），人名及背景音乐处可能有误，请预览核对。"]


def _save_checkpoint(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _index(store, params, ctx):
    video_id = _id(params, "video_id")
    video = store.video(video_id)
    group = store.group(video["group_id"])
    path = existing_file(video["path"], "原视频", {".mp4"})
    fingerprint = _fingerprint(path)
    visual, transcribe = _flag(params, "visual"), _flag(params, "transcribe")
    semantic = _flag(params, "semantic")
    seconds = finite_number(params.get("segment_seconds", 8), "片段索引间隔", 2, 60)
    offset = finite_number(params.get("subtitle_offset", 0), "字幕偏移", -86400, 86400)
    subtitle_value = params.get("subtitle_path", "")
    if subtitle_value is None:
        subtitle_value = ""
    subtitle_value = _text(subtitle_value, "字幕路径", 32768, required=False)
    subtitle_path = existing_file(subtitle_value, "字幕", {".srt", ".vtt", ".ass", ".ssa"}) if subtitle_value else None
    if not subtitle_path and not transcribe and not visual:
        raise UserError("请绑定外挂字幕，或勾选自动转录/视觉分析后再建立索引。")
    client = OpenAIClient(ctx.settings) if visual or semantic else None
    info = ctx.media.probe(path)
    if math.ceil(info["duration"] / seconds) > 20000:
        raise UserError("单次索引超过20000个时间段，请增大索引间隔或先分割长视频。")
    config = {"version": 2, "source": list(fingerprint), "subtitle_path": str(subtitle_path) if subtitle_path else "",
              "subtitle_fingerprint": list(_fingerprint(subtitle_path)) if subtitle_path else None,
              "subtitle_offset": offset, "visual": visual, "semantic": semantic or visual, "transcribe": transcribe and not bool(subtitle_path),
              "language": params.get("language", "auto"), "segment_seconds": seconds, "context": group["description"],
              "vision_model": client.vision_model if visual else "", "embedding_model": client.embedding_model if client else "",
              "whisper_model": ctx.settings.get("whisper_model", "small")}
    signature = hashlib.sha256(json.dumps(config, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
    cache = ctx.workspace / "cache" / "index" / video_id / signature
    cache.mkdir(parents=True, exist_ok=True)
    checkpoint_path = cache / "checkpoint.json"
    checkpoint = {"segments": {}, "cues": []}
    if checkpoint_path.is_file():
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if not isinstance(checkpoint.get("segments"), dict) or not isinstance(checkpoint.get("cues"), list):
                raise ValueError()
        except (ValueError, OSError):
            checkpoint = {"segments": {}, "cues": []}
    warnings, cues = [], []
    if subtitle_path:
        cues, warnings = parse_subtitles(subtitle_path, offset, info["duration"])
        if transcribe:
            warnings.append("已绑定外挂字幕，按设置优先使用外挂字幕，没有再次执行语音转录。")
    elif transcribe:
        if checkpoint["cues"]:
            cues = [Cue(**cue) for cue in checkpoint["cues"]]
            warnings.append("已复用本次索引配置下生成的自动字幕，请预览核对人名和台词。")
        else:
            cues, asr_warnings = _transcribe(path, info, params, ctx)
            warnings.extend(asr_warnings)
            checkpoint["cues"] = [dict(start=c.start, end=c.end, text=c.text) for c in cues]
            _save_checkpoint(checkpoint_path, checkpoint)
    if not visual:
        warnings.append("本次未分析画面；场景、人物外貌和无台词情节不能通过字幕索引完整检索。")
    if not client:
        warnings.append("本次仅建立本地字幕关键词索引；如需语义和跨语言检索，请勾选建立语义索引并重新索引。")
    total = math.ceil(info["duration"] / seconds)
    segments, cue_index = [], 0
    for index in range(total):
        start, end = index * seconds, min(info["duration"], (index + 1) * seconds)
        # Keep a point cue exactly at this window's start for this window only.
        while cue_index < len(cues) and cues[cue_index].end <= start and cues[cue_index].start < start:
            cue_index += 1
        relevant = []
        # Overlapping cues are legal, so scan all remaining starts within this window.
        current = cue_index
        while current < len(cues) and cues[current].start < end:
            if cues[current].overlaps(start, end) and cues[current].text not in relevant:
                relevant.append(cues[current].text)
            current += 1
        subtitle = "\n".join(relevant)
        if not subtitle and not visual:
            continue
        saved = checkpoint["segments"].get(str(index))
        if saved and Path(saved.get("thumbnail", "")).is_file():
            segment = saved
        else:
            thumbnail = cache / f"{index:06d}.jpg"
            mid = start + (end - start) / 2
            ctx.media.extract_frame(path, mid, thumbnail)
            caption = ""
            if visual:
                first, last = cache / f"{index:06d}_a.jpg", cache / f"{index:06d}_b.jpg"
                ctx.media.extract_frame(path, start + (end - start) * 0.15, first)
                ctx.media.extract_frame(path, start + (end - start) * 0.85, last)
                caption = client.caption([first, thumbnail, last], subtitle, group["description"])
            segment = {"start": start, "end": end, "subtitle": subtitle, "caption": caption,
                       "thumbnail": str(thumbnail), "embedding": None, "embedding_model": ""}
            checkpoint["segments"][str(index)] = segment
            _save_checkpoint(checkpoint_path, checkpoint)
        segments.append(segment)
        ctx.progress(0.15 + 0.6 * (index + 1) / total, f"正在索引 {video['name']}：{index + 1}/{total} 段…")
    if not segments:
        raise UserError("没有可建立索引的片段，请检查字幕、时间偏移或启用视觉分析。")
    if client:
        pending = [s for s in segments if not s.get("embedding")]
        for position in range(0, len(pending), 32):
            batch = pending[position:position + 32]
            vectors = client.embed([s["subtitle"] + "\n" + s["caption"] for s in batch])
            for segment, vector in zip(batch, vectors):
                segment["embedding"], segment["embedding_model"] = vector, client.embedding_model
            _save_checkpoint(checkpoint_path, checkpoint)
            ctx.progress(0.75 + 0.2 * min(1, (position + len(batch)) / len(pending)), "正在建立多语言语义向量…")
    if _fingerprint(path) != fingerprint:
        raise UserError("索引过程中原视频发生变化，已保留旧索引；请等待文件写入完成后重试。")
    if subtitle_path and list(_fingerprint(subtitle_path)) != config["subtitle_fingerprint"]:
        raise UserError("索引过程中字幕发生变化，已保留旧索引；请重试。")
    if store.group(group["id"])["description"] != group["description"]:
        raise UserError("索引过程中分组说明发生变化，已保留旧索引；请重试。")
    store.replace_index(video_id, info, fingerprint, config, str(subtitle_path) if subtitle_path else "", segments)
    ctx.progress(1, "索引完成，可重复查询。")
    mode = "visual+subtitle" if visual and cues else "visual" if visual else "subtitle"
    return {"video_id": video_id, "segment_count": len(segments), "mode": mode, "warnings": warnings}


def _normalize(value):
    return re.sub(r"\s+", "", value.casefold())


def _lexical(query, text):
    text = _normalize(text)
    terms = [t.strip('"“”\'‘’') for t in re.split(r"[\s,，;；]+", query.casefold()) if t.strip('"“”\'‘’')]
    values = []
    for term in terms:
        term = _normalize(term)
        if term in text:
            values.append(1.0)
        elif len(term) >= 3 and re.search(r"[\u3040-\u30ff\u3400-\u9fff]", term):
            grams = {term[i:i + 2] for i in range(len(term) - 1)}
            overlap = sum(1 for gram in grams if gram in text) / len(grams)
            values.append(0.6 * overlap if overlap >= 0.5 else 0)
        else:
            values.append(0)
    return sum(values) / len(values) if values else 0


def _search(store, params, ctx):
    group_id = _id(params, "group_id")
    group = store.group(group_id)
    query = _text(params.get("query", ""), "查询关键词", 500)
    limit = finite_number(params.get("limit", 20), "结果数量", 1, 100)
    if limit != int(limit):
        raise UserError("结果数量必须是整数。")
    semantic = _flag(params, "semantic")
    rows = store.segments(group_id)
    warnings = []
    if not rows:
        return {"results": [], "warnings": ["当前分组还没有可检索索引，请先选择视频并建立索引。"]}
    health = {}
    for video in store.videos(group_id):
        health[video["id"]] = _file_health(video, group)
    unavailable = {row["video_id"] for row in rows if health[row["video_id"]] in ("missing", "changed")}
    if unavailable:
        warnings.append(f"有{len(unavailable)}个视频的原片、字幕或分组说明已变化/缺失；已跳过旧索引，请重新索引。")
    rows = [row for row in rows if row["video_id"] not in unavailable]
    query_vector, model = None, str(ctx.settings.get("embedding_model") or "text-embedding-3-small")
    if semantic:
        compatible = [row for row in rows if row["embedding"] and row["embedding_model"] == model]
        if not str(ctx.settings.get("api_key") or "").strip():
            warnings.append("未填写 OpenAI 密钥，本次降级为本地关键词检索。")
        elif not compatible:
            warnings.append("没有与当前模型匹配的语义向量，本次使用关键词检索；请填写密钥并重新建立索引。")
        else:
            try:
                query_vector = OpenAIClient(ctx.settings).embed([query])[0]
                if len(compatible) != len(rows):
                    warnings.append("部分片段未建立当前模型的向量，这些片段仅参与关键词匹配。")
            except UserError as exc:
                warnings.append(str(exc) + " 本次已降级为关键词检索。")
    candidates, invalid_vectors = [], False
    for row in rows:
        text = "\n".join(part for part in (row["caption"], row["subtitle"]) if part)
        lexical = _lexical(query, text)
        cosine = None
        if query_vector and row["embedding"] and row["embedding_model"] == model:
            try:
                vector = json.loads(row["embedding"])
                if len(vector) != len(query_vector) or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector):
                    raise ValueError()
                norm = math.sqrt(sum(v * v for v in vector))
                cosine = sum(a * b for a, b in zip(vector, query_vector)) / norm if norm else None
            except (ValueError, TypeError):
                invalid_vectors = True
        if lexical <= 0 and (cosine is None or cosine < 0.22):
            continue
        score = max(lexical, 0.8 * max(0, cosine or 0) + 0.2 * lexical)
        match_type = "语义+关键词" if cosine is not None and lexical else "语义" if cosine is not None else "画面描述关键词" if row["caption"] else "字幕关键词"
        candidates.append({"video_id": row["video_id"], "path": row["path"], "name": row["name"],
            "start": max(0, row["start"]), "end": min(row["duration"], row["end"]),
            "text": text, "score": round(score, 4), "thumbnail": row["thumbnail"], "match_type": match_type})
    if invalid_vectors:
        warnings.append("发现损坏或维度不匹配的向量，已跳过其语义评分，请重新索引。")
    # Merge temporal neighbours before applying the result cap, keeping useful clip lengths.
    candidates.sort(key=lambda item: (item["video_id"], item["start"]))
    merged = []
    for item in candidates:
        previous = merged[-1] if merged else None
        if previous and previous["video_id"] == item["video_id"] and item["start"] <= previous["end"] + 0.05 and item["end"] - previous["start"] <= 60:
            previous["end"] = max(previous["end"], item["end"])
            if item["score"] > previous["score"]:
                previous["score"], previous["thumbnail"], previous["match_type"] = item["score"], item["thumbnail"], item["match_type"]
            if item["text"] not in previous["text"]:
                previous["text"] = (previous["text"] + "\n" + item["text"])[:12000]
        else:
            merged.append(item)
    merged.sort(key=lambda item: (-item["score"], item["name"], item["start"]))
    return {"results": merged[:int(limit)], "warnings": warnings}


def handle(command, params, ctx):
    with Store(ctx.workspace) as store:
        if command == "group.list":
            return {"groups": store.groups()}
        if command in ("group.create", "group.update"):
            name = _text(params.get("name", ""), "分组名称", 120)
            description = _text(params.get("description", ""), "分组说明", 6000, required=False)
            return store.save_group(name, description, _id(params, "group_id") if command == "group.update" else None)
        if command == "group.delete":
            store.delete_group(_id(params, "group_id"))
            return {"deleted": True}
        if command == "video.list":
            group_id = _id(params, "group_id")
            group = store.group(group_id)
            return {"videos": [_video_public(video, group) for video in store.videos(group_id)]}
        if command == "video.add":
            group_id = _id(params, "group_id")
            group = store.group(group_id)
            paths = params.get("paths")
            if not isinstance(paths, list) or not paths or len(paths) > 500:
                raise UserError("请一次选择1至500个 MP4 视频。")
            existing = {video["path_key"] for video in store.videos(group_id)}
            records, warnings = [], []
            for position, value in enumerate(paths):
                path = existing_file(value, "视频", {".mp4"})
                key = os.path.normcase(str(path))
                if key in existing:
                    warnings.append(f"已跳过重复素材：{path.name}")
                    continue
                info = ctx.media.probe(path)
                size, modified = _fingerprint(path)
                records.append(info | {"id": uuid.uuid4().hex, "path_key": key, "name": path.name,
                                       "source_size": size, "source_mtime": modified})
                existing.add(key)
                ctx.progress((position + 1) / len(paths), f"已检查视频 {position + 1}/{len(paths)}")
            store.add_videos(group_id, records)
            return {"videos": [_video_public(video, group) for video in store.videos(group_id)], "warnings": warnings}
        if command == "video.remove":
            store.remove_video(_id(params, "video_id"))
            return {"deleted": True}
        if command == "index.run":
            return _index(store, params, ctx)
        if command == "search.run":
            return _search(store, params, ctx)
    raise UserError(f"未知素材库操作：{command}")
