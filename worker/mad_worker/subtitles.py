"""Strict, dependency-free subtitle import. Times always refer to source seconds."""
import html
import math
import re
from dataclasses import dataclass, replace

from .errors import UserError
from .validation import existing_file, finite_number

ASS_CLEANUP_VERSION = 1
_OVERRIDE = re.compile(r"\{([^{}]*)\}")
_DRAWING_TAG = re.compile(r"\(|\)|\\p\s*([+-]?\d+)(?=\s|\\|$)|\\r[^\\()]*")
_FRAME_SECONDS = 0.12
_JOIN_SECONDS = 0.05
_EPSILON = 1e-7


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str
    lane: str = ""
    lines: tuple[str, ...] = ()

    def overlaps(self, start, end):
        """Use half-open windows; a zero-duration cue belongs to its timestamp."""
        if self.start == self.end:
            return start <= self.start < end
        return self.start < end and self.end > start


def _clock(value):
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    if len(parts) not in (2, 3):
        raise ValueError("时间格式应为 时:分:秒 或 分:秒")
    if any(not re.fullmatch(r"\d+(?:\.\d+)?", item) for item in parts):
        raise ValueError("时间含有非法字符")
    numbers = list(map(float, parts))
    if any(not part.isdigit() for part in parts[:-1]):
        raise ValueError("小时和分钟必须是整数")
    if any(n >= 60 for n in numbers[1:]):
        raise ValueError("时间中的分或秒必须小于60")
    if len(numbers) == 2 and numbers[0] >= 60:
        raise ValueError("分钟超过59时请包含小时")
    return sum(n * 60 ** i for i, n in enumerate(reversed(numbers)))


def _ass_visible(text):
    """Read drawing state in tag order, across line breaks and override blocks.

    Tags inside parentheses (clip/transform arguments) do not change the
    top-level drawing state. In particular, vector clips still display text.
    """
    visible, drawing, removed, position = [], False, 0, 0
    for block in _OVERRIDE.finditer(text):
        span = text[position:block.start()]
        if drawing:
            removed += bool(span.strip())
        else:
            visible.append(span)
        depth = 0
        for tag in _DRAWING_TAG.finditer(block.group(1)):
            value = tag.group(0)
            if value == "(":
                depth += 1
            elif value == ")":
                depth = max(0, depth - 1)
            elif not depth:
                if tag.group(1) is None:  # \\r resets overrides, including drawing.
                    drawing = False
                else:
                    number = tag.group(1)
                    drawing = not number.startswith("-") and bool(number.lstrip("+0"))
        position = block.end()
    span = text[position:]
    if drawing:
        removed += bool(span.strip())
    else:
        visible.append(span)
    plain = "".join(visible).replace(r"\N", "\n").replace(r"\n", "\n").replace(r"\h", " ")
    return plain, removed


def _clean(text):
    text = re.sub(r"<[^>]*>", "", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _decode(raw):
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings = ("utf-16",)
    else:
        encodings = ("utf-8-sig", "gb18030", "shift_jis")
    for encoding in encodings:
        try:
            text = raw.decode(encoding)
            if "\x00" in text:
                continue
            return text
        except UnicodeError:
            pass
    raise UserError("字幕编码无法识别，请另存为 UTF-8 后重新导入。")


def _source_lines(text):
    return tuple(line for part in text.splitlines() if (line := _clean(part)))


@dataclass(frozen=True)
class _AssEvent:
    cue: Cue
    style: str
    actor: str
    fx: bool

    def key(self):
        return self.style, self.actor, self.cue.text, self.cue.lines


def _deduplicate_events(events, stats):
    unique = {}
    for event in events:
        key = (*event.key(), event.cue.start, event.cue.end)
        previous = unique.get(key)
        if previous is None:
            unique[key] = event
        else:
            stats["duplicates_removed"] += 1
            # Keep a normal base event conservative if a duplicate FX layer exists.
            if previous.fx and not event.fx:
                unique[key] = event
    return list(unique.values())


def _compact_ass(events, stats):
    groups = {}
    for event in _deduplicate_events(events, stats):
        # A normal base layer must not interrupt its independently animated layers.
        groups.setdefault((*event.key(), event.fx), []).append(event)
    result = []
    for group in groups.values():
        run, kind, end = [], "", 0

        def flush():
            if len(run) >= (2 if kind == "fx" else 3):
                first = run[0]
                result.append(replace(first, cue=replace(first.cue, end=end)))
                stats["effects_merged"] += len(run) - 1
            else:
                result.extend(run)

        for event in sorted(group, key=lambda e: (e.cue.start, e.cue.end)):
            duration = event.cue.end - event.cue.start
            current = "fx" if event.fx else "short" if 0 < duration <= _FRAME_SECONDS + _EPSILON else "normal"
            if run and (current == "normal" or current != kind or event.cue.start > end + _JOIN_SECONDS + _EPSILON):
                flush()
                run = []
            if not run:
                kind, end = current, event.cue.end
            run.append(event)
            end = max(end, event.cue.end)
        flush()
    # A compacted FX run can now coincide with a full-duration normal base layer.
    return [event.cue for event in _deduplicate_events(result, stats)]


def parse_subtitles(path, offset=0, duration=None, *, diagnostics=None):
    """Return visible cues and notices; optional diagnostics contains counts only."""
    path = existing_file(path, "字幕", {".srt", ".vtt", ".ass", ".ssa"})
    if path.stat().st_size > 20 * 1024 * 1024:
        raise UserError("字幕超过20MB，请确认选择的是文本字幕文件。")
    offset = finite_number(offset, "字幕偏移", -86400, 86400)
    text = _decode(path.read_bytes()).replace("\r\n", "\n").replace("\r", "\n")
    raw_cues = []
    ass = path.suffix.lower() in (".ass", ".ssa")
    stats = diagnostics if diagnostics is not None else {}
    stats.update(format=path.suffix.lower()[1:], cleanup_version=ASS_CLEANUP_VERSION if ass else None,
                 raw_events=0, ignored_comments=0, drawing_only_removed=0, drawing_spans_removed=0,
                 empty_removed=0, duplicates_removed=0, effects_merged=0, clipped_cues=0, effective_cues=0)
    if ass:
        fields = ["layer", "start", "end", "style", "name", "marginl", "marginr", "marginv", "effect", "text"]
        in_events = False
        for lineno, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if line.startswith("["):
                in_events = line.lower() == "[events]"
            elif in_events and line.lower().startswith("format:"):
                fields = [f.strip().lower() for f in line.split(":", 1)[1].split(",")]
                if not {"start", "end", "text"}.issubset(fields) or fields[-1] != "text":
                    raise UserError(f"字幕第{lineno}行的 ASS Format 缺少 Start/End/Text，或 Text 不在最后。")
            elif in_events and line.lower().startswith("dialogue:"):
                values = line.split(":", 1)[1].split(",", len(fields) - 1)
                if len(values) != len(fields):
                    raise UserError(f"字幕第{lineno}行的 ASS Dialogue 字段数量不正确。")
                record = dict(zip(fields, values))
                visible, removed = _ass_visible(record["text"])
                words = _clean(visible)
                stats["drawing_spans_removed"] += removed
                stats["drawing_only_removed"] += bool(removed and not words)
                raw_cues.append((record["start"], record["end"], words, lineno,
                                 record.get("style", "").strip(), _source_lines(visible),
                                 record.get("name", record.get("actor", "")).strip(), record.get("effect", "").strip().lower() == "fx"))
            elif in_events and line.lower().startswith("comment:"):
                stats["ignored_comments"] += 1
    else:
        for block in re.split(r"\n\s*\n", text.strip()):
            lines = block.strip().splitlines()
            if not lines or lines[0].startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
                continue
            timing = next((i for i, line in enumerate(lines) if "-->" in line), None)
            if timing is None:
                if any(line.strip() for line in lines):
                    raise UserError("字幕包含没有时间轴的内容块，请检查 SRT/VTT 格式。")
                continue
            left, right = lines[timing].split("-->", 1)
            right = right.strip().split()[0] if right.strip() else ""
            words = "\n".join(lines[timing + 1:])
            raw_cues.append((left, right, _clean(words), len(raw_cues) + 1, "", _source_lines(words), "", False))
    stats["raw_events"] = len(raw_cues)
    if not raw_cues:
        raise UserError("字幕中没有可识别的台词，请选择有效的 SRT、VTT 或 ASS 文件。")
    valid, ass_events = [], []
    for start, end, words, position, lane, lines, actor, fx in raw_cues:
        try:
            start, end = _clock(start), _clock(end)
            if not math.isfinite(start) or not math.isfinite(end) or end < start:
                raise ValueError("起止时间必须为有限值，结束时间不能早于开始时间")
        except (ValueError, OverflowError) as exc:
            raise UserError(f"字幕第{position}项时间轴无效：{exc}。")
        if words:
            cue = Cue(start, end, words, lane[:120], lines)
            if ass:
                ass_events.append(_AssEvent(cue, lane, actor, fx))
            else:
                valid.append(cue)
        else:
            stats["empty_removed"] += 1
    stats["empty_removed"] -= stats["drawing_only_removed"]
    if ass:
        valid = _compact_ass(ass_events, stats)
    if not valid:
        raise UserError("字幕中没有可索引的可见文本；绘图、空行及Comment不会作为台词，请检查字幕内容。")
    result, clipped = [], 0
    for cue in valid:
        start, end = cue.start + offset, cue.end + offset
        if end < 0 or end == 0 and start < 0 or duration is not None and start >= duration:
            clipped += 1
            continue
        result.append(replace(cue, start=max(0, start), end=min(duration, end) if duration is not None else end))
    stats.update(clipped_cues=clipped, effective_cues=len(result))
    result.sort(key=lambda cue: (cue.start, cue.end))
    if not result:
        raise UserError("偏移后的字幕没有落在视频时间范围内的台词，请检查偏移量和字幕对应集数。")
    warnings = [f"有{clipped}条字幕位于视频时间范围外，已忽略；请核对字幕版本及偏移。"] if clipped else []
    if ass and any(stats[key] for key in ("drawing_spans_removed", "duplicates_removed", "effects_merged")):
        warnings.append(f"ASS清理：原始{stats['raw_events']}个显示事件，过滤{stats['drawing_only_removed']}个纯绘图事件、"
                        f"去重{stats['duplicates_removed']}个重复层、合并减少{stats['effects_merged']}个特效事件；"
                        f"时间范围内保留{len(result)}条有效字幕。")
    return result, warnings
