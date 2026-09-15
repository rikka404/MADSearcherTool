"""Small OpenAI HTTP boundary, shared by search and target localization."""
import json
import math
import time
import urllib.error
import urllib.request

from .errors import UserError
from .images import ImagePreparer, INDEX_IMAGE_POLICY
from .profiling import measure
from .prompts import CLIP_CHARACTER_PROMPT


class OpenAIClient:
    def __init__(self, settings, profile=None, *, image_policy=INDEX_IMAGE_POLICY):
        self.key = str(settings.get("api_key") or "").strip()
        if not self.key:
            raise UserError("此操作需要 OpenAI API 密钥，请在设置中填写；纯字幕检索不需要密钥。", "configuration")
        if "\n" in self.key or "\r" in self.key:
            raise UserError("API 密钥不能包含换行，请重新粘贴。", "configuration")
        self.vision_model = str(settings.get("vision_model") or "gpt-4.1-mini").strip()
        self.embedding_model = str(settings.get("embedding_model") or "text-embedding-3-small").strip()
        self.profile = profile
        self.images = ImagePreparer(profile, policy=image_policy)
        self._vision_metadata = {}

    def _diagnostic_scalar(self, value, limit=128):
        """Only bounded scalar previews, with secrets removed before truncation."""
        if isinstance(value, str):
            return value.replace(self.key, "[已隐藏密钥]")[:limit]
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float) and math.isfinite(value):
            return value
        return "<" + type(value).__name__ + ">"

    def _validation_error(self, phase, reason, message, **details):
        if self.profile:
            self.profile.validation_failure(phase=phase, reason=reason, model=self.vision_model,
                                            response=self._vision_metadata, **details)
        hint = "详见索引日志。" if self.profile else "请重试。"
        raise UserError(f"{message}（{reason}）。{hint}", "api")

    def _post(self, endpoint, payload):
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        request = urllib.request.Request("https://api.openai.com/v1/" + endpoint, data=body,
                                         headers={"Authorization": "Bearer " + self.key,
                                                  "Content-Type": "application/json"}, method="POST")
        for attempt in range(3):
            try:
                with measure(self.profile, "api." + endpoint, attempt=attempt + 1):
                    with urllib.request.urlopen(request, timeout=120) as response:
                        result = json.loads(response.read(16 * 1024 * 1024).decode("utf-8"))
                    if not isinstance(result, dict):
                        raise ValueError("Response must be an object")
                if self.profile:
                    self.profile.api_usage(endpoint, str(payload.get("model", "")), result)
                return result
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                    with measure(self.profile, "api.retry_wait", attempt=attempt + 1):
                        time.sleep(1.5 * 2 ** attempt)
                    continue
                messages = {401: "OpenAI 密钥无效，请检查设置。", 403: "OpenAI 账户无权执行此请求，请检查项目权限。",
                            404: "OpenAI 模型不存在或账户无权使用，请检查模型名称。",
                            429: "OpenAI 配额不足或请求过于频繁，请检查余额或稍后重试。"}
                raise UserError(messages.get(exc.code, f"OpenAI 请求失败（HTTP {exc.code}），请检查模型配置并重试。"), "api")
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt < 2:
                    with measure(self.profile, "api.retry_wait", attempt=attempt + 1):
                        time.sleep(1.5 * 2 ** attempt)
                    continue
                raise UserError("无法连接 OpenAI 或请求超时，请检查网络及系统代理是否正在运行后重试；已完成的索引仍保留。", "network")
            except (ValueError, UnicodeError):
                raise UserError("OpenAI 返回了无法解析的响应，请稍后重试。", "api")

    def vision_json(self, prompt, image_paths, schema, name="analysis", *, reference_text="", reference_images=()):
        self._vision_metadata = {}
        if not image_paths:
            raise UserError("视觉分析至少需要一张图像。")
        content = []
        if reference_text:
            content.append({"type": "input_text", "text": reference_text})
        for character_id, path in reference_images:
            content.append({"type": "input_text", "text": "角色参考图，character_id=" + character_id})
            content.append({"type": "input_image", "image_url": self.images.prepare(path), "detail": self.images.policy.detail})
        content.append({"type": "input_text", "text": str(prompt)})
        for path in image_paths:
            content.append({"type": "input_image", "image_url": self.images.prepare(path), "detail": self.images.policy.detail})
        response = self._post("responses", {"model": self.vision_model, "store": False,
            "input": [{"role": "user", "content": content}], "max_output_tokens": 4096 if name == "clip_caption" else 1800,
            "text": {"format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}}})
        output = response.get("output", [])
        parts, output_types, part_types, invalid_content = [], [], [], 0
        refused = False
        if isinstance(output, list):
            for item in output:
                output_types.append(self._diagnostic_scalar(item.get("type"), 32) if isinstance(item, dict) else "<" + type(item).__name__ + ">")
                if not isinstance(item, dict) or item.get("type") != "message":
                    continue
                content_parts = item.get("content", [])
                if not isinstance(content_parts, list):
                    invalid_content += 1
                    continue
                for part in content_parts:
                    if not isinstance(part, dict):
                        continue
                    part_types.append(self._diagnostic_scalar(part.get("type"), 32))
                    refused |= part.get("type") == "refusal"
                    if part.get("type") == "output_text":
                        parts.append(part.get("text", ""))
        incomplete = response.get("incomplete_details")
        self._vision_metadata = {
            "response_id": self._diagnostic_scalar(response.get("id")), "schema": name,
            "status": self._diagnostic_scalar(response.get("status"), 32),
            "incomplete_reason": self._diagnostic_scalar(incomplete.get("reason"), 64) if isinstance(incomplete, dict) else None,
            "output_type": type(output).__name__, "output_item_count": len(output) if isinstance(output, list) else None,
            "output_types": output_types[:16], "part_types": part_types[:32],
            "invalid_message_content_count": invalid_content, "refusal_present": refused,
            "output_text_part_count": len(parts), "output_text_chars": sum(len(p) for p in parts if isinstance(p, str))}
        if self.profile:
            self.profile.event("vision_response_received", **self._vision_metadata)
        if response.get("status") not in (None, "completed"):
            self._validation_error("response", "response_not_completed", "视觉模型未完成分析")
        if not isinstance(output, list):
            self._validation_error("response", "response_output_type", "视觉响应的output不是数组")
        if not parts:
            self._validation_error("response", "model_refusal" if refused else "output_text_missing",
                                   "视觉模型拒绝了请求" if refused else "视觉模型没有返回结构化文本")
        if any(not isinstance(part, str) for part in parts):
            self._validation_error("response", "output_text_type", "视觉响应的文本字段类型错误")
        try:
            result = json.loads("".join(parts))
        except ValueError as exc:
            self._validation_error("response", "structured_json_invalid", "视觉模型返回的JSON无法解析",
                                   json_error_line=getattr(exc, "lineno", None), json_error_column=getattr(exc, "colno", None))
        if not isinstance(result, dict):
            self._validation_error("response", "structured_result_type", "视觉分析结果不是JSON对象",
                                   result_type=type(result).__name__)
        return result

    def caption(self, image_paths, subtitle="", context=""):
        return self.analyze_clip(image_paths, subtitle, context)["caption"]

    def analyze_clip(self, image_paths, subtitle="", context="", characters=(), *, sample_times=None, clip_range=None):
        from .characters import active_characters
        cards = active_characters(characters)
        allowed = {c["id"] for c in cards}
        schema = {"type": "object", "properties": {
            "description": {"type": "string"}, "characters": {"type": "array", "items": {"type": "string"}},
            "scene": {"type": "array", "items": {"type": "string"}}, "actions": {"type": "array", "items": {"type": "string"}}},
            "required": ["description", "characters", "scene", "actions", "character_matches", "unknown_characters"], "additionalProperties": False}
        schema["properties"]["unknown_characters"] = {"type": "array", "items": {"type": "string"}}
        schema["properties"]["character_matches"] = {"type": "array", "items": {"type": "object", "properties": {
            "character_id": {"type": "string", "enum": sorted(allowed) or ["__none__"]},
            "confidence": {"type": "string", "enum": ["high", "medium"]},
            "evidence": {"type": "string"},
            "frame_indices": {"type": "array", "items": {"type": "integer", "enum": list(range(len(image_paths)))}}},
            "required": ["character_id", "confidence", "evidence", "frame_indices"], "additionalProperties": False}}
        reference_text = CLIP_CHARACTER_PROMPT + "\n作品与角色资料JSON：" + json.dumps({
            "work_background": context[:6000], "characters": [
                {k: v for k, v in c.items() if k != "reference_images"} for c in cards]}, ensure_ascii=False)
        references = [(c["id"], path) for c in cards for path in c["reference_images"]]
        prompt = f"以下{len(image_paths)}张才是当前片段采样图，按时间先后、从0开始编号。\n片段字幕JSON：" + json.dumps(subtitle[:6000], ensure_ascii=False)
        if sample_times is not None:
            if len(sample_times) != len(image_paths):
                raise UserError("采样图片和时间戳数量不一致。")
            prompt += "\n片段范围与采样位置JSON：" + json.dumps({"clip_range_seconds": clip_range,
                "samples": [{"frame_index": i, "source_seconds": t} for i, t in enumerate(sample_times)]}, allow_nan=False)
        result = self.vision_json(prompt, image_paths, schema, "clip_caption", reference_text=reference_text, reference_images=references)
        return self._validate_clip_result(result, allowed, len(image_paths))

    def _validate_clip_result(self, result, allowed, frame_count):
        """Preserve strict acceptance rules while explaining every inspected violation."""
        issues, previews = [], []
        issue_count = 0

        def issue(reason, field, message):
            nonlocal issue_count
            issue_count += 1
            if len(issues) < 128:
                issues.append({"reason": reason, "field": field, "message": message})

        description = result.get("description")
        tag_keys = ("characters", "scene", "actions", "unknown_characters")
        tags = [result.get(key) for key in tag_keys]
        tag_summary = {}
        if not isinstance(description, str):
            issue("description_type", "description", "画面描述不是文本")
        elif not description.strip():
            issue("description_empty", "description", "画面描述为空")
        for key, values in zip(tag_keys, tags):
            tag_summary[key] = {"type": type(values).__name__, "count": len(values) if isinstance(values, list) else None}
            if not isinstance(values, list):
                issue("tag_list_type", key, f"{key}不是数组")
            elif any(not isinstance(v, str) for v in values):
                issue("tag_item_type", key, f"{key}包含非文本元素")
        matches = result.get("character_matches")
        if not isinstance(matches, list):
            issue("matches_type", "character_matches", "角色关联不是数组")
        elif len(matches) > len(allowed):
            issue("match_count_exceeds_candidates", "character_matches", "返回的角色关联数量超过候选角色数量")
        seen = {}
        # Validate every returned entry, but keep diagnostic previews bounded.
        inspected = matches if isinstance(matches, list) else []
        for index, item in enumerate(inspected):
            field = f"character_matches[{index}]"
            if not isinstance(item, dict):
                issue("match_not_object", field, f"第{index + 1}条角色关联不是对象")
                if len(previews) < 64:
                    previews.append({"match_index": index, "type": type(item).__name__})
                continue
            key, indices = item.get("character_id"), item.get("frame_indices")
            evidence, confidence = item.get("evidence"), item.get("confidence")
            duplicate_of = seen.get(key) if isinstance(key, str) else None
            if len(previews) < 64:
                previews.append({"match_index": index, "character_id": self._diagnostic_scalar(key),
                    "character_id_type": type(key).__name__, "character_id_chars": len(key) if isinstance(key, str) else None,
                    "known_id": isinstance(key, str) and key in allowed, "duplicate_of": duplicate_of,
                    "confidence": self._diagnostic_scalar(confidence, 32), "confidence_type": type(confidence).__name__,
                    "evidence_type": type(evidence).__name__, "evidence_chars": len(evidence) if isinstance(evidence, str) else None,
                    "evidence_blank": not evidence.strip() if isinstance(evidence, str) else None,
                    "frame_indices_type": type(indices).__name__, "frame_indices_count": len(indices) if isinstance(indices, list) else None,
                    "frame_indices_preview": [{"type": type(v).__name__, "value": self._diagnostic_scalar(v, 32)} for v in indices[:16]] if isinstance(indices, list) else [],
                    "frame_indices_truncated": isinstance(indices, list) and len(indices) > 16})
            if not isinstance(key, str):
                issue("character_id_type", field + ".character_id", f"第{index + 1}条关联的角色ID不是文本")
            else:
                if key not in allowed:
                    issue("character_id_unknown", field + ".character_id", f"第{index + 1}条关联的角色ID不在候选列表中")
                if duplicate_of is not None:
                    issue("character_id_duplicate", field + ".character_id", f"第{index + 1}条关联重复使用第{duplicate_of + 1}条的角色ID")
                seen.setdefault(key, index)
            if confidence not in ("high", "medium"):
                issue("confidence_invalid", field + ".confidence", f"第{index + 1}条关联的置信等级无效")
            if not isinstance(evidence, str):
                issue("evidence_type", field + ".evidence", f"第{index + 1}条关联的识别依据不是文本")
            elif not evidence.strip():
                issue("evidence_empty", field + ".evidence", f"第{index + 1}条关联的识别依据为空")
            elif len(evidence) > 1000:
                issue("evidence_too_long", field + ".evidence", f"第{index + 1}条关联的识别依据超过1000字")
            if not isinstance(indices, list):
                issue("frame_indices_type", field + ".frame_indices", f"第{index + 1}条关联的采样帧序号不是数组")
            elif not indices:
                issue("frame_indices_empty", field + ".frame_indices", f"第{index + 1}条关联没有提供采样帧序号")
            else:
                if any(type(i) is not int for i in indices):
                    issue("frame_index_type", field + ".frame_indices", f"第{index + 1}条关联的采样帧序号包含非整数")
                if any(type(i) is int and not 0 <= i < frame_count for i in indices):
                    issue("frame_index_out_of_range", field + ".frame_indices", f"第{index + 1}条关联的采样帧序号超出0至{frame_count - 1}范围")
        if issues:
            self._validation_error("clip_analysis", issues[0]["reason"], "视觉分析校验失败：" + issues[0]["message"],
                sample_frame_count=frame_count, allowed_character_ids=sorted(allowed),
                description_type=type(description).__name__, description_chars=len(description) if isinstance(description, str) else None,
                tag_fields=tag_summary, matches_type=type(matches).__name__,
                match_count=len(matches) if isinstance(matches, list) else None,
                matches_preview=previews, matches_truncated=isinstance(matches, list) and len(matches) > len(previews),
                issues=issues, issue_count=issue_count, issues_truncated=issue_count > len(issues))
        for item in matches:
            item["frame_indices"] = sorted(set(item["frame_indices"]))
        if self.profile:
            self.profile.event("clip_analysis_validated", response_id=self._vision_metadata.get("response_id"),
                match_count=len(matches), matched_character_ids=[m["character_id"] for m in matches],
                unknown_character_count=len(result["unknown_characters"]), candidate_count=len(allowed), sample_frame_count=frame_count)
        return {"caption": "；".join([description.strip()] + ["、".join(t) for t in tags if t])[:8000], "character_matches": matches}

    def embed(self, texts):
        if not texts or any(not isinstance(t, str) or not t.strip() for t in texts):
            raise UserError("语义向量输入不能为空。")
        vectors = []
        for start in range(0, len(texts), 32):
            batch = [text[:12000] for text in texts[start:start + 32]]
            response = self._post("embeddings", {"model": self.embedding_model, "input": batch, "encoding_format": "float"})
            try:
                rows = sorted(response["data"], key=lambda item: item["index"])
                if len(rows) != len(batch) or [r["index"] for r in rows] != list(range(len(batch))):
                    raise ValueError()
                for row in rows:
                    vector = row["embedding"]
                    if not isinstance(vector, list) or not vector or len(vector) > 65536 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector):
                        raise ValueError()
                    norm = math.sqrt(sum(v * v for v in vector))
                    if norm <= 0 or not math.isfinite(norm):
                        raise ValueError()
                    vectors.append([v / norm for v in vector])
            except (KeyError, ValueError, TypeError):
                raise UserError("OpenAI 返回了无效的语义向量，请重试。", "api")
        if any(len(v) != len(vectors[0]) for v in vectors):
            raise UserError("OpenAI 返回的向量维度不一致，请检查 embedding 模型。", "api")
        return vectors
