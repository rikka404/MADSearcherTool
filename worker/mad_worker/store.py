"""SQLite library. A complete new index replaces the old one in one transaction."""
import json
import sqlite3
import time
import uuid
from contextlib import closing

from .errors import UserError
from .characters import signature as character_signature


SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
 id TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
 description TEXT NOT NULL DEFAULT '', created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS videos (
 id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
 path TEXT NOT NULL, path_key TEXT NOT NULL, name TEXT NOT NULL,
 duration REAL NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL, fps REAL NOT NULL,
 source_size INTEGER NOT NULL, source_mtime INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'not_indexed', subtitle_path TEXT NOT NULL DEFAULT '',
 index_config TEXT NOT NULL DEFAULT '', indexed_at REAL,
 UNIQUE(group_id,path_key));
CREATE TABLE IF NOT EXISTS segments (
 id INTEGER PRIMARY KEY AUTOINCREMENT, video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
 start REAL NOT NULL, end REAL NOT NULL, subtitle TEXT NOT NULL DEFAULT '',
 caption TEXT NOT NULL DEFAULT '', thumbnail TEXT NOT NULL DEFAULT '',
 embedding TEXT, embedding_model TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS segments_video_time ON segments(video_id,start);
CREATE INDEX IF NOT EXISTS videos_group ON videos(group_id);
CREATE TABLE IF NOT EXISTS characters (
 id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
 name TEXT NOT NULL DEFAULT '', aliases TEXT NOT NULL DEFAULT '[]',
 work_info TEXT NOT NULL DEFAULT '', identity TEXT NOT NULL DEFAULT '', appearance TEXT NOT NULL DEFAULT '',
 reference_images TEXT NOT NULL DEFAULT '[]', position INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS characters_group ON characters(group_id,position);
CREATE TABLE IF NOT EXISTS segment_characters (
 segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
 character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
 confidence TEXT NOT NULL CHECK(confidence IN ('high','medium')),
 evidence TEXT NOT NULL, frame_indices TEXT NOT NULL,
 PRIMARY KEY(segment_id,character_id));
CREATE INDEX IF NOT EXISTS segment_characters_character ON segment_characters(character_id);
CREATE TABLE IF NOT EXISTS shots (
 video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
 position INTEGER NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
 PRIMARY KEY(video_id,position));
"""


SUBTITLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS subtitle_indexes (
 video_id TEXT PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE, config TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS subtitle_cues (
 video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
 position INTEGER NOT NULL, start REAL NOT NULL, end REAL NOT NULL, text TEXT NOT NULL, lane TEXT NOT NULL,
 PRIMARY KEY(video_id,position));
CREATE TABLE IF NOT EXISTS subtitle_units (
 id INTEGER PRIMARY KEY AUTOINCREMENT, video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
 start REAL NOT NULL, end REAL NOT NULL, text TEXT NOT NULL, kind TEXT NOT NULL, cue_ids TEXT NOT NULL,
 embedding TEXT, embedding_model TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS subtitle_units_video_time ON subtitle_units(video_id,start);
"""


class Store:
    def __init__(self, workspace):
        self.workspace = workspace
        database = workspace / "library.sqlite3"
        existed = database.is_file() and database.stat().st_size > 0
        self.connection = sqlite3.connect(str(database), timeout=15)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA journal_mode=WAL")
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version > 3:
                raise UserError("数据库由更新版本创建，请使用对应版本的程序。")
            if version < 3:
                if existed:
                    backups = workspace / "backups"
                    backups.mkdir(parents=True, exist_ok=True)
                    with closing(sqlite3.connect(str(backups / f"library_before_v3_{uuid.uuid4().hex}.sqlite3"))) as backup:
                        self.connection.backup(backup)
                additions = ("ALTER TABLE segments ADD COLUMN shot_id TEXT NOT NULL DEFAULT '';\n"
                             "ALTER TABLE segments ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}';\n") if version < 2 else ""
                self.connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\n" + additions + SUBTITLE_SCHEMA
                                             + "\nPRAGMA user_version=3;\nCOMMIT;")
        except BaseException:
            self.connection.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.connection.close()

    def group(self, group_id):
        row = self.connection.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
        if not row:
            raise UserError("所选动画分组不存在，请重新选择。", "not_found")
        return dict(row) | {"characters": self.characters(group_id)}

    def characters(self, group_id):
        result = []
        for row in self.connection.execute("SELECT * FROM characters WHERE group_id=? ORDER BY position,id", (group_id,)):
            card = dict(row)
            for key in ("aliases", "reference_images"):
                card[key] = json.loads(card[key])
            result.append({k: v for k, v in card.items() if k not in ("group_id", "position")})
        return result

    def groups(self):
        return [dict(row) | {"characters": self.characters(row["id"])} for row in self.connection.execute("""SELECT g.*,
            (SELECT COUNT(*) FROM videos v WHERE v.group_id=g.id) video_count,
            (SELECT COUNT(*) FROM segments s JOIN videos v ON v.id=s.video_id WHERE v.group_id=g.id) segment_count
            FROM groups g ORDER BY g.created,g.id""")]

    def save_group(self, name, description, group_id=None, characters=None):
        if group_id:
            self.group(group_id)
        try:
            with self.connection:
                if group_id:
                    self.connection.execute("UPDATE groups SET name=?,description=? WHERE id=?", (name, description, group_id))
                else:
                    group_id = uuid.uuid4().hex
                    self.connection.execute("INSERT INTO groups VALUES (?,?,?,?)", (group_id, name, description, time.time()))
                if characters is not None:
                    keep = {c["id"] for c in characters}
                    for card in self.characters(group_id):
                        if card["id"] not in keep:
                            self.connection.execute("DELETE FROM characters WHERE id=?", (card["id"],))
                    for position, card in enumerate(characters):
                        owner = self.connection.execute("SELECT group_id FROM characters WHERE id=?", (card["id"],)).fetchone()
                        if owner and owner[0] != group_id:
                            raise UserError("角色ID属于其他分组，请重新添加该角色。")
                        self.connection.execute("""INSERT INTO characters
                            (id,group_id,name,aliases,work_info,identity,appearance,reference_images,position)
                            VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                            name=excluded.name,aliases=excluded.aliases,work_info=excluded.work_info,
                            identity=excluded.identity,appearance=excluded.appearance,
                            reference_images=excluded.reference_images,position=excluded.position""",
                            (card["id"], group_id, card["name"], json.dumps(card["aliases"], ensure_ascii=False),
                             card["work_info"], card["identity"], card["appearance"],
                             json.dumps(card["reference_images"], ensure_ascii=False), position))
        except sqlite3.IntegrityError:
            raise UserError("已存在同名动画分组，请更换名称。")
        return next(group for group in self.groups() if group["id"] == group_id)

    def delete_group(self, group_id):
        self.group(group_id)
        with self.connection:
            self.connection.execute("DELETE FROM groups WHERE id=?", (group_id,))

    def video(self, video_id):
        row = self.connection.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
        if not row:
            raise UserError("所选视频不存在，请重新导入或选择。", "not_found")
        return dict(row)

    def videos(self, group_id):
        self.group(group_id)
        return [dict(row) for row in self.connection.execute("SELECT * FROM videos WHERE group_id=? ORDER BY name,id", (group_id,))]

    def add_videos(self, group_id, records):
        self.group(group_id)
        with self.connection:
            for record in records:
                self.connection.execute("""INSERT INTO videos
                    (id,group_id,path,path_key,name,duration,width,height,fps,source_size,source_mtime)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (record["id"], group_id, record["path"], record["path_key"], record["name"],
                    record["duration"], record["width"], record["height"], record["fps"], record["source_size"], record["source_mtime"]))

    def remove_video(self, video_id):
        self.video(video_id)
        with self.connection:
            self.connection.execute("DELETE FROM videos WHERE id=?", (video_id,))

    def replace_index(self, video_id, info, fingerprint, config, subtitle_path, segments, *, shot_ranges=(), subtitle_index=None):
        from .thumbnails import promote
        # Never store a disposable sampling path in a newly committed index.
        # Keep the caller's cache records unchanged so resume identity is stable.
        segments = [dict(s, thumbnail=promote(self.workspace, s["thumbnail"])) if s.get("thumbnail") else dict(s)
                    for s in segments]
        # All expensive work is finished before this transaction begins.
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            video = self.video(video_id)
            group = self.group(video["group_id"])
            if config.get("context", "") != group["description"]:
                raise UserError("分组说明在索引时发生变化，已保留旧索引；请重试。")
            if config.get("visual") and config.get("character_signature") != character_signature(group["characters"]):
                raise UserError("角色资料在索引时发生变化，已保留旧索引；请重试。")
            self.connection.execute("DELETE FROM segments WHERE video_id=?", (video_id,))
            self.connection.execute("DELETE FROM shots WHERE video_id=?", (video_id,))
            self.connection.executemany("INSERT INTO shots VALUES (?,?,?,?)",
                [(video_id, i, start, end) for i, (start, end) in enumerate(shot_ranges)])
            allowed = {c["id"] for c in group["characters"]}
            for s in segments:
                cursor = self.connection.execute("""INSERT INTO segments
                    (video_id,start,end,subtitle,caption,thumbnail,embedding,embedding_model,shot_id,metadata) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (video_id, s["start"], s["end"], s["subtitle"], s["caption"], s["thumbnail"],
                     json.dumps(s["embedding"], allow_nan=False) if s.get("embedding") else None, s.get("embedding_model", ""),
                     s.get("shot_id", ""), json.dumps(s.get("metadata", {}), allow_nan=False)))
                for match in s.get("character_matches", []):
                    if match["character_id"] not in allowed:
                        raise UserError("角色资料在索引时发生变化，请重新索引。")
                    self.connection.execute("INSERT INTO segment_characters VALUES (?,?,?,?,?)",
                        (cursor.lastrowid, match["character_id"], match["confidence"], match["evidence"],
                         json.dumps(match["frame_indices"])))
            self.connection.execute("""UPDATE videos SET duration=?,width=?,height=?,fps=?,source_size=?,source_mtime=?,
                status='indexed',subtitle_path=?,index_config=?,indexed_at=? WHERE id=?""",
                (info["duration"], info["width"], info["height"], info["fps"], fingerprint[0], fingerprint[1],
                 subtitle_path, json.dumps(config, ensure_ascii=False, sort_keys=True), time.time(), video_id))
            if subtitle_index is not None:
                self._replace_subtitles(video_id, subtitle_index)

    def _replace_subtitles(self, video_id, prepared):
        self.connection.execute("DELETE FROM subtitle_cues WHERE video_id=?", (video_id,))
        self.connection.execute("DELETE FROM subtitle_units WHERE video_id=?", (video_id,))
        self.connection.execute("INSERT INTO subtitle_indexes VALUES (?,?) ON CONFLICT(video_id) DO UPDATE SET config=excluded.config",
                                (video_id, json.dumps(prepared["config"], ensure_ascii=False, allow_nan=False)))
        self.connection.executemany("INSERT INTO subtitle_cues VALUES (?,?,?,?,?,?)",
            [(video_id, i, cue["start"], cue["end"], cue["text"], cue["lane"]) for i, cue in enumerate(prepared["cues"])])
        self.connection.executemany("INSERT INTO subtitle_units (video_id,start,end,text,kind,cue_ids,embedding,embedding_model) VALUES (?,?,?,?,?,?,?,?)",
            [(video_id, u["start"], u["end"], u["text"], u["kind"], json.dumps(u["cue_ids"]),
              json.dumps(u["embedding"], allow_nan=False) if u.get("embedding") else None, u.get("embedding_model", ""))
             for u in prepared["units"]])

    def replace_subtitles(self, video_id, prepared):
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            video = self.video(video_id)
            if [video["source_size"], video["source_mtime"]] != prepared["config"]["source"]:
                raise UserError("素材记录在补建过程中发生变化，已保留旧台词索引。")
            self._replace_subtitles(video_id, prepared)

    def subtitle_indexes(self, group_id):
        return {row["video_id"]: json.loads(row["config"]) for row in self.connection.execute(
            "SELECT s.* FROM subtitle_indexes s JOIN videos v ON v.id=s.video_id WHERE v.group_id=?", (group_id,))}

    def subtitle_cues(self, video_id):
        return [dict(row) for row in self.connection.execute(
            "SELECT start,end,text,lane FROM subtitle_cues WHERE video_id=? ORDER BY position", (video_id,))]

    def subtitle_units(self, group_id, video_id=None):
        return [dict(row) | {"cue_ids": json.loads(row["cue_ids"])} for row in self.connection.execute(
            "SELECT s.* FROM subtitle_units s JOIN videos v ON v.id=s.video_id WHERE v.group_id=?"
            + (" AND s.video_id=?" if video_id else "") + " ORDER BY s.video_id,s.start,s.id",
            (group_id, video_id) if video_id else (group_id,))]

    def segments(self, group_id):
        rows = [dict(row) for row in self.connection.execute("""SELECT s.*,v.path,v.name,v.duration,v.source_size,v.source_mtime,
            v.index_config FROM segments s JOIN videos v ON v.id=s.video_id WHERE v.group_id=? ORDER BY v.id,s.start""", (group_id,))]
        matches = {}
        for row in self.connection.execute("""SELECT sc.* FROM segment_characters sc
                JOIN segments s ON s.id=sc.segment_id JOIN videos v ON v.id=s.video_id WHERE v.group_id=?""", (group_id,)):
            item = dict(row)
            item["frame_indices"] = json.loads(item["frame_indices"])
            matches.setdefault(item.pop("segment_id"), []).append(item)
        return [{**row, "metadata": json.loads(row["metadata"]), "character_matches": matches.get(row["id"], [])} for row in rows]

    def shot_ranges(self, group_id):
        result = {}
        for row in self.connection.execute("""SELECT s.video_id,s.start,s.end FROM shots s
                JOIN videos v ON v.id=s.video_id WHERE v.group_id=? ORDER BY s.video_id,s.position""", (group_id,)):
            result.setdefault(row["video_id"], []).append((row["start"], row["end"]))
        return result
