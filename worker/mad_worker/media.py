"""FFmpeg boundary: literal argument lists, bounded calls, no source mutations."""
import json
import math
import os
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from .errors import UserError
from .validation import existing_file, finite_number, time_range


class Media:
    def __init__(self, settings=None):
        self.settings = settings or {}

    def iter_index_frames(self, path, max_edge):
        from .frame_stream import iter_frames
        return iter_frames(self.executable("ffmpeg"), path, max_edge)

    def executable(self, name):
        configured = str(self.settings.get(name + "_path") or name).strip()
        resolved = shutil.which(configured)
        if not resolved:
            raise UserError(f"找不到 {name}，请在设置中选择正确的 {name}.exe。", "dependency")
        return resolved

    def _call(self, command, timeout=300):
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=timeout,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except subprocess.TimeoutExpired:
            raise UserError("视频处理超时，请尝试较短片段或检查磁盘速度。", "timeout")
        except OSError as exc:
            raise UserError(f"无法启动视频工具：{exc}", "dependency")
        if result.returncode:
            detail = result.stderr.strip()[-1600:]
            raise UserError(f"视频处理失败。请确认文件完整、编码受支持且输出可写。\n{detail}", "media")
        return result

    def run(self, args, timeout=600):
        return self._call([self.executable("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", *map(str, args)], timeout)

    def probe(self, path):
        path = existing_file(path, "视频")
        result = self._call([self.executable("ffprobe"), "-v", "error", "-show_streams",
                             "-show_format", "-of", "json", str(path)], 60)
        try:
            data = json.loads(result.stdout)
            streams = data.get("streams", [])
            video = next((s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), None)
            if video is None:
                raise UserError("所选文件没有可用视频流。")
            duration = float(video.get("duration") or data.get("format", {}).get("duration") or 0)
            fps_raw = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0"
            try:
                fps = float(Fraction(fps_raw))
                if fps <= 0:
                    raise ValueError()
            except (ValueError, ZeroDivisionError):
                fps_raw = video.get("r_frame_rate", "0")
                fps = float(Fraction(fps_raw))
            width, height = int(video["width"]), int(video["height"])
            if not math.isfinite(duration) or duration <= 0 or not math.isfinite(fps) or fps <= 0 or width <= 0 or height <= 0:
                raise ValueError()
        except (ValueError, KeyError, TypeError, ZeroDivisionError):
            raise UserError("无法读取有效的视频时长、尺寸或帧率，请先转码为标准 MP4。", "media")
        count_raw = str(video.get("nb_frames") or "")
        count = int(count_raw) if count_raw.isdigit() and int(count_raw) > 0 else None
        return {"path": str(path), "duration": duration, "width": width, "height": height,
                "fps": fps, "has_audio": any(s.get("codec_type") == "audio" for s in streams),
                "codec": video.get("codec_name", "unknown"), "fps_rational": fps_raw, "frame_count": count}

    def extract_frame_index(self, path, index, output):
        from .timecode import frame_index
        source = existing_file(path, "视频")
        index = frame_index(index, "帧号")
        output = Path(output).resolve()
        if output == source:
            raise UserError("输出不能覆盖原视频。")
        output.parent.mkdir(parents=True, exist_ok=True)
        self.run(["-y", "-i", source, "-map", "0:v:0", "-an",
                  "-vf", f"trim=start_frame={index}:end_frame={index + 1},setpts=PTS-STARTPTS",
                  "-fps_mode", "passthrough", "-frames:v", "1", "-update", "1", output], timeout=1800)
        if not output.is_file() or output.stat().st_size == 0:
            raise UserError("没有解码到指定帧，请检查帧号是否超出实际视频范围。", "media")
        return str(output)

    def extract_frame(self, path, seconds, output, *, max_edge=None):
        source = existing_file(path, "视频")
        seconds = finite_number(seconds, "取帧时间", 0)
        output = Path(output).resolve()
        if output == source:
            raise UserError("输出不能覆盖原视频。")
        output.parent.mkdir(parents=True, exist_ok=True)
        filters = []
        if max_edge is not None:
            edge = int(finite_number(max_edge, "采样图片长边", 16, 4096))
            filters = ["-vf", f"scale=w='min({edge},iw)':h='min({edge},ih)':force_original_aspect_ratio=decrease"]
        self.run(["-y", "-ss", f"{seconds:.6f}", "-i", source, "-map", "0:v:0", *filters,
                  "-frames:v", "1", "-update", "1", output])
        if not output.is_file() or output.stat().st_size == 0:
            raise UserError("该时间点未能读取画面，请将时间向前调整一帧。", "media")
        return str(output)

    def extract_frames(self, path, start, end, fps, directory):
        info = self.probe(path)
        start, end = time_range(start, end, info["duration"])
        fps = finite_number(fps, "输出帧率", 1, 120)
        directory = Path(directory).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        if list(directory.iterdir()):
            raise UserError("帧缓存目录必须为空，避免混入上次任务帧。")
        self.run(["-n", "-ss", f"{start:.6f}", "-i", info["path"], "-t", f"{end-start:.6f}",
                  "-map", "0:v:0", "-an", "-vf", f"fps={fps:.10g}", "-start_number", "0", "-q:v", "2", directory / "%06d.jpg"], 900)
        frames = sorted(directory.glob("*.jpg"))
        if not frames:
            raise UserError("片段没有可解码画面，请延长片段至少一帧。", "media")
        return frames

    def make_preview(self, path, start, end, output):
        info = self.probe(path)
        start, end = time_range(start, end, info["duration"])
        output = Path(output).resolve()
        if output == Path(info["path"]):
            raise UserError("输出不能覆盖原视频。")
        output.parent.mkdir(parents=True, exist_ok=True)
        self.run(["-y", "-ss", f"{start:.6f}", "-i", info["path"], "-t", f"{end-start:.6f}",
                  "-map", "0:v:0", "-map", "0:a:0?", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                  "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                  "-c:a", "aac", "-movflags", "+faststart", output], 900)
        return str(output)
