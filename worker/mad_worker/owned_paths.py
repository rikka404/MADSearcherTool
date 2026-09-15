"""Containment checks for application-owned files; never traverse reparse points."""
import os
import stat
from pathlib import Path

from .errors import UserError


def absolute(path):
    return Path(os.path.abspath(path))


def checked(root, path):
    root, path = absolute(root), absolute(path)
    if not path.is_relative_to(root):
        raise UserError("文件不在允许操作的目录内。", "storage")
    # Check ancestors too: a configured directory may itself be a junction.
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise UserError("清理和索引资源不允许经过符号链接或目录联接：" + str(part), "storage")
    return path


def fingerprint(path):
    info = path.stat()
    return info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino
