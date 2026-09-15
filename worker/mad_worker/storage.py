"""Reviewed cache cleanup with durable thumbnail migration and explicit boundaries."""
import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from .errors import UserError
from .owned_paths import absolute, checked, fingerprint
from .thumbnails import promote

PROJECT = Path(__file__).resolve().parents[2]
RESULT_MARKERS = ("manifest.json", "INCOMPLETE.txt", "export_report.json", "source_frames", "rgba", "masks", "ae_exports")
PROTECTED_NAMES = {"exports", "models", "characters", ".git", ".venv", "library.sqlite3",
                   "library.sqlite3-wal", "library.sqlite3-shm", "settings.json", ".activity.lock"}
OWNED_THUMBNAIL = re.compile(r"(?:[a-f0-9]{64}\.(?:jpg|png|webp)|[a-f0-9]{32}\.tmp)\Z")
CLIP_RESULT = re.compile(r".+_\d+\.\d{2}_\d+\.\d{2}_[a-f0-9]{6}\.mp4\Z", re.IGNORECASE)


def _path(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise UserError("发现无效的资源引用；为保护结果，未清理缓存。", "storage")
    path = Path(value)
    if not path.is_absolute():
        raise UserError("发现相对资源引用；无法确认其位置，未清理缓存。", "storage")
    return absolute(path)


def _extras(params):
    values = params.get("protected_paths", [])
    if not isinstance(values, list) or len(values) > 128:
        raise UserError("protected_paths必须是最多128个绝对路径的数组。", "storage")
    return {_path(value) for value in values}


def _library(workspace):
    """Read existing data without creating a database or running a schema migration."""
    database = checked(workspace, workspace / "library.sqlite3")
    if not database.exists():
        return {"thumbnails": [], "references": [], "identity": []}
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=3)) as connection:
            connection.execute("BEGIN")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > 3:
                raise UserError("数据库版本高于当前程序，不能安全清理。", "storage")
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            # Known old libraries can lack optional tables, but core tables must exist.
            thumbs = connection.execute("SELECT id,thumbnail FROM segments ORDER BY id").fetchall()
            videos = connection.execute("SELECT path,subtitle_path FROM videos ORDER BY id").fetchall()
            references = [value for row in videos for value in row if value]
            identity = [thumbs, videos]
            if "characters" in tables:
                characters = connection.execute("SELECT reference_images FROM characters ORDER BY id").fetchall()
                identity.append(characters)
                for (raw,) in characters:
                    images = json.loads(raw)
                    if not isinstance(images, list):
                        raise ValueError("Invalid reference list")
                    references.extend(images)
            if "subtitle_indexes" in tables:
                subtitles = connection.execute("SELECT config FROM subtitle_indexes ORDER BY video_id").fetchall()
                identity.append(subtitles)
                for (raw,) in subtitles:
                    config = json.loads(raw)
                    if not isinstance(config, dict):
                        raise ValueError("Invalid subtitle config")
                    if config.get("subtitle_path"):
                        references.append(config["subtitle_path"])
            return {"thumbnails": [(key, value) for key, value in thumbs if value],
                    "references": [_path(value) for value in references], "identity": identity}
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise UserError("无法完整读取索引资源引用，已停止清理；请先检查数据库。", "storage") from exc


def _roots(workspace):
    return [("运行缓存", workspace, workspace / "cache"),
            ("历史索引日志", workspace, workspace / "logs" / "index"),
            ("数据库备份", workspace, workspace / "backups"),
            ("安装包缓存", PROJECT, PROJECT / "artifacts" / "wheels"),
            ("依赖下载缓存", PROJECT, PROJECT / "models" / "pip-cache"),
            ("无引用缩略图", workspace, workspace / "thumbnails")]


def _protected(path, references):
    return path in references or any(parent in references for parent in path.parents)


def _reference_set(paths):
    result = set(paths)
    try:
        for path in paths:
            result.add(path.resolve())
    except (OSError, RuntimeError) as exc:
        raise UserError("资源引用无法解析，已停止清理以保护现有文件。", "storage") from exc
    return result


def _result_tree(directory, boundary):
    ancestors = [directory, *directory.parents]
    for ancestor in ancestors[:ancestors.index(absolute(boundary)) + 1]:
        # Fixed marker lookups avoid re-enumerating large cache directories for every file.
        if any((ancestor / name).exists() for name in RESULT_MARKERS):
            return True
        if ancestor != boundary and (ancestor / "library.sqlite3").exists():
            return True
        if ancestor != directory and ancestor.name.casefold() in {"exports", "thumbnails"}:
            return True
    return False


def _files(boundary, root, protected, kept, warnings):
    """Yield individual regular files only. Directories containing results are opaque."""
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            checked(boundary, directory)
            if not directory.exists():
                continue
            if _protected(directory, protected):
                kept.add(str(directory))
                continue
            # Protect results even if the configured cleanup root lies inside a task.
            if _result_tree(directory, boundary):
                kept.add(str(directory))
            else:
                with os.scandir(directory) as entries:
                    items = sorted(entries, key=lambda entry: entry.name)
                for item in items:
                    path = Path(item.path)
                    try:
                        checked(boundary, path)
                        if item.name.casefold() in PROTECTED_NAMES or CLIP_RESULT.fullmatch(item.name) or _protected(path, protected):
                            kept.add(str(path))
                        elif item.is_dir(follow_symlinks=False):
                            stack.append(path)
                        elif item.is_file(follow_symlinks=False):
                            yield path
                        else:
                            kept.add(str(path))
                    except (OSError, UserError) as exc:
                        kept.add(str(path))
                        warnings.add(str(exc))
                continue
        except (OSError, UserError) as exc:
            kept.add(str(directory))
            warnings.add(str(exc))


def _plan(params, ctx):
    workspace = checked(ctx.workspace, ctx.workspace)
    library = _library(workspace)
    extra = _extras(params)
    protected = _reference_set(set(library["references"]) | extra)
    thumb_paths = {_path(value) for _, value in library["thumbnails"]}
    # Old cache-backed thumbnails can be removed only after a successful migration.
    legacy = {path for path in thumb_paths if path.is_relative_to(workspace / "cache")}
    protected |= _reference_set(thumb_paths - legacy)
    protected |= {workspace / name for name in ("exports", "characters", "models")}
    kept, warnings, files, groups = set(), set(), {}, []
    missing = []
    for path in sorted(legacy):
        try:
            checked(workspace, path)
            fingerprint(path)
        except FileNotFoundError:
            missing.append(str(path))
        except (OSError, UserError) as exc:
            # Unsafe legacy references are retained, never followed or removed.
            protected |= _reference_set({path})
            warnings.add(str(exc))
    migratable = {path for path in legacy if not _protected(path, protected)} - {Path(p) for p in missing}
    for category, boundary, root in _roots(workspace):
        count, size = 0, 0
        for path in _files(boundary, root, protected, kept, warnings):
            if category == "无引用缩略图" and not OWNED_THUMBNAIL.fullmatch(path.name):
                kept.add(str(path))
                continue
            try:
                info = fingerprint(path)
            except OSError as exc:
                warnings.add(str(exc))
                continue
            files[str(path)] = (str(boundary), str(root), info, category)
            count += 1
            size += info[0]
        groups.append({"name": category, "path": str(root), "files": count, "bytes": size})
    # A thumbnail inside a protected result or selected input tree needs no migration.
    migratable = {path for path in migratable if str(path) in files}
    migration_bytes = sum(files[str(path)][2][0] for path in migratable)
    if missing:
        warnings.add(f"已有{len(missing)}张缩略图缺失，本次不能恢复，相关索引记录仍保留。")
    digest_data = [str(workspace), sorted(str(p) for p in extra), library["identity"], sorted(files.items()),
                   sorted(str(p) for p in migratable)]
    token = hashlib.sha256(json.dumps(digest_data, ensure_ascii=True, sort_keys=True).encode()).hexdigest()
    summary = {"plan_token": token, "workspace": str(workspace), "index_directory": str(workspace),
               "groups": groups, "files": len(files), "bytes": sum(item[2][0] for item in files.values()),
               "thumbnail_migration_count": len(migratable), "thumbnail_copy_bytes_upper_bound": migration_bytes,
               "missing_thumbnail_count": len(missing), "protected_count": len(kept),
               "warnings": sorted(warnings)[:20]}
    return summary, files, library, migratable, protected


def scan(params, ctx):
    ctx.progress(0.1, "正在检查缓存占用和索引图片引用…")
    return _plan(params, ctx)[0]


def _migrate(ctx, library, legacy):
    if not legacy:
        return 0
    mapping = {}
    for index, path in enumerate(sorted(legacy)):
        ctx.progress(0.05 + 0.35 * index / len(legacy), f"正在保护索引缩略图：{index + 1}/{len(legacy)}…")
        target = promote(ctx.workspace, path)
        mapping[path] = target
    # All copies exist before changing any DB row; an interruption keeps old files.
    database = checked(ctx.workspace, ctx.workspace / "library.sqlite3")
    with closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=3)) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT id,thumbnail FROM segments WHERE thumbnail != '' ORDER BY id").fetchall()
            if current != library["thumbnails"]:
                raise UserError("索引引用已变化，请重新扫描缓存；未删除文件。", "storage")
            for key, old in current:
                if _path(old) in mapping:
                    connection.execute("UPDATE segments SET thumbnail=? WHERE id=? AND thumbnail=?", (mapping[_path(old)], key, old))
    return len(mapping)


def clean(params, ctx):
    token = params.get("plan_token")
    if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}", token):
        raise UserError("请先扫描缓存，确认清理范围后提交扫描令牌。", "storage")
    summary, files, library, legacy, protected = _plan(params, ctx)
    if token != summary["plan_token"]:
        raise UserError("缓存或索引引用已经变化，请重新扫描并确认清理范围。", "storage")
    # Measure only new permanent files; existing deduplicated thumbnails are retained.
    thumbnail_dir = checked(ctx.workspace, ctx.workspace / "thumbnails")
    before = {p.name for p in thumbnail_dir.glob("*")}
    migrated = _migrate(ctx, library, legacy)
    added_bytes = sum(p.stat().st_size for p in thumbnail_dir.glob("*")
                      if p.name not in before and p.is_file())
    current_library = _library(ctx.workspace)
    protected |= _reference_set(set(current_library["references"]) | {_path(value) for _, value in current_library["thumbnails"]})
    removed_bytes, removed, failures, failed_count, skipped = 0, 0, [], 0, 0
    directories = set()
    for index, (name, (boundary, root, expected, category)) in enumerate(sorted(files.items())):
        path, boundary, root = Path(name), Path(boundary), Path(root)
        try:
            checked(boundary, path)
            if not path.is_relative_to(root) or _protected(path, protected) or fingerprint(path) != expected:
                skipped += 1
                continue
            # Recheck markers immediately before each unlink, including newly created tasks.
            if _result_tree(path.parent, boundary):
                skipped += 1
                continue
            path.unlink()
            removed_bytes += expected[0]
            removed += 1
            directories.update(parent for parent in path.parents if parent != root and parent.is_relative_to(root))
        except FileNotFoundError:
            skipped += 1
        except (OSError, UserError) as exc:
            failed_count += 1
            if len(failures) < 20:
                failures.append({"path": str(path), "message": str(exc)})
        if index % 100 == 0:
            ctx.progress(0.4 + 0.55 * (index + 1) / max(1, len(files)), f"已清理{removed}个缓存文件…")
    for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        try:
            checked(directory.parent, directory)
            if not _protected(directory, protected):
                directory.rmdir()  # Empty only; never recursively delete an uninspected tree.
        except (OSError, UserError):
            pass
    return {"removed_files": removed, "removed_bytes": removed_bytes, "thumbnail_added_bytes": added_bytes,
            "net_reclaimed_bytes": removed_bytes - added_bytes, "migrated_thumbnails": migrated,
            "skipped_files": skipped, "failed_files": failed_count, "failures": failures, "warnings": summary["warnings"],
            "index_directory": str(ctx.workspace)}
