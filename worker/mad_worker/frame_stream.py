"""Bounded FFmpeg frame/PTS stream, with no media output on protocol stdout."""
import math
import os
import queue
import re
import subprocess
import threading
from collections import deque

from .errors import UserError

_FRAME = re.compile(r"\bn:\s*(\d+).*?\bpts_time:\s*([-+\d.eE]+).*?\bs:(\d+)x(\d+)")


def iter_frames(executable, path, max_edge):
    """Yield (decode index, seconds from first frame, BGR ndarray); always close."""
    import numpy as np

    filters = (f"scale=w='min({max_edge},iw)':h='min({max_edge},ih)':"
               "force_original_aspect_ratio=decrease:flags=fast_bilinear,format=bgr24,showinfo")
    args = [executable, "-hide_banner", "-loglevel", "info", "-nostdin", "-i", str(path),
            "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", filters, "-fps_mode", "passthrough",
            "-c:v", "rawvideo", "-threads", "1", "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"]
    try:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except OSError as exc:
        raise UserError("无法启动镜头检测解码器，请检查FFmpeg路径。", "dependency") from exc
    stopped = threading.Event()
    metadata, frames = queue.Queue(8), queue.Queue(2)
    errors = deque(maxlen=8)

    def put(target, value):
        while not stopped.is_set():
            try:
                target.put(value, timeout=0.2)
                return
            except queue.Full:
                pass

    def read_metadata():
        try:
            for raw in iter(process.stderr.readline, b""):
                line = raw.decode("utf-8", errors="replace")
                match = _FRAME.search(line)
                if match:
                    put(metadata, (int(match[1]), float(match[2]), int(match[3]), int(match[4])))
                elif "showinfo" not in line:
                    errors.append(line.strip()[:300])
                if stopped.is_set():
                    return
        except Exception as exc:
            put(metadata, exc)
        finally:
            put(metadata, None)

    def read_pixels():
        try:
            while not stopped.is_set():
                meta = metadata.get(timeout=60)
                if meta is None:
                    break
                if isinstance(meta, Exception):
                    raise meta
                index, pts, width, height = meta
                if not (0 < width <= max_edge and 0 < height <= max_edge and math.isfinite(pts)):
                    raise UserError("镜头解码返回了无效尺寸或时间戳。", "media")
                remaining, chunks = width * height * 3, []
                while remaining:
                    chunk = process.stdout.read(remaining)
                    if not chunk:
                        raise UserError("镜头解码帧数据不完整，请检查视频。", "media")
                    chunks.append(chunk)
                    remaining -= len(chunk)
                put(frames, (index, pts, width, height, b"".join(chunks)))
        except Exception as exc:
            put(frames, exc)
        finally:
            put(frames, None)

    threads = [threading.Thread(target=read_metadata, daemon=True), threading.Thread(target=read_pixels, daemon=True)]
    for thread in threads:
        thread.start()
    origin, previous, expected = None, -1.0, 0
    try:
        while True:
            try:
                item = frames.get(timeout=60)
            except queue.Empty as exc:
                raise UserError("视频顺序解码超过60秒没有返回画面，请检查磁盘或视频文件。", "timeout") from exc
            if item is None:
                break
            if isinstance(item, Exception):
                raise UserError("镜头解码失败，请检查FFmpeg和视频完整性。", "media") from item
            index, pts, width, height, pixels = item
            origin = pts if origin is None else origin
            seconds = pts - origin
            if index != expected or seconds <= previous:
                raise UserError("视频解码时间戳不连续或未递增，请先转码为标准MP4。", "media")
            previous, expected = seconds, index + 1
            yield index, seconds, np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, 3)
        try:
            code = process.wait(timeout=60)
        except subprocess.TimeoutExpired as exc:
            raise UserError("视频解码结束等待超时。", "timeout") from exc
        if code or not expected:
            raise UserError("视频顺序解码失败，请检查编码与完整性。\n" + "\n".join(errors)[-1200:], "media")
    finally:
        stopped.set()
        if process.poll() is None:
            process.kill()
        process.wait()
        for thread in threads:
            thread.join(timeout=2)
        process.stdout.close()
        process.stderr.close()
