"""Pixel outputs are authoritative; AE paths are bounded, discrete approximations."""
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from .errors import UserError


MAX_CONTOURS_PER_FRAME = 64
MAX_VERTICES_PER_CONTOUR = 256
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


def mask_contours(alpha):
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
    if len(contours) > MAX_CONTOURS_PER_FRAME:
        raise UserError(f"某帧包含 {len(contours)} 条轮廓，超过 AE 路径上限 {MAX_CONTOURS_PER_FRAME}；请使用透明 PNG/MOV。", "export_limit")
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
        if len(vertices) > MAX_VERTICES_PER_CONTOUR:
            raise UserError(f"某条轮廓需要 {len(vertices)} 个顶点，超过 AE 路径上限 {MAX_VERTICES_PER_CONTOUR}；请使用透明 PNG/MOV。", "export_limit")
        result.append({"depth": depth, "vertices": vertices})
    # Fixed slots per depth maintain ADD/SUBTRACT ordering across every frame.
    result.sort(key=lambda entry: (entry["depth"], -polygon_area(entry["vertices"])))
    return result


def polygon_area(vertices):
    return abs(sum(vertices[i][0] * vertices[(i + 1) % len(vertices)][1]
                   - vertices[(i + 1) % len(vertices)][0] * vertices[i][1]
                   for i in range(len(vertices))) / 2)


def build_ae_payload(source_dir, mask_paths, fps, width, height):
    frames, counts, total_vertices = [], [1], 0  # one ADD slot even for an entirely empty clip
    for mask_path in mask_paths:
        with Image.open(mask_path) as image:
            if image.size != (width, height):
                raise UserError("AE 输出序列含有尺寸不一致的蒙版。", "export")
            contours = mask_contours(np.asarray(image.convert("L")))
        grouped = []
        for contour in contours:
            depth = contour["depth"]
            while len(grouped) <= depth:
                grouped.append([])
            grouped[depth].append(contour["vertices"])
            total_vertices += len(contour["vertices"])
        if total_vertices > MAX_AE_VERTICES:
            raise UserError("AE 路径数据超过 100 万顶点，请缩短片段或使用透明 PNG/MOV。", "export_limit")
        while len(counts) < len(grouped):
            counts.append(0)
        for depth, paths in enumerate(grouped):
            counts[depth] = max(counts[depth], len(paths))
        frames.append(grouped)
    if not frames:
        raise UserError("没有可输出的蒙版帧。")
    if sum(counts) > MAX_CONTOURS_PER_FRAME:
        raise UserError("跨帧嵌套轮廓需要超过 64 条 AE 路径，请使用透明 PNG/MOV。", "export_limit")
    return {"source": str((Path(source_dir) / "000000.png").resolve()).replace("\\", "/"),
            "fps": fps, "width": width, "height": height, "count": len(frames),
            "slots": counts, "frames": frames}


def export_ae(source_dir, mask_paths, fps, width, height, output):
    if not 1 <= fps <= 99:
        raise UserError("该视频帧率超出 AE 脚本支持的 1–99 fps，请使用 PNG/MOV 或先转码到常用帧率。", "export_limit")
    data = build_ae_payload(source_dir, mask_paths, fps, width, height)
    # Embed owned JSON as an ordinary ES3 literal: no eval, external parser, or
    # executable user input. Hold keyframes support changing vertex topology.
    serialized = json.dumps(data, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    script = """#target aftereffects
// Generated by MADSearcher. PNG alpha is authoritative; paths are approximations.
// Run via File > Scripts > Run Script File, or AfterFX.exe -r this_file.jsx.
(function () {
    var data = __PAYLOAD__;
    var source = new File(data.source);
    if (!source.exists) { alert("Missing source_frames sequence. Keep this script with its export folder."); return; }
    app.beginUndoGroup("MADSearcher cutout paths");
    try {
        if (!app.project) app.newProject();
        var comp = app.project.activeItem;
        var duration = data.count / data.fps;
        if (!(comp instanceof CompItem)) {
            comp = app.project.items.addComp("MADSearcher cutout", data.width, data.height, 1, Math.max(duration, 1 / data.fps), data.fps);
        }
        var base = Math.max(0, comp.time);
        if (comp.duration < base + duration) comp.duration = base + duration;
        var options = new ImportOptions(source);
        options.sequence = true;
        options.forceAlphabetical = true;
        var footage = app.project.importFile(options);
        footage.name = "MADSearcher source sequence";
        if (!footage.mainSource.isStill) footage.mainSource.conformFrameRate = data.fps;
        var layer = comp.layers.add(footage);
        layer.name = "MADSearcher cutout (editable paths)";
        layer.startTime = base;
        layer.inPoint = base;
        layer.outPoint = base + duration;
        layer.motionBlur = false;
        layer.frameBlendingType = FrameBlendingType.NO_FRAME_BLEND;
        var times = [];
        for (var f = 0; f < data.count; f++) times.push(base + f / data.fps);
        for (var depth = 0; depth < data.slots.length; depth++) {
            for (var slot = 0; slot < data.slots[depth]; slot++) {
                var mask = layer.property("ADBE Mask Parade").addProperty("ADBE Mask Atom");
                mask.name = "MAD d" + depth + " p" + slot;
                mask.maskMode = depth % 2 ? MaskMode.SUBTRACT : MaskMode.ADD;
                mask.rotoBezier = false;
                var shapes = [];
                for (var frame = 0; frame < data.count; frame++) {
                    var group = data.frames[frame][depth];
                    var points = group && group[slot] ? group[slot] : [[-4,-4],[-3,-4],[-3,-3]];
                    var shape = new Shape();
                    shape.vertices = points;
                    shape.closed = true;
                    var tangents = [];
                    for (var p = 0; p < points.length; p++) tangents.push([0,0]);
                    shape.inTangents = tangents;
                    shape.outTangents = tangents;
                    shapes.push(shape);
                }
                var path = mask.property("ADBE Mask Shape");
                path.setValuesAtTimes(times, shapes);
                for (var key = 1; key <= path.numKeys; key++) {
                    path.setInterpolationTypeAtKey(key, KeyframeInterpolationType.HOLD, KeyframeInterpolationType.HOLD);
                }
            }
        }
        comp.openInViewer();
    } catch (error) {
        alert("MADSearcher: " + error.toString() + "\\nUndo once to remove any partial import.");
    } finally {
        app.endUndoGroup();
    }
})();
""".replace("__PAYLOAD__", serialized)
    Path(output).write_text(script, encoding="utf-8")
    return str(output)
