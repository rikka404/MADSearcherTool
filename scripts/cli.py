"""Public command-line adapter; all media/AI work uses the shared worker services."""
import argparse
import io
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))

from mad_worker.errors import UserError


# Flag declarations stay separate from the worker handlers: extending the CLI
# does not duplicate validation, persistence, model calls, or export logic.
COMMANDS = {
    "system.check": ("检查本地依赖和配置", []),
    "group.list": ("列出动画分组", []),
    "group.create": ("创建动画分组", ["name", "description"]),
    "group.update": ("更新分组名称和角色说明", ["group_id", "name", "description"]),
    "group.delete": ("删除分组和库记录，保留原视频", ["group_id"]),
    "video.list": ("列出一个分组的素材", ["group_id"]),
    "video.add": ("批量导入 MP4 路径", ["group_id", "paths"]),
    "video.remove": ("移除素材记录，保留原视频", ["video_id"]),
    "video.probe": ("读取视频元数据", ["path"]),
    "video.frame": ("提取某个时间点的画面", ["path", "time"]),
    "index.run": ("为一个素材建立或恢复索引", ["video_id", "subtitle_path", "subtitle_offset",
        "transcribe", "language", "visual", "semantic", "segment_seconds"]),
    "search.run": ("在一个分组内检索镜头", ["group_id", "query", "limit", "semantic"]),
    "preview.make": ("生成可播放的 MP4 预览", ["path", "start", "end"]),
    "clip.export": ("导出指定范围的 MP4", ["path", "start", "end", "output_dir"]),
    "cutout.run": ("执行 SAM 2 人物抠像和输出", ["path", "start", "end", "prompt_mode", "mask_path",
        "prompt", "reference_path", "box", "output_dir", "export_video", "export_ae"]),
    "ae.send": ("把已生成 JSX 派发给 After Effects（不保存工程）", ["script_path"]),
}
FLOATS = {"time", "start", "end", "subtitle_offset", "segment_seconds"}
FLAGS = {"transcribe", "visual", "semantic", "export_video", "export_ae"}
SETTINGS = {
    "FfmpegPath": "ffmpeg_path", "FfprobePath": "ffprobe_path", "VisionModel": "vision_model",
    "EmbeddingModel": "embedding_model", "SamCheckpoint": "sam_checkpoint", "SamConfig": "sam_config",
    "Device": "device", "WhisperModel": "whisper_model", "WhisperDevice": "whisper_device",
    "AfterFxPath": "afterfx_path",
}


def read_json(path):
    try:
        if path != "-" and Path(path).stat().st_size > 2_000_000:
            raise UserError("JSON 输入超过 2 MB，请缩小请求。")
        raw = sys.stdin.read(2_000_001) if path == "-" else Path(path).read_text(encoding="utf-8-sig")
        if len(raw) > 2_000_000:
            raise UserError("JSON 输入超过 2 MB，请缩小请求。")
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise UserError("无法读取 JSON 输入，请检查路径、UTF-8 编码及格式。")
    if not isinstance(value, dict):
        raise UserError("JSON 输入必须是对象。")
    return value


def parser_for_cli():
    parser = argparse.ArgumentParser(description="MADSearcher CLI · 输出 UTF-8 NDJSON；Ctrl+C 取消。")
    parser.add_argument("--version", action="version", version="MADSearcher 1.0.0")
    commands = parser.add_subparsers(dest="command", required=True)
    for command, (description, fields) in COMMANDS.items():
        child = commands.add_parser(command, help=description, description=description)
        child.add_argument("--params-file", help="UTF-8 JSON 参数文件；- 表示标准输入。显式参数优先。")
        child.add_argument("--workspace", help="工作区目录，默认使用桌面端设置或项目 workspace。")
        child.add_argument("--settings", help="普通设置 JSON，可用桌面 PascalCase 或 worker snake_case；不加载密钥。")
        for name in SETTINGS.values():
            choices = ["cpu", "cuda"] if name in ("device", "whisper_device") else None
            child.add_argument("--" + name.replace("_", "-"), choices=choices, default=None)
        for field in fields:
            options = {"default": None}
            if field in FLOATS:
                options["type"] = float
            elif field == "limit":
                options["type"] = int
            elif field in FLAGS:
                options["action"] = argparse.BooleanOptionalAction
            elif field == "paths":
                options["nargs"] = "+"
            elif field == "box":
                options.update(nargs=4, type=float, metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"))
            elif field == "prompt_mode":
                options["choices"] = ["mask", "box", "text", "reference"]
            child.add_argument("--" + field.replace("_", "-"), **options)
    raw = commands.add_parser("request", help="执行完整原始请求 JSON（脚本集成入口）")
    raw.add_argument("--request-file", required=True, help="UTF-8 请求文件；- 表示标准输入。")
    return parser


def compose_request(args):
    if args.command == "request":
        request = read_json(args.request_file)
        request.setdefault("workspace", str(ROOT / "workspace"))
        request.setdefault("settings", {})
        if isinstance(request["settings"], dict):
            request["settings"].setdefault("api_key", os.environ.get("OPENAI_API_KEY", ""))
        return request
    settings_file = Path(args.settings) if args.settings else ROOT / "workspace" / "settings.json"
    stored = read_json(str(settings_file)) if args.settings or settings_file.is_file() else {}
    settings = {snake: stored.get(snake, stored.get(pascal)) for pascal, snake in SETTINGS.items()
                if snake in stored or pascal in stored}
    settings = {key: value for key, value in settings.items() if value is not None}
    settings.update({key: getattr(args, key) for key in SETTINGS.values() if getattr(args, key) is not None})
    settings["api_key"] = os.environ.get("OPENAI_API_KEY", "")
    checkpoint = ROOT / "models" / "sam2.1_hiera_tiny.pt"
    if not settings.get("sam_checkpoint") and checkpoint.is_file():
        settings["sam_checkpoint"] = str(checkpoint)
    workspace = args.workspace or stored.get("workspace") or stored.get("Workspace") or str(ROOT / "workspace")
    if not isinstance(workspace, str) or not workspace.strip():
        raise UserError("工作区必须为有效路径。")
    workspace = str(Path(workspace).expanduser().resolve())
    params = read_json(args.params_file) if args.params_file else {}
    params.update({field: getattr(args, field) for field in COMMANDS[args.command][1] if getattr(args, field) is not None})
    if args.command in ("clip.export", "cutout.run"):
        params.setdefault("output_dir", str(Path(workspace) / "exports"))
    return {"command": args.command, "params": params, "settings": settings, "workspace": workspace}


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parser_for_cli().parse_args()
    try:
        request = compose_request(args)
        os.environ.setdefault("HF_HOME", str(ROOT / "models" / "huggingface"))
        os.environ.setdefault("TORCH_HOME", str(ROOT / "models" / "torch"))
        # Reuse the exact worker error redaction and stdout isolation contract.
        from mad_worker.__main__ import main as worker_main
        original_stdin = sys.stdin
        try:
            sys.stdin = io.StringIO(json.dumps(request, ensure_ascii=False, allow_nan=False) + "\n")
            return worker_main()
        finally:
            sys.stdin = original_stdin
    except KeyboardInterrupt:
        print(json.dumps({"type": "error", "code": "cancelled", "message": "任务已取消。"}, ensure_ascii=False), flush=True)
        return 130
    except (UserError, ValueError, OSError) as exc:
        message = str(exc)
        key = os.environ.get("OPENAI_API_KEY", "")
        if key:
            message = message.replace(key, "[已隐藏密钥]")
        print(json.dumps({"type": "error", "code": getattr(exc, "code", "validation"), "message": message}, ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
