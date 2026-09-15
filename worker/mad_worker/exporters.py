"""Pixel outputs are authoritative; AE paths are bounded, discrete approximations."""
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from .errors import UserError


MAX_CONTOURS_PER_FRAME = 64
MAX_VERTICES_PER_CONTOUR = 4096
MAX_AE_VERTICES = 1_000_000


def write_rgba_frame(source, alpha, rgba_path, mask_path):
    """Preserve decoded RGB and an 8-bit straight (unpremultiplied) alpha plane."""
    alpha = np.asarray(alpha)
    with Image.open(source) as image:
        rgb = image.convert("RGB")
        if alpha.shape != (rgb.height, rgb.width):
            raise UserError("输出蒙版尺寸与视频画面不一致，任务已停止。", "inference")
        if alpha.dtype == np.bool_:
            alpha = alpha.astype(np.uint8) * 255
        elif alpha.dtype != np.uint8:
            raise UserError("输出蒙版必须是 8 位灰度或布尔图像。", "inference")
        mask = Image.fromarray(alpha)
        rgba = rgb.convert("RGBA")
        rgba.putalpha(mask)
        rgba.save(rgba_path, compress_level=3)
        mask.save(mask_path, compress_level=3)


def export_prores(media, rgba_dir, fps, frame_count, output):
    media.run(["-n", "-framerate", f"{fps:.10g}", "-start_number", "0",
               "-i", Path(rgba_dir) / "%06d.png", "-frames:v", frame_count,
               "-an", "-c:v", "prores_ks", "-profile:v", "4", "-pix_fmt", "yuva444p10le",
               "-alpha_bits", "16", "-threads", "2", output], timeout=1800)
    return str(output)


def export_preview(media, rgba_dir, width, height, fps, frame_count, output):
    """A checkerboard MP4 can be played by WPF; it is explicitly not alpha media."""
    checker_path = Path(output).with_name("checkerboard.png")
    checker = Image.new("RGB", (width, height), (54, 59, 67))
    draw = ImageDraw.Draw(checker)
    cell = max(8, min(width, height) // 24)
    for y in range(0, height, cell):
        for x in range(0, width, cell):
            if (x // cell + y // cell) % 2:
                draw.rectangle((x, y, x + cell - 1, y + cell - 1), fill=(79, 85, 94))
    checker.save(checker_path)
    media.run(["-n", "-loop", "1", "-framerate", f"{fps:.10g}", "-i", checker_path,
               "-framerate", f"{fps:.10g}", "-start_number", "0",
               "-i", Path(rgba_dir) / "%06d.png", "-filter_complex_threads", "1",
               "-filter_complex", "[0:v][1:v]overlay=shortest=1:format=auto,scale=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p[v]",
               "-map", "[v]", "-frames:v", frame_count, "-an", "-c:v", "libx264",
               "-preset", "veryfast", "-crf", "20", "-threads", "2", "-movflags", "+faststart", output], timeout=1800)
    return str(output)


def mask_contours(alpha, *, diagnostics=None):
    """Retain the contour tree, including holes and islands inside holes.

    Paths follow pixel centres with <= 0.75 px simplification. Subpixel alpha is
    not representable by vector paths. No component is silently discarded.
    """
    try:
        import cv2
    except ImportError:
        raise UserError("AE 路径输出需要 OpenCV，请运行 scripts/setup.ps1 安装基础依赖。", "dependency")
    binary = (np.asarray(alpha) >= 128).astype(np.uint8) * 255
    if binary.ndim != 2:
        raise UserError("AE 路径需要二维灰度蒙版。")
    contours, hierarchy = cv2.findContours(binary, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    stats = diagnostics if diagnostics is not None else {}
    stats.update(contours=len(contours), max_vertices=0)
    if len(contours) > MAX_CONTOURS_PER_FRAME:
        raise UserError(f"包含{len(contours)}条轮廓，超过本工具矢量导出保护上限{MAX_CONTOURS_PER_FRAME}；可使用像素Alpha遮罩。", "export_limit")
    if hierarchy is None:
        return []
    result = []
    for index, contour in enumerate(contours):
        depth, parent = 0, int(hierarchy[0, index, 3])
        while parent >= 0:
            depth += 1
            parent = int(hierarchy[0, parent, 3])
        polygon = cv2.approxPolyDP(contour, 0.75, True).reshape(-1, 2)
        if len(polygon) < 3 or abs(float(cv2.contourArea(polygon))) < 0.01:
            # A one-pixel/line component still requires a visible path.
            x, y, w, h = cv2.boundingRect(contour)
            vertices = [[float(x), float(y)], [float(x + w), float(y)],
                        [float(x + w), float(y + h)], [float(x), float(y + h)]]
        else:
            vertices = (polygon.astype(np.float64) + 0.5).tolist()
        stats.update(contour_index=index, vertices=len(vertices), max_vertices=max(stats["max_vertices"], len(vertices)))
        if len(vertices) > MAX_VERTICES_PER_CONTOUR:
            raise UserError(f"第{index + 1}条轮廓需要{len(vertices)}个顶点，超过本工具矢量导出保护上限{MAX_VERTICES_PER_CONTOUR}；可使用像素Alpha遮罩。", "export_limit")
        result.append({"depth": depth, "vertices": vertices})
    # Fixed slots per depth maintain ADD/SUBTRACT ordering across every frame.
    result.sort(key=lambda entry: (entry["depth"], -polygon_area(entry["vertices"])))
    return result


def polygon_area(vertices):
    return abs(sum(vertices[i][0] * vertices[(i + 1) % len(vertices)][1]
                   - vertices[(i + 1) % len(vertices)][0] * vertices[i][1]
                   for i in range(len(vertices))) / 2)


def build_ae_payload(source_dir, mask_paths, fps, width, height, *, diagnostics=None):
    frames, counts, total_vertices = [], [1], 0  # one ADD slot even for an entirely empty clip
    stats = diagnostics if diagnostics is not None else {}
    stats.update(frames_processed=0, total_vertices=0, max_vertices_per_contour=0, max_contours_per_frame=0,
                 limits={"contours_per_frame": MAX_CONTOURS_PER_FRAME, "vertices_per_contour": MAX_VERTICES_PER_CONTOUR,
                         "total_vertices": MAX_AE_VERTICES}, simplification_pixels=0.75)
    for frame_index, mask_path in enumerate(mask_paths):
        frame_stats = {"frame_index": frame_index, "time_seconds": round(frame_index / fps, 6)}
        try:
            with Image.open(mask_path) as image:
                if image.size != (width, height):
                    raise UserError("AE输出序列含有尺寸不一致的蒙版。", "export")
                contours = mask_contours(np.asarray(image.convert("L")), diagnostics=frame_stats)
        except UserError as exc:
            stats["failure"] = frame_stats
            raise UserError(f"第{frame_index + 1}帧（片段内{frame_index / fps:.3f}秒）：{exc}", exc.code) from exc
        stats["max_vertices_per_contour"] = max(stats["max_vertices_per_contour"], frame_stats["max_vertices"])
        stats["max_contours_per_frame"] = max(stats["max_contours_per_frame"], frame_stats["contours"])
        grouped = []
        for contour in contours:
            depth = contour["depth"]
            while len(grouped) <= depth:
                grouped.append([])
            grouped[depth].append(contour["vertices"])
            total_vertices += len(contour["vertices"])
        if total_vertices > MAX_AE_VERTICES:
            stats["failure"] = frame_stats | {"total_vertices": total_vertices}
            raise UserError(f"第{frame_index + 1}帧时累计{total_vertices}个顶点，超过本工具100万顶点保护上限；可使用像素Alpha遮罩。", "export_limit")
        while len(counts) < len(grouped):
            counts.append(0)
        for depth, paths in enumerate(grouped):
            counts[depth] = max(counts[depth], len(paths))
        frames.append(grouped)
        stats.update(frames_processed=len(frames), total_vertices=total_vertices)
    if not frames:
        raise UserError("没有可输出的蒙版帧。")
    if sum(counts) > MAX_CONTOURS_PER_FRAME:
        stats["failure"] = {"required_slots": sum(counts)}
        raise UserError("跨帧嵌套轮廓超过本工具64个矢量路径槽保护上限；可使用像素Alpha遮罩。", "export_limit")
    stats["required_slots"] = sum(counts)
    return {"source": str((Path(source_dir) / "000000.png").resolve()).replace("\\", "/"),
            "fps": fps, "width": width, "height": height, "count": len(frames),
            "slots": counts, "frames": frames}


def export_ae(source_dir, mask_paths, fps, width, height, output):
    """Legacy vector-only helper; application exports use ae_export.export_bundle."""
    if not 1 <= fps <= 99:
        raise UserError("该视频帧率超出AE脚本支持的1–99 fps。", "export_limit")
    from .ae_export import write_script
    data = build_ae_payload(source_dir, mask_paths, fps, width, height)
    data.update(mode="paths", name="cutout")
    return write_script(data, output)
