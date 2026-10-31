# -*- coding: utf-8 -*-
"""SQLite 存储层 —— 对应 TS 版的 ``@earendil-works/pi-durable/storage/sqlite``。

核心规则:每一次状态变更都在**一个事务**里原子提交。
进程可以在任意两次提交之间死掉,已提交的数据永远完整,未提交的仿佛从未发生。

一个文件同一时刻只归一个进程所有(没有跨进程锁,与原版一致)。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

_UNSET = object()

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  kind    TEXT NOT NULL,
  content TEXT NOT NULL,
  model   TEXT
);
CREATE TABLE IF NOT EXISTS documents (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT UNIQUE,
  mode       TEXT NOT NULL DEFAULT 'input',
  status     TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS tasks (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  type          TEXT NOT NULL,
  status        TEXT NOT NULL,
  submission_id INTEGER NOT NULL,
  payload       TEXT NOT NULL,
  checkpoint    TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  type    TEXT NOT NULL,
  payload TEXT NOT NULL
);
"""


class Storage:
    """持久性的栖身之所:条目、文档、任务、提交、事件,全部落在一个 SQLite 文件里。"""

    def __init__(self, db_file):
        Path(db_file).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_file), isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._lock = threading.RLock()

    @contextmanager
    def commit(self) -> Iterator["Storage"]:
        """一次原子提交:with 块里的所有写入一起落盘,要么全部存下,要么什么都不存。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # ---------------- entries(条目) ----------------

    def append_entry(self, kind: str, content: Any, model: Any = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO entries(kind, content, model) VALUES(?, ?, ?)",
            (
                kind,
                json.dumps(content, ensure_ascii=False),
                json.dumps(model, ensure_ascii=False) if model is not None else None,
            ),
        )
        return int(cur.lastrowid)

    def list_entries(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, kind, content, model FROM entries ORDER BY id"
        ).fetchall()
        return [
            {
                "id": r[0],
                "kind": r[1],
                "content": json.loads(r[2]),
                "model": json.loads(r[3]) if r[3] else None,
            }
            for r in rows
        ]

    # ---------------- documents(文档) ----------------

    def get_doc(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute("SELECT value FROM documents WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_doc(self, key: str, value: Any) -> None:
        self._conn.execute(
            "INSERT INTO documents(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    # ---------------- submissions(提交项) ----------------

    def insert_submission(self, mode: str, request_id: Optional[str]) -> int:
        cur = self._conn.execute(
            "INSERT INTO submissions(mode, request_id, status) VALUES(?, ?, ?)",
            (mode, request_id, "open"),
        )
        return int(cur.lastrowid)

    def find_submission_id_by_request(self, request_id: str) -> Optional[int]:
        row = self._conn.execute(
            "SELECT id FROM submissions WHERE request_id = ?", (request_id,)
        ).fetchone()
        return int(row[0]) if row else None

    def get_submission(self, sid: int) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT id, request_id, mode, status FROM submissions WHERE id = ?", (sid,)
        ).fetchone()
        if not row:
            return None
        return {"id": row[0], "request_id": row[1], "mode": row[2], "status": row[3]}

    def set_submission_status(self, sid: int, status: str) -> None:
        self._conn.execute("UPDATE submissions SET status = ? WHERE id = ?", (status, sid))

    def set_submission_mode(self, sid: int, mode: str) -> None:
        self._conn.execute("UPDATE submissions SET mode = ? WHERE id = ?", (mode, sid))

    def oldest_open_input(self) -> Optional[int]:
        row = self._conn.execute(
            "SELECT id FROM submissions WHERE status = 'open' AND mode = 'input' "
            "ORDER BY id LIMIT 1"
        ).fetchone()
        return int(row[0]) if row else None

    def next_submission_needing_work(self) -> Optional[int]:
        """下一个需要推进的提交:排队中的新输入,或上一个进程留下的半途运行。"""
        row = self._conn.execute(
            "SELECT s.id FROM submissions s "
            "WHERE (s.status = 'open' AND s.mode = 'input') "
            "   OR (s.status = 'running' AND EXISTS("
            "       SELECT 1 FROM tasks t WHERE t.submission_id = s.id AND t.status != 'done')) "
            "ORDER BY s.id LIMIT 1"
        ).fetchone()
        return int(row[0]) if row else None

    def finalize_finished_submissions(self) -> int:
        """把"状态还是 running 但已经没有待办任务"的提交宣告完成。
        只在重新打开存储(harness.resume)时调用:工作与状态在同一事务里落盘,
        所以"没有待办任务"就意味着这次运行实际上已经做完了。"""
        cur = self._conn.execute(
            "UPDATE submissions SET status = 'done' WHERE status = 'running' AND NOT EXISTS("
            "SELECT 1 FROM tasks t WHERE t.submission_id = submissions.id AND t.status != 'done')"
        )
        return cur.rowcount

    def open_steers(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id FROM submissions WHERE status = 'open' AND mode = 'steer' ORDER BY id"
        ).fetchall()
        return [{"id": r[0]} for r in rows]

    # ---------------- tasks(任务) ----------------

    def insert_task(self, type_: str, submission_id: int, payload: Dict[str, Any]) -> int:
        cur = self._conn.execute(
            "INSERT INTO tasks(type, status, submission_id, payload) VALUES(?, 'pending', ?, ?)",
            (type_, submission_id, json.dumps(payload, ensure_ascii=False)),
        )
        return int(cur.lastrowid)

    def update_task(self, task_id: int, status: Optional[str] = None, checkpoint: Any = _UNSET) -> None:
        sets: List[str] = []
        args: List[Any] = []
        if status is not None:
            sets.append("status = ?")
            args.append(status)
        if checkpoint is not _UNSET:
            sets.append("checkpoint = ?")
            args.append(json.dumps(checkpoint, ensure_ascii=False))
        if sets:
            args.append(task_id)
            self._conn.execute("UPDATE tasks SET {} WHERE id = ?".format(", ".join(sets)), args)

    def next_open_task(self, submission_id: int) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT id, type, status, payload, checkpoint FROM tasks "
            "WHERE submission_id = ? AND status != 'done' ORDER BY id LIMIT 1",
            (submission_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "type": row[1],
            "status": row[2],
            "payload": json.loads(row[3]),
            "checkpoint": json.loads(row[4]) if row[4] else None,
        }

    def count_open_tasks_all(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status != 'done'"
        ).fetchone()
        return int(row[0])

    # ---------------- events(事件,供 watch 使用) ----------------

    def emit(self, type_: str, payload: Dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT INTO events(type, payload) VALUES(?, ?)",
            (type_, json.dumps(payload, ensure_ascii=False)),
        )

    def last_event_id(self) -> int:
        row = self._conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
        return int(row[0])

    def events_since(self, last_id: int) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, type, payload FROM events WHERE id > ? ORDER BY id", (last_id,)
        ).fetchall()
        return [{"id": r[0], "type": r[1], "payload": json.loads(r[2])} for r in rows]

    def close(self) -> None:
        self._conn.close()
