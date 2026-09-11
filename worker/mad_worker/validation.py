import math
from pathlib import Path
from .errors import UserError


def finite_number(value, label, minimum=None, maximum=None):
    try:
        if isinstance(value, bool):
            raise ValueError()
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        raise UserError(f"{label}必须是有效数字。")
    if not math.isfinite(number):
        raise UserError(f"{label}不能为无穷大或 NaN。")
    if minimum is not None and number < minimum:
        raise UserError(f"{label}不能小于 {minimum}。")
    if maximum is not None and number > maximum:
        raise UserError(f"{label}不能大于 {maximum}。")
    return number


def existing_file(value, label="文件", suffixes=None):
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise UserError(f"请选择{label}。")
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise UserError(f"{label}不存在或不是文件：{path}")
    if suffixes and path.suffix.lower() not in suffixes:
        raise UserError(f"{label}格式不支持，请选择 {' / '.join(suffixes)}。")
    return path


def time_range(start, end, duration):
    start = finite_number(start, "开始时间", 0)
    end = finite_number(end, "结束时间", 0)
    if end <= start:
        raise UserError("结束时间必须大于开始时间。")
    if end > duration + 0.02:
        raise UserError(f"结束时间超过视频时长（{duration:.3f} 秒）。")
    return start, min(end, duration)
