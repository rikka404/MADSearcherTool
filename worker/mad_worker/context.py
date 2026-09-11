from pathlib import Path
from .errors import UserError
from .media import Media


class Context:
    def __init__(self, workspace, settings=None, emit=None):
        if not isinstance(workspace, str) or not workspace.strip():
            raise UserError("请设置有效的工作区目录。")
        self.workspace = Path(workspace).expanduser().resolve()
        try:
            self.workspace.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise UserError(f"无法创建工作区，请选择可写目录：{exc}")
        self.settings = settings or {}
        if not isinstance(self.settings, dict):
            raise UserError("settings 必须是 JSON 对象。")
        self.media = Media(self.settings)
        self.emit = emit or (lambda event: None)

    def progress(self, value, message):
        self.emit({"type": "progress", "progress": max(0, min(1, float(value))), "message": str(message)})
