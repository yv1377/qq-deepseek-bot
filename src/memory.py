"""记忆层。

分六层：
1. 群聊背景缓存（GroupHistory，内存环形缓冲）——给模型提供"刚才群里在聊什么"；
2. 会话消息（SQLite messages 表）——每个群 / 每个私聊独立会话；
3. 会话摘要（SQLite summaries 表）——老消息压缩后长期保留，防止上下文爆炸；
4. 原文归档（SQLite archive 表）——摘要压缩掉的原文不丢，recall_chat 可以找回原话；
5. 笔记（SQLite notes / user_notes 表）——长期事实（工具写入 + 记忆管家自动整理）；
6. 待办（SQLite todos 表）——bot 答应过 / 群友托付的未完成事项，形成闭环。
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL,
    role       TEXT    NOT NULL,
    name       TEXT,
    content    TEXT    NOT NULL,
    ts         REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS summaries (
    session_id TEXT PRIMARY KEY,
    content    TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    created_at REAL    NOT NULL,
    source     TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_notes_session ON notes(session_id, id);

CREATE TABLE IF NOT EXISTS user_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    content    TEXT    NOT NULL,
    created_at REAL    NOT NULL,
    source     TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_user_notes_user ON user_notes(user_id, id);

-- 原文归档：摘要压缩后原文搬到这里，不再直接删除，供 recall_chat 找回原话
CREATE TABLE IF NOT EXISTS archive (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL,
    role       TEXT    NOT NULL,
    name       TEXT,
    content    TEXT    NOT NULL,
    ts         REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_archive_session ON archive(session_id, id);
CREATE INDEX IF NOT EXISTS idx_archive_ts ON archive(ts);

-- 待办 / 承诺：把「我答应过的事」变成可追踪、能闭环的任务
CREATE TABLE IF NOT EXISTS todos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'open',
    created_at REAL    NOT NULL,
    done_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_todos_session ON todos(session_id, status);

-- 图片记忆：把“看过并理解过的图”持久化，之后聊天可以自然引用
CREATE TABLE IF NOT EXISTS image_memory (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    user_id     INTEGER NOT NULL DEFAULT 0,
    name        TEXT    NOT NULL DEFAULT '',
    description TEXT    NOT NULL,
    expression  TEXT    NOT NULL DEFAULT '',
    media_key   TEXT    NOT NULL DEFAULT '',
    created_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_image_memory_session ON image_memory(session_id, id);

-- 定时提醒：到点由后台调度器主动发出去
CREATE TABLE IF NOT EXISTS reminders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL,
    user_id    INTEGER NOT NULL,
    content    TEXT    NOT NULL,
    due_at     REAL    NOT NULL,
    created_at REAL    NOT NULL,
    done       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(done, due_at);

-- 亲密度：每群每个人和她的互动累计，关系越熟说话越随便
CREATE TABLE IF NOT EXISTS affinity (
    user_id    INTEGER NOT NULL,
    group_id   INTEGER NOT NULL,
    score      INTEGER NOT NULL DEFAULT 0,
    updated_at REAL    NOT NULL,
    PRIMARY KEY (user_id, group_id)
);

-- 主动关心：记下值得惦记的事（考试/生病/加班…），到期主动问一句
CREATE TABLE IF NOT EXISTS care_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL,
    user_id    INTEGER NOT NULL,
    name       TEXT    NOT NULL DEFAULT '',
    kind       TEXT    NOT NULL,
    detail     TEXT    NOT NULL DEFAULT '',
    created_at REAL    NOT NULL,
    due_at     REAL    NOT NULL,
    sent       INTEGER NOT NULL DEFAULT 0,
    sent_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_care_due ON care_events(sent, due_at);
"""


@dataclass(slots=True)
class ChatLine:
    name: str
    text: str
    ts: float
    uid: int = 0


def speaker_label(name: str, uid: int | None) -> str:
    """统一的说话人标签：昵称 + QQ 号，让模型分得清谁是谁。

    同昵称不同人、同一个人改昵称，都能靠 QQ 号区分/合并。
    """
    name = str(name or "").strip() or "群友"
    if uid:
        return f"{name}（QQ {int(uid)}）"
    return name


class GroupHistory:
    """每个群一份的短期聊天背景（重启即丢，属于"气氛记忆"）。"""

    def __init__(self, size: int = 25):
        self.size = max(0, size)
        self._data: dict[int, deque[ChatLine]] = defaultdict(
            lambda: deque(maxlen=self.size or 1)
        )

    def add(self, group_id: int, name: str, text: str, uid: int = 0) -> None:
        if self.size <= 0:
            return
        text = " ".join(str(text).split())
        if not text:
            return
        self._data[group_id].append(
            ChatLine(name=str(name), text=text[:300], ts=time.time(), uid=int(uid or 0))
        )

    def render(self, group_id: int, limit: int | None = None) -> str:
        lines = list(self._data.get(group_id, ()))
        if not lines:
            return ""
        if limit:
            lines = lines[-limit:]
        return "\n".join(
            f"{speaker_label(line.name, line.uid)}: {line.text}" for line in lines
        )

    def members(self, group_id: int, limit: int = 8) -> list[tuple[int, str]]:
        """本群最近说过话的人（uid, 最新昵称），给花名册注入用。"""
        latest: dict[int, str] = {}
        order: list[int] = []
        for line in reversed(self._data.get(group_id, ())):
            if not line.uid:
                continue
            if line.uid not in latest:
                latest[line.uid] = line.name
                order.append(line.uid)
            if len(order) >= limit:
                break
        return [(uid, latest[uid]) for uid in order]

    def clear(self, group_id: int) -> None:
        self._data.pop(group_id, None)

    def last_ts(self, group_id: int) -> float:
        """最近一条群消息的时间戳（没有则 0），主动发言用它判断群是否安静。"""
        lines = self._data.get(group_id)
        return lines[-1].ts if lines else 0.0


class MemoryStore:
    """SQLite 持久化：异步接口 + 单连接 + 锁（够一个 bot 用）。"""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    # -- 生命周期 ---------------------------------------------------------
    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await asyncio.to_thread(self._connect)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        self._migrate(conn)
        conn.commit()
        return conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """老库平滑升级：补 notes / user_notes 的 source 列，图片记忆补 expression。"""
        for table in ("notes", "user_notes"):
            columns = {
                str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
            }
            if "source" not in columns:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN source TEXT NOT NULL DEFAULT ''"
                )
        image_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(image_memory)")
        }
        if "expression" not in image_columns:
            conn.execute(
                "ALTER TABLE image_memory ADD COLUMN expression TEXT NOT NULL DEFAULT ''"
            )

    async def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            await asyncio.to_thread(conn.close)

    # -- 底层封装 ---------------------------------------------------------
    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        assert self._conn is not None
        with self._conn:
            self._conn.execute(sql, params)

    def _fetch(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        assert self._conn is not None
        return list(self._conn.execute(sql, params).fetchall())

    async def _write(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        async with self._lock:
            await asyncio.to_thread(self._execute, sql, params)

    async def _read(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> list[sqlite3.Row]:
        async with self._lock:
            return await asyncio.to_thread(self._fetch, sql, params)

    # -- 消息 -------------------------------------------------------------
    async def add_message(
        self, session_id: str, role: str, name: str, content: str
    ) -> None:
        await self._write(
            "INSERT INTO messages(session_id, role, name, content, ts) VALUES(?,?,?,?,?)",
            (session_id, role, name, content, time.time()),
        )

    async def recent_messages(
        self, session_id: str, limit: int
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        rows = await self._read(
            "SELECT role, name, content, ts FROM messages "
            "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        )
        return [dict(row) for row in reversed(rows)]

    async def messages_since(
        self, session_id: str, since_ts: float, limit: int = 200
    ) -> list[dict[str, Any]]:
        """取某个时间点之后的消息（活动消息 + 归档原文），按时间正序。"""
        rows = await self._read(
            "SELECT role, name, content, ts FROM ("
            "  SELECT role, name, content, ts FROM messages "
            "  WHERE session_id = ? AND ts >= ?"
            "  UNION ALL "
            "  SELECT role, name, content, ts FROM archive "
            "  WHERE session_id = ? AND ts >= ?"
            ") ORDER BY ts ASC LIMIT ?",
            (session_id, float(since_ts), session_id, float(since_ts), max(1, limit)),
        )
        return [dict(row) for row in rows]

    async def count_messages(self, session_id: str) -> int:
        rows = await self._read(
            "SELECT COUNT(*) AS n FROM messages WHERE session_id = ?", (session_id,)
        )
        return int(rows[0]["n"]) if rows else 0

    async def peek_oldest(
        self, session_id: str, keep: int
    ) -> list[dict[str, Any]]:
        """取出除最近 keep 条以外的最老消息（只读，不删除）。

        摘要失败时不能丢消息，所以先 peek、摘要成功后再 delete。
        """
        rows = await self._read(
            "SELECT id, role, name, content, ts FROM messages WHERE session_id = ? "
            "AND id NOT IN ("
            "  SELECT id FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?"
            ") ORDER BY id ASC",
            (session_id, session_id, max(0, keep)),
        )
        return [
            {
                "id": int(row["id"]),
                "role": row["role"],
                "name": row["name"],
                "content": row["content"],
                "ts": float(row["ts"]),
            }
            for row in rows
        ]

    async def delete_messages(self, ids: list[int]) -> None:
        """按 id 删除消息（摘要成功后的清理）。"""
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        await self._write(
            f"DELETE FROM messages WHERE id IN ({placeholders})", tuple(ids)
        )

    # -- 摘要 -------------------------------------------------------------
    async def get_summary(self, session_id: str) -> str:
        rows = await self._read(
            "SELECT content FROM summaries WHERE session_id = ?", (session_id,)
        )
        return str(rows[0]["content"]) if rows else ""

    async def set_summary(self, session_id: str, content: str) -> None:
        await self._write(
            "INSERT INTO summaries(session_id, content, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET content = excluded.content, "
            "updated_at = excluded.updated_at",
            (session_id, content, time.time()),
        )

    # -- 笔记（工具 + 记忆管家用） ----------------------------------------
    async def add_note(self, session_id: str, content: str, source: str = "") -> None:
        await self._write(
            "INSERT INTO notes(session_id, content, created_at, source) VALUES(?,?,?,?)",
            (session_id, content, time.time(), source[:20]),
        )

    async def recent_notes(self, session_id: str, limit: int = 10) -> list[str]:
        rows = await self._read(
            "SELECT content FROM notes WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        )
        return [str(row["content"]) for row in rows]

    # -- 图片记忆 ---------------------------------------------------------
    async def add_image_memory(
        self,
        session_id: str,
        user_id: int,
        name: str,
        description: str,
        media_key: str = "",
        expression: str = "",
    ) -> None:
        """记住一张看过/理解过的图；同一张图（media_key）只留最新描述和表达。"""
        text = " ".join(str(description).split())[:300]
        if len(text) < 4:
            return
        feeling = " ".join(str(expression).split())[:120]
        key = str(media_key or "").strip()[:200]
        if key:
            rows = await self._read(
                "SELECT id FROM image_memory WHERE session_id = ? AND media_key = ? "
                "ORDER BY id DESC LIMIT 1",
                (session_id, key),
            )
            if rows:
                await self._write(
                    "UPDATE image_memory SET description = ?, expression = ?, "
                    "name = ?, created_at = ? WHERE id = ?",
                    (text, feeling, str(name or "")[:40], time.time(), int(rows[0]["id"])),
                )
                return
        await self._write(
            "INSERT INTO image_memory(session_id, user_id, name, description, "
            "expression, media_key, created_at) VALUES(?,?,?,?,?,?,?)",
            (
                session_id,
                int(user_id or 0),
                str(name or "")[:40],
                text,
                feeling,
                key,
                time.time(),
            ),
        )
        # 每个会话最多保留 200 条图片记忆
        await self._write(
            "DELETE FROM image_memory WHERE session_id = ? AND id NOT IN ("
            "SELECT id FROM image_memory WHERE session_id = ? "
            "ORDER BY id DESC LIMIT 200)",
            (session_id, session_id),
        )

    async def recent_image_memories(
        self, session_id: str, limit: int = 6
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        rows = await self._read(
            "SELECT name, description, expression, created_at FROM image_memory "
            "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        )
        return [
            {
                "name": str(row["name"] or ""),
                "description": str(row["description"] or ""),
                "expression": str(row["expression"] or ""),
                "created_at": float(row["created_at"]),
            }
            for row in reversed(rows)
        ]

    async def update_latest_image_memory(
        self, session_id: str, extra: str
    ) -> bool:
        """群友解释了某张看不懂的图时，把说明补进最近一条图片记忆。"""
        text = " ".join(str(extra).split())[:120]
        if not text:
            return False
        rows = await self._read(
            "SELECT id, description FROM image_memory WHERE session_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
        if not rows:
            return False
        current = str(rows[0]["description"] or "")
        merged = (current + "；群友解释：" + text)[:300]
        await self._write(
            "UPDATE image_memory SET description = ?, created_at = ? WHERE id = ?",
            (merged, time.time(), int(rows[0]["id"])),
        )
        return True

    async def recent_note_entries(
        self, session_id: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """带时间戳的会话笔记（注入上下文时标上日期，减少"记不清什么时候"的胡编）。"""
        rows = await self._read(
            "SELECT content, created_at, source FROM notes "
            "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        )
        return [
            {
                "content": str(row["content"]),
                "created_at": float(row["created_at"]),
                "source": str(row["source"] or ""),
            }
            for row in rows
        ]

    async def add_user_note(
        self, user_id: int, content: str, source: str = ""
    ) -> None:
        """Phase 3.5：关于某个群友的长期事实（偏好、约定、称呼）。"""
        await self._write(
            "INSERT INTO user_notes(user_id, content, created_at, source) "
            "VALUES(?,?,?,?)",
            (user_id, content, time.time(), source[:20]),
        )

    async def recent_user_notes(self, user_id: int, limit: int = 5) -> list[str]:
        rows = await self._read(
            "SELECT content FROM user_notes WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        return [str(row["content"]) for row in rows]

    async def recent_user_note_entries(
        self, user_id: int, limit: int = 5
    ) -> list[dict[str, Any]]:
        rows = await self._read(
            "SELECT content, created_at FROM user_notes "
            "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        return [
            {"content": str(row["content"]), "created_at": float(row["created_at"])}
            for row in rows
        ]

    async def search_notes(
        self, session_id: str, query: str, limit: int = 5
    ) -> list[str]:
        """按关键词模糊检索本会话的笔记（工具 search_notes 用）。"""
        rows = await self._read(
            "SELECT content FROM notes WHERE session_id = ? AND content LIKE ? "
            "ORDER BY id DESC LIMIT ?",
            (session_id, f"%{query}%", limit),
        )
        if rows:
            return [str(row["content"]) for row in rows]
        # 没搜到关键词时退回最近的笔记，方便模型参考
        return await self.recent_notes(session_id, limit)

    async def stats_overview(self) -> dict[str, int]:
        """自检用：全库规模总览。"""
        rows = await self._read(
            "SELECT (SELECT COUNT(*) FROM messages) AS messages,"
            " (SELECT COUNT(*) FROM archive) AS archive,"
            " (SELECT COUNT(*) FROM notes) AS notes,"
            " (SELECT COUNT(*) FROM notes WHERE source = '画像') AS portraits,"
            " (SELECT COUNT(*) FROM todos WHERE status = 'open') AS todos,"
            " (SELECT COUNT(DISTINCT session_id) FROM messages) AS sessions"
        )
        if not rows:
            return {}
        return {key: int(value) for key, value in dict(rows[0]).items()}

    async def count_notes(self, session_id: str) -> int:
        rows = await self._read(
            "SELECT COUNT(*) AS n FROM notes WHERE session_id = ?", (session_id,)
        )
        return int(rows[0]["n"]) if rows else 0

    async def replace_notes(
        self, session_id: str, entries: list[tuple[str, str]]
    ) -> int:
        """用合并后的画像整体替换会话笔记（entries: (content, source)）。"""
        async with self._lock:
            await asyncio.to_thread(self._replace_notes_sync, session_id, entries)
        return len(entries)

    def _replace_notes_sync(
        self, session_id: str, entries: list[tuple[str, str]]
    ) -> None:
        assert self._conn is not None
        with self._conn:
            self._conn.execute("DELETE FROM notes WHERE session_id = ?", (session_id,))
            now = time.time()
            self._conn.executemany(
                "INSERT INTO notes(session_id, content, created_at, source) "
                "VALUES(?,?,?,?)",
                [
                    (session_id, str(content)[:300], now, str(source)[:20])
                    for content, source in entries
                ],
            )

    # -- 原文归档与找回（Context Steward） --------------------------------
    async def archive_messages(self, session_id: str, rows: list[dict[str, Any]]) -> int:
        """把摘要压缩掉的原文搬进归档，"压缩"不等于"遗忘"。"""
        if not rows:
            return 0
        payload = [
            (
                session_id,
                str(item.get("role") or "user"),
                item.get("name"),
                str(item.get("content") or ""),
                float(item.get("ts") or time.time()),
            )
            for item in rows
        ]
        async with self._lock:
            await asyncio.to_thread(self._archive_sync, payload)
        return len(payload)

    def _archive_sync(self, payload: list[tuple[Any, ...]]) -> None:
        assert self._conn is not None
        with self._conn:
            self._conn.executemany(
                "INSERT INTO archive(session_id, role, name, content, ts) "
                "VALUES(?,?,?,?,?)",
                payload,
            )

    async def archive_count(self, session_id: str) -> int:
        rows = await self._read(
            "SELECT COUNT(*) AS n FROM archive WHERE session_id = ?", (session_id,)
        )
        return int(rows[0]["n"]) if rows else 0

    async def recall(
        self, session_id: str, query: str, limit: int = 6
    ) -> list[dict[str, Any]]:
        """找回历史原话：同时搜「归档原文」和「当前活动消息」，按时间倒序。

        中文没有空格，整句话直接 LIKE 命中率低，所以先整句搜、再拆关键词搜。
        """
        limit = max(1, min(20, int(limit)))
        query = (query or "").strip()
        terms: list[str] = []
        if query:
            terms.append(query)
        terms.extend(
            part
            for part in re.split(r"[\s，,。？?！!、;；:：/\\]+", query)
            if len(part) >= 2 and part != query
        )
        terms = terms[:4] or []

        found: list[dict[str, Any]] = []
        for term in terms:
            found.extend(await self._search_table("archive", session_id, term, limit))
            found.extend(await self._search_table("messages", session_id, term, limit))

        seen: set[tuple[str, int]] = set()
        unique: list[dict[str, Any]] = []
        for item in sorted(found, key=lambda item: item["ts"], reverse=True):
            key = (str(item["content"])[:100], int(item["ts"]))
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
            if len(unique) >= limit:
                break
        return unique

    async def _search_table(
        self, table: str, session_id: str, term: str, limit: int
    ) -> list[dict[str, Any]]:
        if table not in {"archive", "messages"}:
            return []
        rows = await self._read(
            f"SELECT role, name, content, ts FROM {table} "
            "WHERE session_id = ? AND content LIKE ? ORDER BY ts DESC LIMIT ?",
            (session_id, f"%{term}%", limit),
        )
        return [
            {
                "role": str(row["role"]),
                "name": str(row["name"] or ""),
                "content": str(row["content"]),
                "ts": float(row["ts"]),
                "source": table,
            }
            for row in rows
        ]

    # -- 待办 / 承诺（任务闭环） ------------------------------------------
    async def add_todo(self, session_id: str, content: str) -> int:
        await self._write(
            "INSERT INTO todos(session_id, content, status, created_at) "
            "VALUES(?,?,'open',?)",
            (session_id, content, time.time()),
        )
        rows = await self._read(
            "SELECT id FROM todos WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
        return int(rows[0]["id"]) if rows else 0

    async def list_todos(
        self, session_id: str, limit: int = 10, status: str = "open"
    ) -> list[dict[str, Any]]:
        rows = await self._read(
            "SELECT id, content, status, created_at, done_at FROM todos "
            "WHERE session_id = ? AND status = ? ORDER BY id ASC LIMIT ?",
            (session_id, status, max(1, limit)),
        )
        return [
            {
                "id": int(row["id"]),
                "content": str(row["content"]),
                "status": str(row["status"]),
                "created_at": float(row["created_at"]),
                "done_at": row["done_at"],
            }
            for row in rows
        ]

    async def finish_todo(self, session_id: str, ident: str) -> dict[str, Any] | None:
        """把待办标记完成：ident 可以是 #3 / 3，也可以是内容关键词。"""
        raw = (ident or "").strip().lstrip("#")
        target: dict[str, Any] | None = None
        if raw.isdigit():
            rows = await self._read(
                "SELECT id, content, created_at FROM todos "
                "WHERE session_id = ? AND id = ? AND status = 'open'",
                (session_id, int(raw)),
            )
            if rows:
                target = {
                    "id": int(rows[0]["id"]),
                    "content": str(rows[0]["content"]),
                }
        if target is None and raw:
            rows = await self._read(
                "SELECT id, content FROM todos "
                "WHERE session_id = ? AND status = 'open' AND content LIKE ? "
                "ORDER BY id DESC LIMIT 1",
                (session_id, f"%{raw}%"),
            )
            if rows:
                target = {
                    "id": int(rows[0]["id"]),
                    "content": str(rows[0]["content"]),
                }
        if target is None:
            return None
        await self._write(
            "UPDATE todos SET status = 'done', done_at = ? WHERE id = ?",
            (time.time(), target["id"]),
        )
        return target

    async def list_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        """Phase 4.1：给 WebUI 用的会话列表（按最后活跃时间倒序）。"""
        rows = await self._read(
            "SELECT session_id, COUNT(*) AS count, MAX(ts) AS last_ts "
            "FROM messages GROUP BY session_id ORDER BY last_ts DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]

    async def daily_message_counts(
        self, days: int = 7, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        """最近 N 天每天的消息条数（活动消息 + 归档），没有的天补 0。"""
        span = max(1, min(30, int(days)))
        since = time.time() - span * 86400
        if session_id:
            rows = await self._read(
                "SELECT date(ts, 'unixepoch', 'localtime') AS day, COUNT(*) AS n "
                "FROM (SELECT ts FROM messages WHERE session_id = ? UNION ALL "
                "SELECT ts FROM archive WHERE session_id = ?) "
                "WHERE ts >= ? GROUP BY day ORDER BY day",
                (session_id, session_id, since),
            )
        else:
            rows = await self._read(
                "SELECT date(ts, 'unixepoch', 'localtime') AS day, COUNT(*) AS n "
                "FROM (SELECT ts FROM messages UNION ALL SELECT ts FROM archive) "
                "WHERE ts >= ? GROUP BY day ORDER BY day",
                (since,),
            )
        counts = {str(row["day"]): int(row["n"]) for row in rows}
        result: list[dict[str, Any]] = []
        now = time.time()
        for offset in range(span - 1, -1, -1):
            day = time.strftime("%Y-%m-%d", time.localtime(now - offset * 86400))
            result.append({"day": day, "count": counts.get(day, 0)})
        return result

    async def recent_texts(self, since_ts: float, limit: int = 2000) -> list[str]:
        """近段时间群友说过的话（词云统计用，只要 user 消息）。"""
        rows = await self._read(
            "SELECT content FROM ("
            "  SELECT content, ts FROM messages WHERE role = 'user' AND ts >= ?"
            "  UNION ALL "
            "  SELECT content, ts FROM archive WHERE role = 'user' AND ts >= ?"
            ") ORDER BY ts ASC LIMIT ?",
            (float(since_ts), float(since_ts), max(1, limit)),
        )
        return [str(row["content"]) for row in rows]

    async def count_pending_reminders(self) -> int:
        rows = await self._read("SELECT COUNT(*) AS n FROM reminders WHERE done = 0")
        return int(rows[0]["n"]) if rows else 0

    # -- 主动关心 ---------------------------------------------------------
    async def add_care_event(
        self,
        session_id: str,
        user_id: int,
        name: str,
        kind: str,
        detail: str,
        due_at: float,
    ) -> int:
        await self._write(
            "INSERT INTO care_events(session_id, user_id, name, kind, detail, "
            "created_at, due_at) VALUES(?,?,?,?,?,?,?)",
            (
                session_id,
                int(user_id or 0),
                str(name)[:40],
                str(kind)[:20],
                str(detail)[:120],
                time.time(),
                float(due_at),
            ),
        )
        rows = await self._read("SELECT id FROM care_events ORDER BY id DESC LIMIT 1")
        return int(rows[0]["id"]) if rows else 0

    async def recent_care_count(
        self, session_id: str, user_id: int, since: float
    ) -> int:
        rows = await self._read(
            "SELECT COUNT(*) AS n FROM care_events "
            "WHERE session_id = ? AND user_id = ? AND created_at >= ?",
            (session_id, int(user_id), float(since)),
        )
        return int(rows[0]["n"]) if rows else 0

    async def due_care_events(
        self, now: float | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        current = time.time() if now is None else now
        rows = await self._read(
            "SELECT id, session_id, user_id, name, kind, detail, due_at "
            "FROM care_events WHERE sent = 0 AND due_at <= ? "
            "ORDER BY due_at ASC LIMIT ?",
            (float(current), max(1, limit)),
        )
        return [dict(row) for row in rows]

    async def mark_care_sent(self, care_id: int) -> None:
        await self._write(
            "UPDATE care_events SET sent = 1, sent_at = ? WHERE id = ?",
            (time.time(), int(care_id)),
        )

    async def care_sent_since(self, session_id: str, since: float) -> int:
        rows = await self._read(
            "SELECT COUNT(*) AS n FROM care_events "
            "WHERE session_id = ? AND sent = 1 AND sent_at >= ?",
            (session_id, float(since)),
        )
        return int(rows[0]["n"]) if rows else 0

    async def care_sent_to_user_since(
        self, session_id: str, user_id: int, since: float
    ) -> int:
        rows = await self._read(
            "SELECT COUNT(*) AS n FROM care_events "
            "WHERE session_id = ? AND user_id = ? AND sent = 1 AND sent_at >= ?",
            (session_id, int(user_id), float(since)),
        )
        return int(rows[0]["n"]) if rows else 0

    async def count_care_pending(self) -> int:
        rows = await self._read("SELECT COUNT(*) AS n FROM care_events WHERE sent = 0")
        return int(rows[0]["n"]) if rows else 0

    async def recent_care_events(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = await self._read(
            "SELECT id, session_id, user_id, name, kind, detail, due_at, sent, "
            "created_at FROM care_events ORDER BY id DESC LIMIT ?",
            (max(1, limit),),
        )
        return [dict(row) for row in rows]

    # -- 亲密度 -----------------------------------------------------------
    async def bump_affinity(
        self, user_id: int, group_id: int, delta: int = 1
    ) -> int:
        await self._write(
            "INSERT INTO affinity(user_id, group_id, score, updated_at) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(user_id, group_id) DO UPDATE SET "
            "score = score + excluded.score, updated_at = excluded.updated_at",
            (int(user_id), int(group_id), int(delta), time.time()),
        )
        return await self.get_affinity(user_id, group_id)

    async def get_affinity(self, user_id: int, group_id: int) -> int:
        rows = await self._read(
            "SELECT score FROM affinity WHERE user_id = ? AND group_id = ?",
            (int(user_id), int(group_id)),
        )
        return int(rows[0]["score"]) if rows else 0

    async def top_affinity(self, group_id: int, limit: int = 5) -> list[dict[str, Any]]:
        rows = await self._read(
            "SELECT user_id, score FROM affinity WHERE group_id = ? "
            "ORDER BY score DESC LIMIT ?",
            (int(group_id), max(1, limit)),
        )
        return [
            {"user_id": int(row["user_id"]), "score": int(row["score"])}
            for row in rows
        ]

    # -- 定时提醒 ---------------------------------------------------------
    async def add_reminder(
        self, session_id: str, user_id: int, content: str, due_at: float
    ) -> int:
        await self._write(
            "INSERT INTO reminders(session_id, user_id, content, due_at, created_at) "
            "VALUES(?,?,?,?,?)",
            (session_id, int(user_id or 0), str(content)[:300], float(due_at), time.time()),
        )
        rows = await self._read("SELECT id FROM reminders ORDER BY id DESC LIMIT 1")
        return int(rows[0]["id"]) if rows else 0

    async def due_reminders(
        self, now: float | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        current = time.time() if now is None else now
        rows = await self._read(
            "SELECT id, session_id, user_id, content, due_at FROM reminders "
            "WHERE done = 0 AND due_at <= ? ORDER BY due_at ASC LIMIT ?",
            (float(current), max(1, limit)),
        )
        return [dict(row) for row in rows]

    async def mark_reminder_done(self, reminder_id: int) -> None:
        await self._write(
            "UPDATE reminders SET done = 1 WHERE id = ?", (int(reminder_id),)
        )

    async def all_pending_reminders(self, limit: int = 50) -> list[dict[str, Any]]:
        """所有会话未完成的提醒（WebUI 用）。"""
        rows = await self._read(
            "SELECT id, session_id, user_id, content, due_at FROM reminders "
            "WHERE done = 0 ORDER BY due_at ASC LIMIT ?",
            (max(1, limit),),
        )
        return [dict(row) for row in rows]

    async def cancel_reminder(self, reminder_id: int) -> bool:
        """管理员取消任意提醒（WebUI 用）；成功返回 True。"""
        rows = await self._read(
            "SELECT id FROM reminders WHERE id = ? AND done = 0",
            (int(reminder_id),),
        )
        if not rows:
            return False
        await self._write("DELETE FROM reminders WHERE id = ?", (int(reminder_id),))
        return True

    async def remove_reminder(self, reminder_id: int, session_id: str) -> bool:
        """取消某条未完成的提醒（只能取消本会话的）；成功返回 True。"""
        rows = await self._read(
            "SELECT id FROM reminders WHERE id = ? AND session_id = ? AND done = 0",
            (int(reminder_id), session_id),
        )
        if not rows:
            return False
        await self._write(
            "DELETE FROM reminders WHERE id = ? AND session_id = ? AND done = 0",
            (int(reminder_id), session_id),
        )
        return True

    async def list_reminders(
        self, session_id: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        rows = await self._read(
            "SELECT id, content, due_at FROM reminders "
            "WHERE session_id = ? AND done = 0 ORDER BY due_at ASC LIMIT ?",
            (session_id, max(1, limit)),
        )
        return [
            {
                "id": int(row["id"]),
                "content": str(row["content"]),
                "due_at": float(row["due_at"]),
            }
            for row in rows
        ]

    # -- 数据清理 ---------------------------------------------------------
    async def _prune_table(
        self,
        table: str,
        column: str,
        cutoff: float,
        extra_sql: str = "",
        extra_params: tuple[Any, ...] = (),
    ) -> int:
        where = f"WHERE {column} < ? {extra_sql}"
        params = (cutoff,) + extra_params
        rows = await self._read(f"SELECT COUNT(*) AS n FROM {table} {where}", params)
        count = int(rows[0]["n"]) if rows else 0
        if count > 0:
            await self._write(f"DELETE FROM {table} {where}", params)
        return count

    async def prune_archive(self, keep_days: float = 30.0) -> int:
        cutoff = time.time() - max(0.0, float(keep_days)) * 86400
        return await self._prune_table("archive", "ts", cutoff)

    async def prune_image_memory(self, keep_days: float = 30.0) -> int:
        cutoff = time.time() - max(0.0, float(keep_days)) * 86400
        return await self._prune_table("image_memory", "created_at", cutoff)

    async def prune_reminders(self, keep_days: float = 7.0) -> int:
        cutoff = time.time() - max(0.0, float(keep_days)) * 86400
        return await self._prune_table(
            "reminders", "created_at", cutoff, "AND done = 1"
        )

    async def prune_care_events(self, keep_days: float = 30.0) -> int:
        cutoff = time.time() - max(0.0, float(keep_days)) * 86400
        return await self._prune_table(
            "care_events", "created_at", cutoff, "AND sent = 1"
        )

    # -- 维护 -------------------------------------------------------------
    async def reset_session(self, session_id: str) -> None:
        await self._write("DELETE FROM messages WHERE session_id = ?", (session_id,))
        await self._write("DELETE FROM summaries WHERE session_id = ?", (session_id,))
        await self._write("DELETE FROM notes WHERE session_id = ?", (session_id,))
        await self._write("DELETE FROM archive WHERE session_id = ?", (session_id,))
        await self._write("DELETE FROM todos WHERE session_id = ?", (session_id,))

    async def purge_older_than(self, hours: float) -> int:
        """清理很久没动过的会话，防止数据库无限增长。返回删除条数。"""
        cutoff = time.time() - hours * 3600
        rows = await self._read(
            "SELECT COUNT(*) AS n FROM messages WHERE ts < ?", (cutoff,)
        )
        count = int(rows[0]["n"]) if rows else 0
        if count:
            await self._write("DELETE FROM messages WHERE ts < ?", (cutoff,))
        return count

    async def prune_archive(self, days: float) -> int:
        """归档只保留最近 N 天（0 = 不过期），避免长期运行把库撑大。"""
        if days <= 0:
            return 0
        rows = await self._read(
            "SELECT COUNT(*) AS n FROM archive WHERE ts < ?",
            (time.time() - days * 86400,),
        )
        count = int(rows[0]["n"]) if rows else 0
        if count:
            await self._write(
                "DELETE FROM archive WHERE ts < ?", (time.time() - days * 86400,)
            )
        return count
