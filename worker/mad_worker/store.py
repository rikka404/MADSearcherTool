"""SQLite library. A complete new index replaces the old one in one transaction."""
import json
import sqlite3
import time
import uuid

from .errors import UserError


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
"""


class Store:
    def __init__(self, workspace):
        self.connection = sqlite3.connect(str(workspace / "library.sqlite3"), timeout=15)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.connection.close()

    def group(self, group_id):
        row = self.connection.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
        if not row:
            raise UserError("所选动画分组不存在，请重新选择。", "not_found")
        return dict(row)

    def groups(self):
        return [dict(row) for row in self.connection.execute("""SELECT g.*,
            (SELECT COUNT(*) FROM videos v WHERE v.group_id=g.id) video_count,
            (SELECT COUNT(*) FROM segments s JOIN videos v ON v.id=s.video_id WHERE v.group_id=g.id) segment_count
            FROM groups g ORDER BY g.created,g.id""")]

    def save_group(self, name, description, group_id=None):
        if group_id:
            self.group(group_id)
        try:
            with self.connection:
                if group_id:
                    self.connection.execute("UPDATE groups SET name=?,description=? WHERE id=?", (name, description, group_id))
                else:
                    group_id = uuid.uuid4().hex
                    self.connection.execute("INSERT INTO groups VALUES (?,?,?,?)", (group_id, name, description, time.time()))
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

    def replace_index(self, video_id, info, fingerprint, config, subtitle_path, segments):
        # All expensive work is finished before this transaction begins.
        with self.connection:
            self.video(video_id)
            self.connection.execute("DELETE FROM segments WHERE video_id=?", (video_id,))
            self.connection.executemany("""INSERT INTO segments
                (video_id,start,end,subtitle,caption,thumbnail,embedding,embedding_model) VALUES (?,?,?,?,?,?,?,?)""",
                [(video_id, s["start"], s["end"], s["subtitle"], s["caption"], s["thumbnail"],
                  json.dumps(s["embedding"], allow_nan=False) if s.get("embedding") else None, s.get("embedding_model", "")) for s in segments])
            self.connection.execute("""UPDATE videos SET duration=?,width=?,height=?,fps=?,source_size=?,source_mtime=?,
                status='indexed',subtitle_path=?,index_config=?,indexed_at=? WHERE id=?""",
                (info["duration"], info["width"], info["height"], info["fps"], fingerprint[0], fingerprint[1],
                 subtitle_path, json.dumps(config, ensure_ascii=False, sort_keys=True), time.time(), video_id))

    def segments(self, group_id):
        return [dict(row) for row in self.connection.execute("""SELECT s.*,v.path,v.name,v.duration,v.source_size,v.source_mtime,
            v.index_config FROM segments s JOIN videos v ON v.id=s.video_id WHERE v.group_id=? ORDER BY v.id,s.start""", (group_id,))]
