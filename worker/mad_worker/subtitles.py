"""Strict, dependency-free subtitle import. Times always refer to source seconds."""
import html
import math
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import UserError
from .validation import existing_file, finite_number


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str

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


def _clean(text, ass=False):
    if ass:
        text = re.sub(r"\{[^}]*\}", "", text)
        text = text.replace(r"\N", "\n").replace(r"\n", "\n").replace(r"\h", " ")
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


def parse_subtitles(path, offset=0, duration=None):
    path = existing_file(path, "字幕", {".srt", ".vtt", ".ass", ".ssa"})
    if path.stat().st_size > 20 * 1024 * 1024:
        raise UserError("字幕超过20MB，请确认选择的是文本字幕文件。")
    offset = finite_number(offset, "字幕偏移", -86400, 86400)
    text = _decode(path.read_bytes()).replace("\r\n", "\n").replace("\r", "\n")
    raw_cues = []
    if path.suffix.lower() in (".ass", ".ssa"):
        fields = ["layer", "start", "end", "style", "name", "marginl", "marginr", "marginv", "effect", "text"]
        events = False
        for lineno, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if line.startswith("["):
                events = line.lower() == "[events]"
            elif events and line.lower().startswith("format:"):
                fields = [f.strip().lower() for f in line.split(":", 1)[1].split(",")]
                if not {"start", "end", "text"}.issubset(fields) or fields[-1] != "text":
                    raise UserError(f"字幕第{lineno}行的 ASS Format 缺少 Start/End/Text，或 Text 不在最后。")
            elif events and line.lower().startswith("dialogue:"):
                values = line.split(":", 1)[1].split(",", len(fields) - 1)
                if len(values) != len(fields):
                    raise UserError(f"字幕第{lineno}行的 ASS Dialogue 字段数量不正确。")
                record = dict(zip(fields, values))
                raw_cues.append((record["start"], record["end"], _clean(record["text"], True), lineno))
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
            raw_cues.append((left, right, _clean("\n".join(lines[timing + 1:])), len(raw_cues) + 1))
    if not raw_cues:
        raise UserError("字幕中没有可识别的台词，请选择有效的 SRT、VTT 或 ASS 文件。")
    result, clipped = [], 0
    for start, end, words, position in raw_cues:
        try:
            start, end = _clock(start), _clock(end)
            if not math.isfinite(start) or not math.isfinite(end) or end < start:
                raise ValueError("起止时间必须为有限值，结束时间不能早于开始时间")
        except (ValueError, OverflowError) as exc:
            raise UserError(f"字幕第{position}项时间轴无效：{exc}。")
        start, end = start + offset, end + offset
        if end < 0 or end == 0 and start < 0 or duration is not None and start >= duration:
            clipped += 1
            continue
        if words:
            result.append(Cue(max(0, start), min(duration, end) if duration is not None else end, words))
    result.sort(key=lambda cue: (cue.start, cue.end))
    if not result:
        raise UserError("偏移后的字幕没有落在视频时间范围内的台词，请检查偏移量和字幕对应集数。")
    warnings = [f"有{clipped}条字幕位于视频时间范围外，已忽略；请核对字幕版本及偏移。"] if clipped else []
    return result, warnings
