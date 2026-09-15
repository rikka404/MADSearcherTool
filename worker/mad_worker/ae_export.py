"""Read-only pixel handoff to AE; optional bounded paths never gate pixel exports."""
import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from .errors import UserError
from .validation import existing_file, finite_number

MODES = {"rgba": "透明PNG序列", "matte": "原画＋独立Alpha遮罩", "paths": "可编辑矢量路径"}
TEMPLATE = Path(__file__).with_name("templates") / "ae_import.jsx"
SCRIPT_VERSION = 2


def validate_mode(value):
    if not isinstance(value, str) or value not in MODES:
        raise UserError("AE导出方式必须为rgba、matte或paths。")
    return value


def _dimensions(width, height, fps, count):
    for value, label, minimum, maximum in ((width, "宽度", 4, 16384), (height, "高度", 4, 16384), (count, "帧数", 1, 3600)):
        if type(value) is not int or not minimum <= value <= maximum:
            raise UserError(f"抠像记录中的{label}无效，须为{minimum}–{maximum}的整数。")
    if width * height > 25_000_000:
        raise UserError("抠像记录超过本工具2500万像素的AE导出保护上限。")
    if type(fps) not in (int, float):
        raise UserError("抠像记录中的fps必须是数值。")
    return finite_number(fps, "AE帧率", 1, 99)


def _sequence(root, name, width, height, count):
    directory = root / name
    if directory.resolve().parent != root or not directory.is_dir():
        raise UserError(f"缺少任务内的{name}序列；请保留完整抠像输出文件夹。", "export")
    expected = [f"{i:06d}.png" for i in range(count)]
    if set(p.name for p in directory.glob("*.png")) != set(expected):
        raise UserError(f"{name}序列缺帧、编号不连续或帧数与manifest不一致。", "export")
    fingerprints = []
    for filename in expected:
        path = directory / filename
        if path.resolve().parent != directory.resolve():
            raise UserError(f"{name}序列含有指向其他目录的链接。", "export")
        try:
            stat = path.stat()
            if not 0 < stat.st_size <= 100 * 1024 * 1024:
                raise UserError(f"{name}/{filename}文件大小异常。", "export")
            # Read PNG headers only. No decode, resize, re-encode, or alpha conversion.
            with Image.open(path) as image:
                modes = {"RGBA"} if name == "rgba" else {"L"} if name == "masks" else {"RGB", "RGBA"}
                if image.format != "PNG" or image.size != (width, height) or image.mode not in modes:
                    raise UserError(f"{name}/{filename}尺寸或像素格式与抠像记录不一致。", "export")
            fingerprints.append((path, stat.st_size, stat.st_mtime_ns))
        except (OSError, Image.DecompressionBombError):
            raise UserError(f"无法读取{name}/{filename}，请检查PNG文件。", "export")
    return fingerprints


def _unchanged(fingerprints):
    for path, size, mtime in fingerprints:
        try:
            stat = path.stat()
            if (stat.st_size, stat.st_mtime_ns) == (size, mtime):
                continue
        except OSError:
            pass
        raise UserError("PNG序列在导出期间发生变化，已停止；请恢复完整结果后补导出。", "export")


def _json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def write_script(data, output):
    output = Path(output)
    serialized = json.dumps(data | {"script_version": SCRIPT_VERSION}, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    script = TEMPLATE.read_text(encoding="utf-8").replace("__PAYLOAD__", serialized)
    temporary = output.with_suffix(".jsx.tmp")
    temporary.write_text(script, encoding="utf-8")
    temporary.replace(output)
    return str(output)


class _Log:
    def __init__(self, directory):
        self.path = directory / "export_log.jsonl"
        self.started = time.perf_counter()
        self.timings = {}

    def event(self, event, **values):
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": event, "elapsed_ms": round((time.perf_counter() - self.started) * 1000, 3),
                                     **values}, ensure_ascii=False, allow_nan=False) + "\n")

    @contextmanager
    def stage(self, name):
        started, status = time.perf_counter(), "completed"
        self.event("stage_start", stage=name)
        try:
            yield
        except BaseException:
            status = "failed"
            raise
        finally:
            elapsed = round((time.perf_counter() - started) * 1000, 3)
            self.timings[name] = elapsed
            self.event("stage_end", stage=name, status=status, duration_ms=elapsed)


def export_bundle(task_dir, width, height, fps, frame_count, mode="matte", progress=None):
    mode = validate_mode(mode)
    fps = _dimensions(width, height, fps, frame_count)
    root = Path(task_dir).resolve()
    directory = root / "ae_exports" / ("ae_" + datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8])
    if directory.resolve().parent.parent != root:
        raise UserError("AE输出目录不能链接到抠像任务目录之外。", "export")
    directory.mkdir(parents=True)
    incomplete = directory / "INCOMPLETE.txt"
    incomplete.write_text("本次AE导出未完成。请检查export_log.jsonl；旧抠像文件未修改。\n", encoding="utf-8")
    log = _Log(directory)
    report = {"schema_version": 1, "script_version": SCRIPT_VERSION, "status": "in_progress", "created_utc": datetime.now(timezone.utc).isoformat(),
              "requested_mode": mode, "actual_mode": mode, "width": width, "height": height, "fps": fps,
              "frame_count": frame_count, "scripts": {}, "warnings": [], "vector_diagnostics": {}}
    scripts, warnings = report["scripts"], report["warnings"]
    try:
        if progress:
            progress("正在检查已有PNG序列，保留原始像素…")
        with log.stage("ae.validate_sequences"):
            fingerprints = _sequence(root, "rgba", width, height, frame_count)
            if mode != "rgba":
                fingerprints += _sequence(root, "source_frames", width, height, frame_count)
        data = {"name": root.name[:100], "width": width, "height": height, "fps": fps, "count": frame_count}
        for name, key in (("rgba", "rgba"), ("source_frames", "source")):
            data[key] = os.path.relpath(root / name / "000000.png", directory).replace("\\", "/")
        with log.stage("ae.pixel_scripts"):
            scripts["rgba"] = write_script(data | {"mode": "rgba"}, directory / "import_rgba.jsx")
            if mode != "rgba":
                scripts["matte"] = write_script(data | {"mode": "matte"}, directory / "import_matte.jsx")
            log.event("script_layout", script_version=SCRIPT_VERSION, color_repair=mode != "rgba",
                      edge_controls=mode != "rgba")
        if mode == "paths":
            if progress:
                progress("保真脚本已准备，正在生成可选的逐帧矢量路径…")
            try:
                with log.stage("ae.vector_paths"):
                    fingerprints += _sequence(root, "masks", width, height, frame_count)
                    from .exporters import build_ae_payload
                    payload = build_ae_payload(root / "source_frames", [root / "masks" / f"{i:06d}.png" for i in range(frame_count)],
                                               fps, width, height, diagnostics=report["vector_diagnostics"])
                    payload.update(data, mode="paths")
                    scripts["paths"] = write_script(payload, directory / "import_paths.jsx")
                warnings.append("矢量路径是0.75像素简化的二值轮廓，不包含半透明alpha；保真版本请用同目录import_matte.jsx或import_rgba.jsx。")
            except Exception as exc:
                # Optional vector processing is isolated from already prepared pixel scripts.
                report["actual_mode"] = "matte"
                report["vector_error"] = {"code": getattr(exc, "code", "export"), "type": type(exc).__name__, "message": str(exc)}
                warnings.append("矢量路径未生成，已提供原画＋Alpha遮罩脚本：" + str(exc))
                log.event("vector_fallback", **report["vector_error"])
            finally:
                log.event("vector_diagnostics", **report["vector_diagnostics"])
        with log.stage("ae.validate_unchanged"):
            _unchanged(fingerprints)
        report.update(status="complete", timings_ms=log.timings, total_ms=round((time.perf_counter() - log.started) * 1000, 3))
        _json(directory / "export_report.json", report)
        log.event("run_end", status="complete", requested_mode=mode, actual_mode=report["actual_mode"])
        incomplete.unlink()
    except Exception as exc:
        report.update(status="failed", error={"code": getattr(exc, "code", "export"), "message": str(exc)}, timings_ms=log.timings)
        _json(directory / "export_report.json", report)
        log.event("run_end", status="failed", **report["error"])
        raise
    return {"ae_script": scripts[report["actual_mode"]], "ae_scripts": scripts,
            "ae_mode": mode, "ae_actual_mode": report["actual_mode"], "ae_script_version": SCRIPT_VERSION, "ae_export_dir": str(directory),
            "ae_report_path": str(directory / "export_report.json"), "ae_log_path": str(log.path), "warnings": warnings}


def reexport(params, ctx):
    mode = validate_mode(params.get("ae_mode", "matte"))
    manifest = existing_file(params.get("manifest_path"), "抠像任务manifest.json", {".json"})
    if manifest.name.lower() != "manifest.json" or manifest.stat().st_size > 2 * 1024 * 1024:
        raise UserError("请选择抠像任务目录中的manifest.json（最多2MiB）。")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError, OSError):
        raise UserError("抠像manifest无法读取，请检查文件是否完整。")
    if (not isinstance(data, dict) or type(data.get("schema_version")) is not int
            or data["schema_version"] not in (1, 2) or data.get("status") != "complete"
            or (manifest.parent / "INCOMPLETE.txt").exists()):
        raise UserError("只能补导出完整的抠像任务；请检查manifest版本、完成状态和INCOMPLETE标记。")
    result = export_bundle(manifest.parent, data.get("width"), data.get("height"), data.get("fps"), data.get("frame_count"),
                           mode, lambda message: ctx.progress(0.5, message))
    ctx.progress(1, "AE补导出完成；原始抠像结果和旧脚本均保留。")
    start_frame = data.get("start_frame", 0)
    if type(start_frame) is not int or start_frame < 0:
        start_frame = 0
    return result | {"output_dir": str(manifest.parent), "manifest_path": str(manifest),
                     "width": data["width"], "height": data["height"], "fps": data["fps"], "frame_count": data["frame_count"],
                     "rgba_dir": str(manifest.parent / "rgba"), "start_frame": start_frame}
