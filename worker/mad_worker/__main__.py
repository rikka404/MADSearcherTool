import contextlib
import hashlib
import importlib.util
import json
import platform
import sys
import uuid
from pathlib import Path
from .context import Context
from .errors import UserError
from .validation import finite_number, time_range


def handle(command, params, ctx):
    if command in ("storage.scan", "storage.clean"):
        from .storage import scan, clean
        return (scan if command == "storage.scan" else clean)(params, ctx)
    if command == "system.check":
        checks = []
        for name in ("ffmpeg", "ffprobe"):
            try:
                path = ctx.media.executable(name)
                checks.append({"name": name, "ok": True, "message": path})
            except UserError as exc:
                checks.append({"name": name, "ok": False, "message": str(exc)})
        for module, label in (("numpy", "NumPy"), ("PIL", "Pillow"), ("cv2", "OpenCV"),
                              ("scenedetect", "PySceneDetect"), ("torch", "PyTorch"), ("sam2", "SAM 2"), ("faster_whisper", "faster-whisper")):
            ok = importlib.util.find_spec(module) is not None
            checks.append({"name": label, "ok": ok, "message": "已安装" if ok else "未安装；请运行 scripts/setup.ps1 对应依赖选项"})
        checkpoint = str(ctx.settings.get("sam_checkpoint") or "")
        checks.append({"name": "SAM 权重", "ok": bool(checkpoint) and Path(checkpoint).is_file(),
                       "message": checkpoint or "未选择，请运行 scripts/setup.ps1 -WithSam 后在设置中选择权重"})
        checks.append({"name": "OpenAI", "ok": bool(str(ctx.settings.get("api_key") or "").strip()),
                       "message": "已配置密钥（尚未验证权限）" if ctx.settings.get("api_key") else "未填写，字幕检索和本地蒙版抠像仍可用"})
        return {"checks": checks, "python": platform.python_version(), "workspace": str(ctx.workspace)}
    if command == "video.probe":
        return ctx.media.probe(params.get("path"))
    if command == "cutout.range":
        from .timecode import resolve_range
        info = ctx.media.probe(params.get("path"))
        return {"video": info, **resolve_range(params, info).as_dict()}
    if command == "cutout.export_ae":
        from .ae_export import reexport
        return reexport(params, ctx)
    if command in ("video.frame", "preview.make", "clip.export"):
        info = ctx.media.probe(params.get("path"))
        if command == "video.frame":
            output = ctx.workspace / "cache" / "frames" / (uuid.uuid4().hex + ".png")
            if params.get("frame_index") is not None:
                from .timecode import frame_index, frame_rate, total_frames
                if params.get("time") is not None:
                    raise UserError("取帧时请只填写帧号或秒数其中一种。")
                index = frame_index(params["frame_index"], "帧号")
                count, _ = total_frames(info, frame_rate(info))
                if index >= count:
                    raise UserError(f"帧号必须小于视频总帧数 {count}。")
                return {"path": ctx.media.extract_frame_index(info["path"], index, output),
                        "width": info["width"], "height": info["height"], "frame_index": index}
            moment = finite_number(params.get("time", 0), "取帧时间", 0)
            if moment >= info["duration"]:
                raise UserError("取帧时间必须小于视频时长。")
            return {"path": ctx.media.extract_frame(info["path"], moment, output), "width": info["width"], "height": info["height"]}
        start, end = time_range(params.get("start"), params.get("end"), info["duration"])
        if command == "preview.make":
            if end - start > 300:
                raise UserError("单次预览最长5分钟，请缩短范围。")
            stat = Path(info["path"]).stat()
            key = hashlib.sha256(f'{info["path"]}|{stat.st_size}|{stat.st_mtime_ns}|{start}|{end}'.encode()).hexdigest()[:24]
            output = ctx.workspace / "cache" / "previews" / (key + ".mp4")
            if output.exists():
                return {"path": str(output)}
        else:
            directory = str(params.get("output_dir") or "").strip()
            if not directory:
                raise UserError("请选择片段输出目录。")
            output = Path(directory).resolve() / f"{Path(info['path']).stem}_{start:.2f}-{end:.2f}_{uuid.uuid4().hex[:6]}.mp4"
        ctx.progress(0.1, "正在生成片段…")
        temporary = output.with_name(output.stem + ".partial.mp4")
        ctx.media.make_preview(info["path"], start, end, temporary)
        temporary.replace(output)
        return {"path": str(output)}
    if command.startswith(("group.", "video.", "index.", "search.")):
        from . import search
        return search.handle(command, params, ctx)
    if command.startswith("cutout."):
        from . import cutout
        return cutout.handle(command, params, ctx)
    if command.startswith("ae."):
        from . import adobe
        return adobe.handle(command, params, ctx)
    raise UserError(f"未知操作：{command}")


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    protocol = sys.stdout
    def emit(event):
        protocol.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
        protocol.flush()
    secret = ""
    try:
        raw = sys.stdin.readline(2_000_001)
        if len(raw) > 2_000_000:
            raise UserError("请求过大。")
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise UserError("请求必须是 JSON 对象。")
        command, params = request.get("command"), request.get("params", {})
        if not isinstance(command, str) or not isinstance(params, dict):
            raise UserError("command 必须为字符串，params 必须为对象。")
        settings = request.get("settings", {})
        if isinstance(settings, dict):
            secret = str(settings.get("api_key") or "")
        ctx = Context(request.get("workspace"), settings, emit)
        from .activity import workspace_activity
        with contextlib.redirect_stdout(sys.stderr), workspace_activity(ctx.workspace, exclusive=command == "storage.clean"):
            data = handle(command, params, ctx)
        emit({"type": "result", "data": data})
        return 0
    except Exception as exc:
        message = str(exc)
        if secret:
            message = message.replace(secret, "[已隐藏密钥]")
        code = getattr(exc, "code", "internal")
        if isinstance(exc, json.JSONDecodeError):
            message, code = "请求 JSON 格式不正确。", "validation"
        elif isinstance(exc, ModuleNotFoundError):
            message, code = f"缺少运行依赖 {exc.name}，请运行 scripts/setup.ps1。", "dependency"
        elif isinstance(exc, PermissionError):
            message, code = "没有文件访问权限，请选择可写工作区或输出目录。", "permission"
        emit({"type": "error", "code": code, "message": message or "操作失败，请检查设置。"})
        return 1


if __name__ == "__main__":
    sys.exit(main())
