"""Shared non-drop timecode and source-frame range contract for cutout clients."""
import math
import re
from dataclasses import dataclass
from fractions import Fraction

from .errors import UserError
from .validation import finite_number

MAX_FRAME_INDEX = 2**53 - 1


def frame_rate(info):
    try:
        rate = Fraction(str(info.get("fps_rational") or info["fps"]))
        if rate <= 0:
            raise ValueError()
    except (ValueError, ZeroDivisionError):
        rate = Fraction(str(finite_number(info.get("fps"), "视频帧率", 1, 120)))
    finite_number(float(rate), "视频帧率", 1, 120)
    return rate


def frame_index(value, label):
    number = finite_number(value, label, 0, MAX_FRAME_INDEX)
    if number != int(number):
        raise UserError(f"{label}必须是从0开始的整数帧号。")
    return int(number)


def nominal_fps(rate):
    return math.ceil(rate)


def format_timecode(index, rate):
    seconds, frames = divmod(index, nominal_fps(rate))
    minutes, seconds = divmod(seconds, 60)
    width = max(2, len(str(nominal_fps(rate) - 1)))
    return f"{minutes:02d}:{seconds:02d}:{frames:0{width}d}"


def position_frame(value, rate, label):
    if isinstance(value, str) and ":" in value:
        match = re.fullmatch(r"([0-9]{1,9}):([0-9]{2}):([0-9]{2,3})", value.strip())
        if not match:
            raise UserError(f"{label}请使用 分钟:秒:帧，例如 01:23:05；也可输入小数秒。")
        minutes, seconds, frames = map(int, match.groups())
        nominal = nominal_fps(rate)
        if seconds >= 60 or frames >= nominal:
            raise UserError(f"{label}的秒必须为00–59，帧必须为0–{nominal - 1}（当前按{nominal}帧编号）。")
        return frame_index((minutes * 60 + seconds) * nominal + frames, label)
    seconds = finite_number(value, label, 0)
    # Fraction avoids an extra frame from binary floating-point rounding.
    return frame_index(math.floor(Fraction(str(seconds)) * rate + Fraction(1, 2)), label)


def total_frames(info, rate):
    known = info.get("frame_count")
    if known is not None:
        count = frame_index(known, "视频总帧数")
        if count > 0:
            return count, False
    duration = finite_number(info.get("duration"), "视频时长", 0)
    return max(1, frame_index(math.ceil(Fraction(str(duration)) * rate), "估计总帧数")), True


@dataclass(frozen=True)
class FrameRange:
    start_frame: int
    end_frame: int
    rate: Fraction
    total_frames: int
    total_frames_estimated: bool

    @property
    def frame_count(self):
        return self.end_frame - self.start_frame

    @property
    def start(self):
        return float(self.start_frame / self.rate)

    @property
    def end(self):
        return float(self.end_frame / self.rate)

    def as_dict(self):
        return {"start_frame": self.start_frame, "end_frame": self.end_frame,
                "start": self.start, "end": self.end, "frame_count": self.frame_count,
                "start_timecode": format_timecode(self.start_frame, self.rate),
                "end_timecode": format_timecode(self.end_frame, self.rate),
                "fps": float(self.rate), "fps_rational": str(self.rate), "nominal_fps": nominal_fps(self.rate),
                "total_frames": self.total_frames, "total_frames_estimated": self.total_frames_estimated}


def resolve_range(params, info, max_seconds=120, max_frames=3600):
    rate = frame_rate(info)
    explicit_frames = any(params.get(key) is not None for key in ("start_frame", "end_frame"))
    if explicit_frames:
        if any(params.get(key) is not None for key in ("start", "end")):
            raise UserError("帧号范围不能与秒数/时间码范围同时填写。")
        start = frame_index(params.get("start_frame"), "起始帧")
        end = frame_index(params.get("end_frame"), "结束帧")
    else:
        start = position_frame(params.get("start"), rate, "起点")
        end = position_frame(params.get("end"), rate, "终点")
    if end <= start:
        raise UserError("结束点必须晚于起点至少一帧；结束帧不包含在输出中。")
    total, estimated = total_frames(info, rate)
    if start >= total or end > total:
        raise UserError(f"帧范围超出视频：起点应小于{total}，终点最多为{total}（{format_timecode(total, rate)}）。")
    if end - start > max_frames or Fraction(end - start, 1) / rate > max_seconds:
        raise UserError(f"单次抠像最多 {max_seconds} 秒 / {max_frames} 帧，请先裁短片段。")
    return FrameRange(start, end, rate, total, estimated)
