"""Explicit Adobe script dispatch, independent of GUI and MCP availability."""
import os
import shutil
import subprocess
from pathlib import Path

from .errors import UserError
from .validation import existing_file


def handle(command, params, ctx):
    if command != "ae.send":
        raise UserError(f"未知 Adobe 操作：{command}")
    script = existing_file(params.get("script_path"), "AE 脚本", {".jsx"})
    configured = str(ctx.settings.get("afterfx_path") or "").strip()
    if not configured:
        raise UserError("请配置 AfterFX.exe 路径；CLI 可使用 --afterfx-path 指定。", "configuration")
    executable = shutil.which(configured)
    if not executable or not Path(executable).is_file():
        raise UserError("找不到 AfterFX.exe，请检查 After Effects 安装位置。", "dependency")
    if os.name != "nt":
        raise UserError("AE 交接入口只支持 Windows。", "dependency")
    try:
        process = subprocess.Popen([executable, "-r", str(script)], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError as exc:
        raise UserError(f"无法启动 After Effects 脚本：{exc}", "adobe")
    return {"dispatched": True, "pid": process.pid, "script_path": str(script),
            "message": "脚本已交给 After Effects；这不代表执行完成，请在 AE 查看结果。本操作不保存工程。"}
