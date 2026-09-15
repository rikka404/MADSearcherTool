"""Versioned shot detection and frame sampling, independent of roles and cloud models."""
import bisect
import hashlib
import json
import math
from contextlib import closing
from pathlib import Path

from .errors import UserError
from .validation import finite_number

DETECTOR_VERSION = "adaptive_0.6.6_pts_flash_v1"
SAMPLING_VERSION = "duration_motion_dedup_v1"
MAX_SEGMENTS = 20000
MAX_FRAMES = 2_000_000
SENSITIVITY = {"low": (4.0, 20.0), "medium": (3.0, 15.0), "high": (2.2, 10.0)}


def options(params):
    mode = params.get("segmentation", "fixed" if "segment_seconds" in params else "shot")
    sensitivity = params.get("scene_sensitivity", "medium")
    if mode not in ("shot", "fixed") or not isinstance(sensitivity, str) or sensitivity not in SENSITIVITY:
        raise UserError("分段模式应为shot/fixed，镜头灵敏度应为low/medium/high。")
    return {"mode": mode, "sensitivity": sensitivity,
            "max_seconds": finite_number(params.get("shot_max_seconds", 20), "长镜头分段上限", 2, 60),
            "fixed_seconds": finite_number(params.get("segment_seconds", 8), "固定分段时长", 2, 60)}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()[:24]


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _valid_detection(data, key):
    if not isinstance(data, dict) or data.get("key") != key:
        return False
    pts, change, light, cuts = (data.get(k) for k in ("pts", "change", "light", "cuts"))
    if not all(isinstance(v, list) for v in (pts, change, light, cuts)) or not 0 < len(pts) <= MAX_FRAMES:
        return False
    if len(change) != len(pts) or len(light) != len(pts) or not cuts or cuts[0] != 0:
        return False
    if any(type(v) not in (int, float) or not math.isfinite(v) for a in (pts, change, light) for v in a):
        return False
    return (type(data.get("filtered_flash_cuts")) is int and pts[0] == 0 and all(a < b for a, b in zip(pts, pts[1:]))
            and all(type(c) is int and 0 <= c < len(pts) for c in cuts)
            and cuts == sorted(set(cuts)))


def detect(path, info, fingerprint, config, ctx, video_id):
    import cv2
    import numpy as np
    key = digest({"source": list(fingerprint), "path": str(path), "detector": DETECTOR_VERSION,
                  "ffmpeg": ctx.media.executable("ffmpeg"), "sensitivity": config["sensitivity"]})
    target = ctx.workspace / "cache" / "shots" / video_id / key / "detection.json"
    with ctx.measure("shots.cache_read"):
        cached = read_json(target)
        valid_cache = _valid_detection(cached, key)
    if valid_cache:
        if ctx.profiler:
            ctx.profiler.count("shot_detection_cache_hits")
        return cached
    try:
        from scenedetect.detectors import AdaptiveDetector
    except ImportError as exc:
        raise UserError("按镜头分段需要PySceneDetect，请运行 scripts/setup.ps1 安装基础依赖，或选择固定时长分段。", "dependency") from exc
    threshold, content_min = SENSITIVITY[config["sensitivity"]]
    detector = AdaptiveDetector(adaptive_threshold=threshold, min_content_val=content_min,
                                min_scene_len=1, window_width=2)
    pts, changes, lights, cuts, flashes = [], [], [], {0}, []
    previous, flash = None, None
    with ctx.measure("media.shot_detection", detector=DETECTOR_VERSION):
        with closing(ctx.media.iter_index_frames(path, 320)) as frames:
            for index, seconds, frame in frames:
                if index >= MAX_FRAMES:
                    raise UserError("单次镜头检测超过200万帧，请分批处理较短视频。")
                tiny = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (32, 18))
                light = float(np.mean(tiny))
                change = float(np.mean(cv2.absdiff(tiny, previous))) if previous is not None else 0.0
                pts.append(seconds)
                changes.append(round(change, 3))
                lights.append(round(light, 3))
                cuts.update(detector.process_frame(index, frame))
                extreme = light > 245 or light < 8
                if extreme and flash is None and previous is not None:
                    flash = (index, seconds, previous.copy())
                elif not extreme and flash is not None:
                    first, moment, before = flash
                    if seconds - moment <= 0.25 and float(np.mean(cv2.absdiff(before, tiny))) < 12:
                        flashes.append((first, index))
                    flash = None
                previous = tiny
                if index % 120 == 0:
                    ctx.progress(0.02 + 0.10 * min(1, seconds / info["duration"]), f"正在检测镜头：{seconds:.1f} / {info['duration']:.1f} 秒…")
        # Flush the detector's look-ahead, without inventing source frames or PTS.
        if pts:
            for offset in range(2):
                cuts.update(detector.process_frame(len(pts) + offset, frame))
    if not pts:
        raise UserError("没有解码到可索引的画面。", "media")
    # Suppress only a brief black/white excursion that returns to a similar frame.
    removed = set()
    ordered = sorted(c for c in cuts if 0 <= c < len(pts))
    for first, last in flashes:
        left, right = bisect.bisect_left(ordered, max(1, first - 1)), bisect.bisect_right(ordered, last + 1)
        removed.update(ordered[left:right])
    data = {"key": key, "pts": pts, "change": changes, "light": lights,
            "cuts": [c for c in ordered if c not in removed], "filtered_flash_cuts": len(removed)}
    if len(data["cuts"]) > MAX_SEGMENTS:
        raise UserError("检测到超过20000个镜头，请降低灵敏度或分批处理视频。")
    with ctx.measure("shots.cache_write"):
        save_json(target, data)
    return data


def fixed_segments(duration, seconds):
    count = math.ceil(duration / seconds)
    if count > MAX_SEGMENTS:
        raise UserError("单次索引超过20000个时间段，请增大索引间隔或分批处理视频。")
    return [{"start": i * seconds, "end": min(duration, (i + 1) * seconds), "shot_id": ""} for i in range(count)]


def shot_segments(data, duration, max_seconds):
    pts = data["pts"]
    boundaries = data["cuts"] + [len(pts)]
    result = []
    for shot, (first, stop) in enumerate(zip(boundaries, boundaries[1:])):
        start, end = pts[first], pts[stop] if stop < len(pts) else duration
        count = max(1, math.ceil((end - start) / max_seconds))
        splits = sorted({first, stop, *(min(stop, bisect.bisect_left(pts, start + (end - start) * i / count))
                                       for i in range(1, count))})
        for a, b in zip(splits, splits[1:]):
            result.append({"start": pts[a], "end": pts[b] if b < len(pts) else duration,
                           "shot_id": f"shot_{shot:06d}", "start_frame": a, "end_frame": b,
                           "shot_start": start, "shot_end": end})
            if len(result) > MAX_SEGMENTS:
                raise UserError("分段超过20000段，请增大长镜头上限或分批处理视频。")
    return result


def sample_count(duration, activity=0):
    if duration <= 2:
        return 1
    if duration <= 5:
        return 2
    if duration <= 10:
        return 3
    return min(6, max(4, math.ceil(duration / 4)) + (1 if activity >= 12 else 0))


def choose_frames(segment, data):
    """Stratify in time, then prefer interior activity without transition frames."""
    pts, change, light = data["pts"], data["change"], data["light"]
    first, stop = segment["start_frame"], segment["end_frame"]
    duration = segment["end"] - segment["start"]
    activity = sum(change[first:stop]) / (stop - first)
    count = min(stop - first, sample_count(duration, activity))
    margin = min(0.15, duration * 0.1)
    lo = min(stop - 1, bisect.bisect_left(pts, segment["start"] + margin, first, stop))
    hi = max(lo + 1, bisect.bisect_left(pts, segment["end"] - margin, lo, stop))
    chosen = []
    for slot in range(count):
        moment = segment["start"] + duration * (slot + 0.5) / count
        radius = duration / count * 0.3
        a = max(lo, min(hi - 1, bisect.bisect_left(pts, moment - radius, lo, hi)))
        b = max(a + 1, min(hi, bisect.bisect_right(pts, moment + radius, a, hi)))
        best = max(range(a, b), key=lambda i: min(change[i], 30) / 30
                   - abs(pts[i] - moment) / max(radius, 0.001) - (3 if light[i] < 8 or light[i] > 245 else 0))
        chosen.append(best)
    return sorted(set(chosen))


def prepare_plan(path, info, fingerprint, config, ctx, video_id, visual):
    data = detect(path, info, fingerprint, config, ctx, video_id) if config["mode"] == "shot" else None
    # A final frame can extend slightly beyond a rounded container duration.
    if data:
        info["duration"] = max(info["duration"], data["pts"][-1] + 1 / info["fps"])
    identity = {"source": list(fingerprint), "path": str(path), "detection": data["key"] if data else None,
                "ffmpeg": ctx.media.executable("ffmpeg"), "options": config, "sampling": SAMPLING_VERSION, "visual": visual}
    key = digest(identity)
    directory = ctx.workspace / "cache" / "samples" / video_id / key
    with ctx.measure("shots.plan"):
        segments = shot_segments(data, info["duration"], config["max_seconds"]) if data else fixed_segments(info["duration"], config["fixed_seconds"])
        for index, segment in enumerate(segments):
            if data:
                indices = choose_frames(segment, data) if visual else [min(segment["end_frame"] - 1,
                    bisect.bisect_left(data["pts"], (segment["start"] + segment["end"]) / 2))]
                segment["samples"] = [{"frame": i, "time": data["pts"][i], "path": str(directory / f"frame_{i:09d}.jpg")} for i in indices]
            else:
                count = sample_count(segment["end"] - segment["start"]) if visual else 1
                segment["samples"] = [{"time": segment["start"] + (segment["end"] - segment["start"]) * (i + 0.5) / count,
                                       "path": str(directory / f"segment_{index:06d}_{i}.jpg")} for i in range(count)]
    durations = sorted(s["end"] - s["start"] for s in segments)
    stats = {"segmentation": config["mode"], "shot_count": len(data["cuts"]) if data else 0,
             "segment_count": len(segments), "min_seconds": durations[0], "median_seconds": durations[len(durations) // 2],
             "max_seconds": durations[-1], "candidate_sample_count": sum(len(s["samples"]) for s in segments),
             "filtered_flash_cuts": data["filtered_flash_cuts"] if data else 0}
    if ctx.profiler:
        ctx.profiler.event("shot_plan", **stats)
        ctx.profiler.count("shot_count", stats["shot_count"])
        ctx.profiler.count("planned_segments", stats["segment_count"])
        ctx.profiler.count("candidate_sample_images", stats["candidate_sample_count"])
        ctx.profiler.count("filtered_flash_cuts", stats["filtered_flash_cuts"])
    return segments, key, directory, stats


def materialize_samples(path, segments, directory, ctx):
    """One sequential decode for missing shot frames; existing images survive retries."""
    from PIL import Image
    with ctx.measure("samples.cache_check"):
        pending = {s["path"]: s for segment in segments for s in segment["samples"] if not _usable_image(s["path"])}
    directory.mkdir(parents=True, exist_ok=True)
    if ctx.profiler:
        ctx.profiler.count("sample_file_cache_hits", sum(len(s["samples"]) for s in segments) - len(pending))
    if not pending:
        return
    with ctx.measure("media.index_samples", count=len(pending)):
        if all("frame" in s for s in pending.values()):
            wanted = {s["frame"]: s for s in pending.values()}
            with closing(ctx.media.iter_index_frames(path, 512)) as frames:
                for index, seconds, pixels in frames:
                    sample = wanted.pop(index, None)
                    if sample is not None:
                        if abs(seconds - sample["time"]) > 0.002:
                            raise UserError("采样与镜头检测的时间戳不一致，请重新建立索引。", "media")
                        target = Path(sample["path"])
                        temporary = target.with_suffix(".tmp")
                        Image.fromarray(pixels[:, :, ::-1]).save(temporary, format="JPEG", quality=90)
                        temporary.replace(target)
                        ctx.progress(0.14, f"正在缓存采样画面：{len(pending) - len(wanted)}/{len(pending)}…")
                    if not wanted:
                        break
            if wanted:
                raise UserError("视频结束前未能提取全部镜头采样，请检查视频。", "media")
        else:
            for sample in pending.values():
                target = Path(sample["path"])
                temporary = target.with_name(target.stem + ".partial.jpg")
                ctx.media.extract_frame(path, sample["time"], temporary, max_edge=512)
                temporary.replace(target)


def _usable_image(path):
    from PIL import Image
    try:
        with Image.open(path) as picture:
            picture.verify()
        return True
    except (OSError, ValueError, SyntaxError):
        return False


def deduplicate(segment):
    import numpy as np
    from PIL import Image
    kept, signatures = [], []
    for sample in segment["samples"]:
        with Image.open(sample["path"]) as image:
            signature = np.asarray(image.convert("RGB").resize((64, 36)), dtype=np.int16)
        # Conservative pixel similarity, no semantic guessing about faces/actions.
        if any(float(np.mean(np.abs(signature - old))) < 1.5 for old in signatures):
            continue
        kept.append(sample)
        signatures.append(signature)
    return kept
