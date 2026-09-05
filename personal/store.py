"""
个人数据中心 —— SQLite 存储层。

承载用户个人数据，全部按 user_id 隔离：
  - schedule 表：课程表（day_of_week 0=周一；weeks 为逗号分隔的教学周列表）
  - todos 表：待办 / DDL / 考试（kind 区分），due_at 为 "YYYY-MM-DD" 或 "YYYY-MM-DD HH:MM"

实现说明：
  - stdlib sqlite3，零第三方依赖；数据库路径由 ECHOGUIDE_DB_PATH 配置，
    默认 <项目根>/data/echoguide.db（Docker 部署时挂载持久化）。
  - 所有方法 async，内部经 asyncio.to_thread 执行同步实现，避免阻塞事件循环。
  - 每次操作新建连接（本地文件开销可忽略），天然规避线程共享连接问题。
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import date, datetime, timedelta
import json
import logging
import os
import pathlib
import sqlite3
from typing import Any, Dict, List, Optional

from core.sqlite import sqlite_session

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schedule (
    user_id     TEXT NOT NULL,
    course      TEXT NOT NULL,
    day_of_week INTEGER NOT NULL,          -- 0=周一 … 6=周日
    start_time  TEXT NOT NULL,             -- "08:30"
    end_time    TEXT NOT NULL,             -- "10:05"
    location    TEXT NOT NULL DEFAULT '',
    weeks       TEXT NOT NULL DEFAULT '',  -- 教学周列表 "1,3,5-8"（空=所有周）
    note        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_schedule_user ON schedule(user_id);

CREATE TABLE IF NOT EXISTS todos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL,
    content      TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'todo',  -- todo / ddl / exam
    due_at       TEXT,                          -- "YYYY-MM-DD" 或 "YYYY-MM-DD HH:MM"
    done         INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    completed_at TEXT,
    source_event_id INTEGER,
    source_url TEXT,
    source_deadline TEXT,
    action_plan_id TEXT,
    evidence TEXT                  -- 行动步骤的原文依据（LLM 起草步骤的溯源字段）
);
CREATE INDEX IF NOT EXISTS idx_todos_user ON todos(user_id);

CREATE TABLE IF NOT EXISTS student_profiles (
    user_id       TEXT PRIMARY KEY,
    college       TEXT NOT NULL DEFAULT '',
    major         TEXT NOT NULL DEFAULT '',
    grade         TEXT NOT NULL DEFAULT '',
    education     TEXT NOT NULL DEFAULT '',
    interests_json TEXT NOT NULL DEFAULT '[]',
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_briefings (
    user_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,               -- today / inbox / free_advice
    day         TEXT NOT NULL,               -- YYYY-MM-DD（本地时区）
    fingerprint TEXT NOT NULL,               -- 输入数据的规范化哈希：数据变了才重新生成
    content     TEXT NOT NULL,
    model       TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, kind, day)
);
"""


def _default_db_path() -> str:
    root = pathlib.Path(__file__).parent.parent.resolve()
    return os.getenv("ECHOGUIDE_DB_PATH", str(root / "data" / "echoguide.db"))


class PersonalStore:
    """SQLite 个人数据存储（按 user_id 隔离的课程表 / 待办 / DDL）。"""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or _default_db_path()
        pathlib.Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ── 基础 ──────────────────────────────────────────────────────────────────

    @contextmanager
    def _connect(self):
        """短生命周期连接（WAL/busy_timeout/commit/close 见 core.sqlite.session）。

        原实现返回裸连接，`with self._connect()` 只 commit 不 close，依赖
        CPython 引用计数回收文件句柄；现在与 layered_store 语义统一。
        """
        with sqlite_session(self.db_path) as conn:
            yield conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            # 兼容 P0 已部署的数据库：CREATE TABLE 不会为既有表补列。
            columns = {row[1] for row in conn.execute("PRAGMA table_info(todos)")}
            for name, definition in {
                "source_event_id": "INTEGER",
                "source_url": "TEXT",
                "source_deadline": "TEXT",
                "action_plan_id": "TEXT",
                "evidence": "TEXT",
            }.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE todos ADD COLUMN {name} {definition}")

    async def _run(self, fn, *args, **kwargs):
        """同步实现包装为异步，避免阻塞事件循环。"""
        return await asyncio.to_thread(fn, *args, **kwargs)

    # ── 课程表 ────────────────────────────────────────────────────────────────

    async def replace_schedule(self, user_id: str, courses: List[Dict[str, Any]]) -> int:
        """整表替换：清空该用户旧课表后写入新课表（重导语义）。"""
        return await self._run(self._replace_schedule_sync, user_id, courses)

    def _replace_schedule_sync(self, user_id: str, courses: List[Dict[str, Any]]) -> int:
        with self._connect() as conn:
            conn.execute("DELETE FROM schedule WHERE user_id = ?", (user_id,))
            conn.executemany(
                """INSERT INTO schedule
                   (user_id, course, day_of_week, start_time, end_time, location, weeks)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        user_id,
                        c["course"],
                        int(c["day_of_week"]),
                        c["start_time"],
                        c["end_time"],
                        c.get("location", ""),
                        c.get("weeks", ""),
                    )
                    for c in courses
                ],
            )
        return len(courses)

    async def get_schedule(self, user_id: str) -> List[Dict[str, Any]]:
        """返回该用户全部课程，按（星期, 开始时间）排序。"""
        return await self._run(self._get_schedule_sync, user_id)

    def _get_schedule_sync(self, user_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM schedule WHERE user_id = ? ORDER BY day_of_week, start_time",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    async def count_schedule(self, user_id: str) -> int:
        return await self._run(self._count_schedule_sync, user_id)

    def _count_schedule_sync(self, user_id: str) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM schedule WHERE user_id = ?", (user_id,)).fetchone()[0]

    async def clear_schedule(self, user_id: str) -> None:
        await self._run(self._clear_schedule_sync, user_id)

    def _clear_schedule_sync(self, user_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM schedule WHERE user_id = ?", (user_id,))

    # ── 待办 / DDL / 考试 ─────────────────────────────────────────────────────

    async def add_todo(
        self,
        user_id: str,
        content: str,
        kind: str = "todo",
        due_at: Optional[str] = None,
        source_event_id: Optional[int] = None,
        source_url: Optional[str] = None,
        source_deadline: Optional[str] = None,
        action_plan_id: Optional[str] = None,
        evidence: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self._run(
            self._add_todo_sync,
            user_id,
            content,
            kind,
            due_at,
            source_event_id,
            source_url,
            source_deadline,
            action_plan_id,
            evidence,
        )

    def _add_todo_sync(
        self,
        user_id: str,
        content: str,
        kind: str,
        due_at: Optional[str],
        source_event_id: Optional[int],
        source_url: Optional[str],
        source_deadline: Optional[str],
        action_plan_id: Optional[str],
        evidence: Optional[str],
    ) -> Dict[str, Any]:
        created = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO todos (user_id, content, kind, due_at, created_at, source_event_id, source_url, source_deadline, action_plan_id, evidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    content,
                    kind,
                    due_at or None,
                    created,
                    source_event_id,
                    source_url,
                    source_deadline,
                    action_plan_id,
                    evidence,
                ),
            )
            return self._todo_row_to_dict(conn.execute("SELECT * FROM todos WHERE id = ?", (cur.lastrowid,)).fetchone())

    async def list_todos(
        self,
        user_id: str,
        status: str = "open",  # open / done / all
        kinds: Optional[List[str]] = None,
        due_before: Optional[date] = None,
    ) -> List[Dict[str, Any]]:
        return await self._run(self._list_todos_sync, user_id, status, kinds, due_before)

    def _list_todos_sync(
        self,
        user_id: str,
        status: str,
        kinds: Optional[List[str]],
        due_before: Optional[date],
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM todos WHERE user_id = ?"
        args: List[Any] = [user_id]
        if status == "open":
            sql += " AND done = 0"
        elif status == "done":
            sql += " AND done = 1"
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args.extend(kinds)
        if due_before is not None:
            sql += " AND due_at IS NOT NULL AND substr(due_at, 1, 10) <= ?"
            args.append(due_before.isoformat())
        sql += " ORDER BY done ASC, due_at IS NULL, due_at ASC, id DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [self._todo_row_to_dict(r) for r in rows]

    async def get_todo(self, user_id: str, todo_id: int) -> Optional[Dict[str, Any]]:
        return await self._run(self._get_todo_sync, user_id, todo_id)

    def _get_todo_sync(self, user_id: str, todo_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM todos WHERE id = ? AND user_id = ?", (todo_id, user_id)).fetchone()
        return self._todo_row_to_dict(row) if row else None

    async def set_todo_done(self, user_id: str, todo_id: int, done: bool = True) -> bool:
        """完成/恢复待办，返回是否成功（不存在或不属于该用户时返回 False）。"""
        return await self._run(self._set_todo_done_sync, user_id, todo_id, done)

    def _set_todo_done_sync(self, user_id: str, todo_id: int, done: bool) -> bool:
        completed = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S") if done else None
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE todos SET done = ?, completed_at = ?
                   WHERE id = ? AND user_id = ?""",
                (1 if done else 0, completed, todo_id, user_id),
            )
        return cur.rowcount > 0

    async def delete_todo(self, user_id: str, todo_id: int) -> bool:
        return await self._run(self._delete_todo_sync, user_id, todo_id)

    def _delete_todo_sync(self, user_id: str, todo_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM todos WHERE id = ? AND user_id = ?", (todo_id, user_id))
        return cur.rowcount > 0

    async def update_todo(
        self,
        user_id: str,
        todo_id: int,
        *,
        content: Optional[str] = None,
        kind: Optional[str] = None,
        due_at: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        return await self._run(self._update_todo_sync, user_id, todo_id, content, kind, due_at)

    def _update_todo_sync(
        self,
        user_id: str,
        todo_id: int,
        content: Optional[str],
        kind: Optional[str],
        due_at: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        fields: List[str] = []
        args: List[Any] = []
        if content is not None:
            fields.append("content = ?")
            args.append(content)
        if kind is not None:
            fields.append("kind = ?")
            args.append(kind)
        if due_at is not None:
            fields.append("due_at = ?")
            args.append(due_at or None)
        if not fields:
            return None
        args.extend([todo_id, user_id])
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE todos SET {', '.join(fields)} WHERE id = ? AND user_id = ?",
                args,
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
        return self._todo_row_to_dict(row)

    # ── 稳定学生画像（通知筛选使用，不依赖自由文本 Memory） ────────────────

    async def get_profile(self, user_id: str) -> Dict[str, Any]:
        return await self._run(self._get_profile_sync, user_id)

    def _get_profile_sync(self, user_id: str) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM student_profiles WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return {"college": "", "major": "", "grade": "", "education": "", "interests": []}
        result = dict(row)
        result["interests"] = json.loads(result.pop("interests_json") or "[]")
        result.pop("user_id", None)
        result.pop("updated_at", None)
        return result

    async def save_profile(self, user_id: str, profile: Dict[str, Any]) -> Dict[str, Any]:
        return await self._run(self._save_profile_sync, user_id, profile)

    def _save_profile_sync(self, user_id: str, profile: Dict[str, Any]) -> Dict[str, Any]:
        clean = {
            "college": str(profile.get("college", "")).strip(),
            "major": str(profile.get("major", "")).strip(),
            "grade": str(profile.get("grade", "")).strip(),
            "education": str(profile.get("education", "")).strip(),
            "interests": [str(v).strip() for v in profile.get("interests", []) if str(v).strip()][:12],
        }
        updated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO student_profiles (user_id, college, major, grade, education, interests_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET college=excluded.college, major=excluded.major,
                   grade=excluded.grade, education=excluded.education, interests_json=excluded.interests_json,
                   updated_at=excluded.updated_at""",
                (
                    user_id,
                    clean["college"],
                    clean["major"],
                    clean["grade"],
                    clean["education"],
                    json.dumps(clean["interests"], ensure_ascii=False),
                    updated,
                ),
            )
        return clean

    # ── LLM 简报缓存（按用户/类型/日期隔离，数据指纹变了才重新生成） ─────────

    async def get_llm_briefing(self, user_id: str, kind: str, day: str, fingerprint: str) -> Optional[str]:
        return await self._run(self._get_llm_briefing_sync, user_id, kind, day, fingerprint)

    def _get_llm_briefing_sync(self, user_id: str, kind: str, day: str, fingerprint: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT content, fingerprint FROM llm_briefings WHERE user_id=? AND kind=? AND day=?",
                (user_id, kind, day),
            ).fetchone()
        if row is None or row["fingerprint"] != fingerprint:
            return None
        return row["content"]

    async def put_llm_briefing(self, user_id: str, kind: str, day: str, fingerprint: str, content: str, model: str = "") -> None:
        await self._run(self._put_llm_briefing_sync, user_id, kind, day, fingerprint, content, model)

    def _put_llm_briefing_sync(self, user_id: str, kind: str, day: str, fingerprint: str, content: str, model: str) -> None:
        now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO llm_briefings (user_id, kind, day, fingerprint, content, model, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, kind, day) DO UPDATE SET fingerprint=excluded.fingerprint,
                   content=excluded.content, model=excluded.model, updated_at=excluded.updated_at""",
                (user_id, kind, day, fingerprint, content, model, now),
            )
            # 只保留最近 7 天：简报是可再生缓存，老数据没有保留价值（全表清理）
            conn.execute(
                "DELETE FROM llm_briefings WHERE day < ?",
                ((datetime.now().astimezone() - timedelta(days=7)).strftime("%Y-%m-%d"),),
            )

    @staticmethod
    def _todo_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        d["done"] = bool(d["done"])
        return d
