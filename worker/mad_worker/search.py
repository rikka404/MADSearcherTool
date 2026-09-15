"""Grouped media indexing and honest lexical / semantic retrieval."""
import hashlib
import bisect
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
from .characters import normalize_characters, signature as character_signature, display_name, active_characters
from . import segmentation
from .images import IMAGE_POLICY
from .prompts import PROMPT_VERSION
from .profiling import IndexProfile
from . import retrieval
from .query_vectors import cached_embeddings, _vector
from . import dialogue, dialogue_search


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
            if group and config.get("visual") and (group.get("characters") or config.get("character_signature")):
                if config.get("character_signature") != character_signature(group.get("characters", [])):
                    return "changed"
        except (ValueError, OSError):
            return "changed"
    return video.get("status", "indexed")


def _video_public(video, group=None, subtitle_config=None):
    try:
        config = json.loads(video.get("index_config") or "{}")
        offset = config.get("subtitle_offset", 0)
    except (ValueError, AttributeError):
        offset = 0
    result = {key: video[key] for key in ("id", "path", "name", "duration", "width", "height", "fps", "subtitle_path")} | {
        "status": _file_health(video, group), "subtitle_offset": offset}
    if subtitle_config:
        result.update(dialogue_cue_count=subtitle_config.get("cue_count", 0),
                      dialogue_semantic_count=subtitle_config.get("semantic_count", 0),
                      dialogue_ready=dialogue.healthy(video, subtitle_config))
        if subtitle_config.get("subtitle_path"):
            result.update(subtitle_path=subtitle_config["subtitle_path"], subtitle_offset=subtitle_config.get("subtitle_offset", 0))
    return result


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
        with ctx.measure("asr.model_load"):
            model = WhisperModel(model_name, device=device, compute_type="int8" if device == "cpu" else "float16",
                                 download_root=str(ctx.workspace / "models" / "whisper"))
        with ctx.measure("asr.transcribe"):
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


def _save_checkpoint(path, data, ctx):
    with ctx.measure("checkpoint.write"):
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
    split_options = segmentation.options(params)
    offset = finite_number(params.get("subtitle_offset", 0), "字幕偏移", -86400, 86400)
    subtitle_value = params.get("subtitle_path", "")
    if subtitle_value is None:
        subtitle_value = ""
    subtitle_value = _text(subtitle_value, "字幕路径", 32768, required=False)
    subtitle_path = existing_file(subtitle_value, "字幕", {".srt", ".vtt", ".ass", ".ssa"}) if subtitle_value else None
    if not subtitle_path and not transcribe and not visual:
        raise UserError("请绑定外挂字幕，或勾选自动转录/视觉分析后再建立索引。")
    client = OpenAIClient(ctx.settings, profile=ctx.profiler) if visual or semantic else None
    with ctx.measure("media.probe"):
        info = ctx.media.probe(path)
    plan, sample_key, sample_directory, plan_stats = segmentation.prepare_plan(
        path, info, fingerprint, split_options, ctx, video_id, visual)
    cards = group.get("characters", [])
    cards_by_id = {card["id"]: card for card in cards}
    config = {"version": 4, "source": list(fingerprint), "subtitle_path": str(subtitle_path) if subtitle_path else "",
              "subtitle_fingerprint": list(_fingerprint(subtitle_path)) if subtitle_path else None,
              "subtitle_offset": offset, "visual": visual, "semantic": semantic or visual, "transcribe": transcribe and not bool(subtitle_path),
              "language": params.get("language", "auto"), "segment_seconds": split_options["fixed_seconds"], "context": group["description"],
              "segmentation": split_options, "sample_key": sample_key, "sampling_version": segmentation.SAMPLING_VERSION,
              "vision_model": client.vision_model if visual else "", "embedding_model": client.embedding_model if client else "",
              "whisper_model": ctx.settings.get("whisper_model", "small"),
              "character_signature": character_signature(cards) if visual else "",
              "prompt_version": PROMPT_VERSION if visual else "", "image_policy": IMAGE_POLICY if visual else ""}
    if ctx.profiler:
        ctx.profiler.event("index_config", visual=visual, semantic=semantic or visual, transcribe=config["transcribe"],
                           character_count=len(cards), reference_count=sum(len(c["reference_images"]) for c in cards),
                           segmentation=split_options, prompt_version=config["prompt_version"], image_policy=config["image_policy"])
    signature = hashlib.sha256(json.dumps(config, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
    cache = ctx.workspace / "cache" / "index" / video_id / signature
    cache.mkdir(parents=True, exist_ok=True)
    checkpoint_path = cache / "checkpoint.json"
    checkpoint = {"segments": {}, "cues": []}
    if checkpoint_path.is_file():
        try:
            with ctx.measure("checkpoint.read"):
                checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if not isinstance(checkpoint, dict) or not isinstance(checkpoint.get("segments"), dict) or not isinstance(checkpoint.get("cues"), list):
                raise ValueError()
        except (ValueError, OSError):
            checkpoint = {"segments": {}, "cues": []}
    warnings, cues = [], []
    if subtitle_path:
        diagnostics = {}
        with ctx.measure("subtitles.parse"):
            try:
                cues, warnings = parse_subtitles(subtitle_path, offset, info["duration"], diagnostics=diagnostics)
            finally:
                if ctx.profiler and diagnostics:
                    ctx.profiler.event("subtitles.cleanup", **diagnostics)
        if transcribe:
            warnings.append("已绑定外挂字幕，按设置优先使用外挂字幕，没有再次执行语音转录。")
    elif transcribe:
        if checkpoint["cues"]:
            cues = [Cue(**cue) for cue in checkpoint["cues"]]
            if ctx.profiler:
                ctx.profiler.count("asr_cache_hits")
            warnings.append("已复用本次索引配置下生成的自动字幕，请预览核对人名和台词。")
        else:
            cues, asr_warnings = _transcribe(path, info, params, ctx)
            warnings.extend(asr_warnings)
            checkpoint["cues"] = [dict(start=c.start, end=c.end, text=c.text) for c in cues]
            _save_checkpoint(checkpoint_path, checkpoint, ctx)
    if not visual:
        warnings.append("本次未分析画面；场景、人物外貌和无台词情节不能通过字幕索引完整检索。")
    if not client:
        warnings.append("本次仅建立本地字幕关键词索引；如需语义和跨语言检索，请勾选建立语义索引并重新索引。")
    total = len(plan)
    segments, cue_index = [], 0
    selected = []
    for index, planned in enumerate(plan):
        start, end = planned["start"], planned["end"]
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
        selected.append({**planned, "index": index, "subtitle": subtitle})
    if not selected:
        raise UserError("没有可建立索引的片段，请检查字幕、时间偏移或启用视觉分析。")
    segmentation.materialize_samples(path, selected, sample_directory, ctx)
    with ctx.measure("samples.deduplicate"):
        for planned in selected:
            planned["samples"] = segmentation.deduplicate(planned) if visual else planned["samples"]
    # Reuse unaffected segments even when another shot or the maximum length changes.
    analysis_directory = ctx.workspace / "cache" / "analysis" / video_id
    with ctx.measure("analysis_cache.read"):
        for planned in selected:
            planned["analysis_path"] = analysis_directory / _analysis_key(config, planned, path)
            if not _cached_segment(checkpoint, planned):
                cached = segmentation.read_json(planned["analysis_path"])
                if isinstance(cached, dict):
                    cached["thumbnail"] = _thumbnail(planned)
                    # The content-addressed key already includes the source interval and samples.
                    cached["shot_id"] = planned["shot_id"]
                    cached.setdefault("metadata", {})
                    if isinstance(cached["metadata"], dict):
                        cached["metadata"].update({k: planned[k] for k in ("shot_start", "shot_end", "start_frame", "end_frame") if k in planned})
                    candidate = {"segments": {str(planned["index"]): cached}}
                    if _cached_segment(candidate, planned):
                        checkpoint["segments"][str(planned["index"])] = cached
                        if ctx.profiler:
                            ctx.profiler.count("analysis_cache_hits")
    references = sum(len(c["reference_images"]) for c in active_characters(cards)) if visual else 0
    remaining = [p for p in selected if not _cached_segment(checkpoint, p)]
    estimate = {"visual_requests": len(selected) if visual else 0,
                "sample_images": sum(len(p["samples"]) for p in selected) if visual else 0,
                "reference_images": len(selected) * references,
                "pending_visual_requests": len(remaining) if visual else 0,
                "pending_sample_images": sum(len(p["samples"]) for p in remaining) if visual else 0,
                "pending_reference_images": len(remaining) * references}
    if ctx.profiler:
        ctx.profiler.event("index_estimate", **estimate)
        for key, value in estimate.items():
            ctx.profiler.count(key, value)
    ctx.progress(0.15, f"分段完成：{plan_stats['shot_count']}个镜头、{len(selected)}个有效片段；"
                 f"本次预计请求{estimate['pending_visual_requests']}次，采样图{estimate['pending_sample_images']}张、"
                 f"参考图累计{estimate['pending_reference_images']}张（费用以实际token为准）。")
    for planned in selected:
        index, start, end, subtitle = planned["index"], planned["start"], planned["end"], planned["subtitle"]
        saved = checkpoint["segments"].get(str(index))
        cache_hit = _cached_segment(checkpoint, planned)
        if cache_hit:
            segment = saved
            if ctx.profiler:
                ctx.profiler.count("segment_cache_hits")
                ctx.profiler.event("segment_cache_hit", segment=index, start_seconds=start, end_seconds=end)
        else:
            samples = planned["samples"]
            thumbnail = _thumbnail(planned)
            analysis = {"caption": "", "character_matches": []}
            if visual:
                with ctx.measure("ai.visual_analysis", segment=index, shot_id=planned["shot_id"],
                                 start_seconds=start, end_seconds=end, sample_count=len(samples),
                                 sample_times=[s["time"] for s in samples]):
                    analysis = client.analyze_clip([s["path"] for s in samples], subtitle, group["description"], cards,
                                                  sample_times=[s["time"] for s in samples], clip_range=(start, end))
            segment = {"start": start, "end": end, "subtitle": subtitle, **analysis,
                       "thumbnail": str(thumbnail), "embedding": None, "embedding_model": "",
                       "shot_id": planned["shot_id"], "metadata": {
                           "sample_times": [s["time"] for s in samples], "sample_frames": [s.get("frame") for s in samples],
                           **{k: planned[k] for k in ("shot_start", "shot_end", "start_frame", "end_frame") if k in planned}}}
            checkpoint["segments"][str(index)] = segment
        if not cache_hit or not planned["analysis_path"].is_file():
            with ctx.measure("analysis_cache.write"):
                segmentation.save_json(planned["analysis_path"], segment)
        # Per-segment files persist every success. Batch the growing aggregate to
        # avoid rewriting the entire index after each of thousands of short shots.
        if (index + 1) % 32 == 0 or planned is selected[-1]:
            _save_checkpoint(checkpoint_path, checkpoint, ctx)
        segments.append(segment)
        ctx.progress(0.15 + 0.6 * (index + 1) / total, f"正在索引 {video['name']}：{index + 1}/{total} 段…")
    if not segments:
        raise UserError("没有可建立索引的片段，请检查字幕、时间偏移或启用视觉分析。")
    if client:
        pending = [s for s in segments if not s.get("embedding")]
        for position in range(0, len(pending), 32):
            batch = pending[position:position + 32]
            with ctx.measure("ai.embedding", count=len(batch)):
                vectors = client.embed([s["subtitle"] + "\n" + s["caption"] +
                    ("\n画面角色：" + "、".join(display_name(cards_by_id[m["character_id"]])
                        for m in s.get("character_matches", [])) if s.get("character_matches") else "") for s in batch])
            for segment, vector in zip(batch, vectors):
                segment["embedding"], segment["embedding_model"] = vector, client.embedding_model
            _save_checkpoint(checkpoint_path, checkpoint, ctx)
            ctx.progress(0.75 + 0.2 * min(1, (position + len(batch)) / len(pending)), "正在建立多语言语义向量…")
    subtitle_config = dialogue.source_config(video | {"source_size": fingerprint[0], "source_mtime": fingerprint[1]},
        str(subtitle_path) if subtitle_path else "", offset, "file" if subtitle_path else "asr")
    prepared_subtitles, subtitle_warnings = dialogue.prepare(cues, subtitle_config, ctx, client if semantic else None,
                                                           store.subtitle_units(group["id"], video_id))
    warnings.extend(subtitle_warnings)
    with ctx.measure("index.validate_sources"):
        if _fingerprint(path) != fingerprint:
            raise UserError("索引过程中原视频发生变化，已保留旧索引；请等待文件写入完成后重试。")
        if subtitle_path and list(_fingerprint(subtitle_path)) != config["subtitle_fingerprint"]:
            raise UserError("索引过程中字幕发生变化，已保留旧索引；请重试。")
        current_group = store.group(group["id"])
        if current_group["description"] != group["description"]:
            raise UserError("索引过程中分组说明发生变化，已保留旧索引；请重试。")
        if visual and character_signature(current_group["characters"]) != config["character_signature"]:
            raise UserError("索引过程中角色资料或参考图片发生变化，已保留旧索引；请重试。")
    with ctx.measure("database.replace_index"):
        store.replace_index(video_id, info, fingerprint, config, str(subtitle_path) if subtitle_path else "", segments,
                            shot_ranges=_shot_ranges(plan), subtitle_index=prepared_subtitles)
    # Keep vectors with each complete segment so unchanged shots need no cloud request.
    # The committed index remains usable even if this optional cache write fails.
    try:
        with ctx.measure("analysis_cache.write"):
            for planned, segment in zip(selected, segments):
                segmentation.save_json(planned["analysis_path"], segment)
    except OSError:
        warnings.append("索引已保存，但部分分析缓存写入失败；下次重建可能需要重新分析，请检查磁盘空间。")
    ctx.progress(1, "索引完成，可重复查询。")
    mode = "visual+subtitle" if visual and cues else "visual" if visual else "subtitle"
    return {"video_id": video_id, "segment_count": len(segments), "mode": mode, "warnings": warnings,
            "subtitle_index": prepared_subtitles["config"],
            "shot_plan": plan_stats, "image_estimate": estimate,
            "character_match_count": sum(len(s.get("character_matches", [])) for s in segments)}


def _cached_segment(checkpoint, planned):
    saved = checkpoint["segments"].get(str(planned["index"]))
    return (isinstance(saved, dict) and saved.get("start") == planned["start"] and saved.get("end") == planned["end"]
            and saved.get("subtitle") == planned["subtitle"] and saved.get("shot_id") == planned["shot_id"]
            and isinstance(saved.get("metadata"), dict) and isinstance(saved.get("thumbnail"), str)
            and saved.get("metadata", {}).get("sample_times") == [s["time"] for s in planned["samples"]]
            and isinstance(saved.get("caption"), str) and isinstance(saved.get("character_matches"), list)
            and Path(saved.get("thumbnail", "")).is_file())


def _thumbnail(planned):
    middle = (planned["start"] + planned["end"]) / 2
    return min(planned["samples"], key=lambda s: abs(s["time"] - middle))["path"]


def _analysis_key(config, planned, path):
    identity = {k: config[k] for k in ("source", "visual", "semantic", "context", "vision_model", "embedding_model",
                                      "character_signature", "prompt_version", "image_policy", "sampling_version")}
    identity.update(path=str(path), start=planned["start"], end=planned["end"], subtitle=planned["subtitle"],
                    samples=[(s.get("frame"), s["time"]) for s in planned["samples"]])
    return segmentation.digest(identity) + ".json"


def _shot_ranges(plan):
    ranges = {}
    for segment in plan:
        if segment["shot_id"]:
            ranges[segment["shot_id"]] = [segment["shot_start"], segment["shot_end"]]
    return list(ranges.values())


def _profiled_index(params, ctx, subtitle_only=False):
    profile = IndexProfile(ctx.workspace, str(params.get("video_id", "")), ctx.settings.get("api_key", ""))
    ctx.profiler = profile
    try:
        with profile:
            ctx.progress(0, "索引日志：" + str(profile.path))
            with ctx.measure("database.open"):
                store = Store(ctx.workspace)
            with store, ctx.measure("index.work"):
                result = (dialogue.backfill(store, params, ctx, lambda: OpenAIClient(ctx.settings, profile=ctx.profiler))
                          if subtitle_only else _index(store, params, ctx))
        result.update(log_path=str(profile.path), timing_summary_path=str(profile.summary_path), timings=profile.summary)
        if profile.write_failed:
            result["warnings"].append("部分耗时日志未能写入，请检查logs目录权限和剩余空间。")
        return result
    except UserError as exc:
        raise UserError(str(exc) + "\n索引日志：" + str(profile.path), exc.code) from exc
    finally:
        ctx.profiler = None


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


def _scene_candidates(rows, plan, cards, character_filter, query_vectors, indexed_vectors, shot_ranges):
    shot_starts = {video: [r[0] for r in ranges] for video, ranges in shot_ranges.items()}
    candidates = []
    nearby = retrieval.neighbour_roles(rows, shot_ranges) if plan.compound and len(plan.groups) > 1 else {}
    for row in rows:
        matches = [{**m, "name": display_name(cards[m["character_id"]]), "start": row["start"], "end": row["end"],
                    "sample_times": row.get("metadata", {}).get("sample_times", [])}
                   for m in row.get("character_matches", []) if m["character_id"] in cards]
        visible_ids = {m["character_id"] for m in matches}
        if character_filter and character_filter not in visible_ids:
            continue
        text = "\n".join(part for part in (row["caption"], row["subtitle"]) if part)
        lexical_scores = {name: _lexical(value, text) for name, value in plan.variants.items()}
        if matches:
            text += "\n画面角色：" + "；".join(m["name"] + ("（待确认）" if m["confidence"] == "medium" else "")
                        + "：" + m["evidence"] for m in matches)
        semantic_scores = {}
        if query_vectors and row["id"] in indexed_vectors:
            vector, norm = indexed_vectors[row["id"]]
            for name, query_vector in query_vectors.items():
                if len(vector) != len(query_vector):
                    continue
                semantic_scores[name] = max(-1, min(1, sum(a * b for a, b in zip(vector, query_vector)) / norm))
        features = retrieval.candidate_features(plan, row, lexical_scores, semantic_scores, cards, nearby.get(row["id"], {}))
        if features is None:
            continue
        if plan.person_only:
            match_type = "角色出镜关联" if features["role_direct"] else "姓名文字提及"
        elif plan.compound:
            match_type = ("原句/外观补充" if features["route"] != "event" else "情节+人物证据" if features["role_direct"]
                          else "情节+邻近人物" if features["role_support"] else "情节相关")
        else:
            cosine, lexical = semantic_scores.get("original"), lexical_scores.get("original", 0)
            match_type = "语义+关键词" if cosine is not None and lexical else "语义" if cosine is not None else "画面描述关键词" if row["caption"] else "字幕关键词"
        context_start, context_end = row["start"], row["end"]
        ranges = shot_ranges.get(row["video_id"], [])
        if row.get("shot_id") and ranges:
            position = max(0, bisect.bisect_right(shot_starts[row["video_id"]], row["start"]) - 1)
            context_start, context_end = ranges[max(0, position - 1)][0], ranges[min(len(ranges) - 1, position + 1)][1]
        candidates.append({"video_id": row["video_id"], "path": row["path"], "name": row["name"],
            "shot_id": row.get("shot_id", ""), "context_start": context_start, "context_end": context_end,
            "duration": row["duration"], "sample_times": row.get("metadata", {}).get("sample_times", []),
            "start": max(0, row["start"]), "end": min(row["duration"], row["end"]),
            "text": text, "_ranking": features, "thumbnail": row["thumbnail"], "match_type": match_type, "character_matches": matches})
    retrieval.rank_candidates(candidates, plan)
    # Merge temporal neighbours before applying the result cap, keeping useful clip lengths.
    candidates.sort(key=lambda item: (item["video_id"], item["start"]))
    merged = []
    for item in candidates:
        previous = merged[-1] if merged else None
        if (previous and previous["video_id"] == item["video_id"] and previous["shot_id"] == item["shot_id"]
                and item["start"] <= previous["end"] + 0.05 and item["end"] - previous["start"] <= 60):
            previous["end"] = max(previous["end"], item["end"])
            previous["character_matches"].extend(item["character_matches"])
            if item["score"] > previous["score"]:
                previous["score"], previous["thumbnail"], previous["match_type"] = item["score"], item["thumbnail"], item["match_type"]
                previous["sample_times"] = item["sample_times"]
                previous["ranking_details"], previous["ranking_summary"] = item["ranking_details"], item["ranking_summary"]
            if item["text"] not in previous["text"]:
                previous["text"] = (previous["text"] + "\n" + item["text"])[:12000]
        else:
            merged.append(item)
    merged.sort(key=lambda item: (-item["score"], item["name"], item["start"]))
    return merged



def _search(store, params, ctx):
    group_id = _id(params, "group_id")
    group = store.group(group_id)
    cards = {c["id"]: c for c in group.get("characters", [])}
    character_filter = params.get("character_id")
    if character_filter is not None and (not isinstance(character_filter, str) or character_filter not in cards):
        raise UserError("指定角色不属于当前动画分组。")
    mode = params.get("mode", "combined")
    if mode not in ("combined", "dialogue", "scene"):
        raise UserError("检索模式应为combined、dialogue或scene。")
    query = _text(params.get("query", ""), "查询关键词", 500, required=mode == "dialogue" or not bool(character_filter))
    plan = retrieval.plan_query(query, {} if mode == "dialogue" else cards,
                                None if mode == "dialogue" else character_filter)
    analysis = plan.public(cards) | {"semantic_variants": [], "search_mode": mode}
    limit = finite_number(params.get("limit", 20), "结果数量", 1, 100)
    if limit != int(limit):
        raise UserError("结果数量必须是整数。")
    semantic = _flag(params, "semantic")
    videos = {v["id"]: v for v in store.videos(group_id)}
    all_rows = store.segments(group_id)
    shot_ranges = store.shot_ranges(group_id)
    warnings = []
    if any(len(ids) > 1 for ids in plan.groups):
        warnings.append("查询中的姓名或别名对应多个角色，已保留候选并降低人物加分；使用完整姓名可减少歧义。")
    unavailable = {v["id"] for v in videos.values() if _file_health(v, group) in ("missing", "changed")}
    rows = [r for r in all_rows if r["video_id"] not in unavailable]
    if unavailable and mode != "dialogue":
        warnings.append(f"有{len(unavailable)}个素材的原片、字幕或角色资料发生变化；旧场景索引已跳过，独立台词按自身来源校验。")
    use_dialogue = mode != "scene" and not plan.person_only
    configs = store.subtitle_indexes(group_id) if use_dialogue else {}
    valid_configs = {key: value for key, value in configs.items() if dialogue.healthy(videos[key], value)}
    units = [u for u in store.subtitle_units(group_id) if u["video_id"] in valid_configs] if use_dialogue else []
    if use_dialogue:
        missing = {r["video_id"] for r in all_rows if r["subtitle"] and r["video_id"] not in valid_configs}
        if missing or len(valid_configs) != len(configs) or not units:
            warnings.append("部分素材没有有效的独立台词索引，请选择素材并使用“仅补建台词索引”；现有视觉分析无需重做。")
    analysis["subtitle_unit_count"] = len(units)
    if mode == "dialogue" and not units or mode != "dialogue" and not rows and not units:
        return {"results": [], "warnings": warnings or ["当前分组还没有可检索索引。"], "query_analysis": analysis}
    model = str(ctx.settings.get("embedding_model") or "text-embedding-3-small").strip()
    query_vectors, scene_vectors, subtitle_vectors = {}, {}, {}
    invalid_vectors, incompatible = False, False
    if semantic and plan.embedding_texts:
        sources = [(rows if mode != "dialogue" else [], scene_vectors), (units, subtitle_vectors)]
        for source, target in sources:
            for row in source:
                if not row.get("embedding") or row.get("embedding_model") != model:
                    incompatible = True
                    continue
                try:
                    vector = _vector(json.loads(row["embedding"]))
                except (ValueError, TypeError):
                    vector = None
                if vector is None:
                    invalid_vectors = True
                else:
                    target[row["id"]] = (vector, 1.0)
        if not str(ctx.settings.get("api_key") or "").strip():
            warnings.append("未填写OpenAI密钥，本次使用本地文字匹配。")
        elif not scene_vectors and not subtitle_vectors:
            warnings.append("没有匹配当前模型的语义向量；台词可单独补建语义索引，本次使用文字匹配。")
        else:
            try:
                requested = plan.embedding_texts if scene_vectors else [plan.original]
                vectors, cache_info = cached_embeddings(ctx.workspace, model, requested, lambda: OpenAIClient(ctx.settings),
                    expected_dimensions={len(v) for target in (scene_vectors, subtitle_vectors) for v, _ in target.values()})
                query_vectors = {name: vectors[text] for name, text in plan.variants.items() if text in vectors}
                analysis.update(semantic_variants=list(query_vectors), vector_cache=cache_info)
                if cache_info["cache_write_failed"]:
                    warnings.append("查询完成，但向量缓存不可写；重复查询可能再次请求接口。")
            except UserError as exc:
                warnings.append(str(exc) + " 本次使用文字匹配。")
        if incompatible:
            warnings.append("部分片段或台词没有当前模型的向量，仅参与文字匹配。")
        if invalid_vectors:
            warnings.append("发现无效向量，已跳过其语义评分，请更新对应索引。")
    if query_vectors:
        query_dimension = len(next(iter(query_vectors.values())))
        skipped = False
        for target in (scene_vectors, subtitle_vectors):
            wrong = [key for key, (vector, _) in target.items() if len(vector) != query_dimension]
            for key in wrong:
                del target[key]
            skipped |= bool(wrong)
        if skipped:
            warnings.append("部分向量维度不匹配，已跳过其语义评分，请更新对应索引。")
    scene_hits = _scene_candidates(rows, plan, cards, character_filter, query_vectors, scene_vectors, shot_ranges) if mode != "dialogue" else []
    cap = max(60, int(limit) * 3)
    if mode == "scene" or plan.person_only:
        return {"results": scene_hits[:int(limit)], "warnings": warnings, "query_analysis": analysis}
    scores = {}
    original = query_vectors.get("original")
    if original:
        scores = {key: max(-1, min(1, sum(a * b for a, b in zip(vector, original)))) for key, (vector, _) in subtitle_vectors.items()}
    eligible_units = dialogue_search.filter_character(units, rows, character_filter) if character_filter else units
    selected = dialogue_search.candidates(eligible_units, query, scores, cap)
    # Only use valid source-shot metadata to decorate dialogue hits; identity never comes from words.
    valid_shots = {key: value for key, value in shot_ranges.items() if key not in unavailable}
    dialogue_hits = dialogue_search.as_hits(selected, videos, valid_configs, rows, valid_shots, character_filter)
    results = dialogue_hits[:int(limit)] if mode == "dialogue" else dialogue_search.combine(scene_hits[:cap], dialogue_hits[:cap], int(limit))
    return {"results": results, "warnings": warnings, "query_analysis": analysis}



def handle(command, params, ctx):
    if command == "index.run":
        return _profiled_index(params, ctx)
    if command == "index.dialogue":
        return _profiled_index(params, ctx, subtitle_only=True)
    with Store(ctx.workspace) as store:
        if command == "group.list":
            return {"groups": store.groups()}
        if command in ("group.create", "group.update"):
            name = _text(params.get("name", ""), "分组名称", 120)
            description = _text(params.get("description", ""), "分组说明", 6000, required=False)
            cards = normalize_characters(params["characters"], ctx.workspace) if "characters" in params else None
            return store.save_group(name, description, _id(params, "group_id") if command == "group.update" else None, cards)
        if command == "group.delete":
            store.delete_group(_id(params, "group_id"))
            return {"deleted": True}
        if command == "video.list":
            group_id = _id(params, "group_id")
            group = store.group(group_id)
            subtitle_configs = store.subtitle_indexes(group_id)
            return {"videos": [_video_public(video, group, subtitle_configs.get(video["id"])) for video in store.videos(group_id)]}
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
        if command == "search.run":
            return _search(store, params, ctx)
    raise UserError(f"未知素材库操作：{command}")
