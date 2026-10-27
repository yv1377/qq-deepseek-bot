"""Phase 4.3：用量与耗时统计。

- 每次模型调用记录 token 与耗时（由 llm.py 回调写入）；
- 按天聚合，供 `/status` 和 WebUI 展示；
- 独立 SQLite 文件（data/metrics.db），失败不影响聊天主流程。

写入是同步的（单行 INSERT，量很小）；调用方都在事件循环线程里，够用。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                REAL    NOT NULL,
    kind              TEXT    NOT NULL,
    model             TEXT    NOT NULL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    duration_ms       REAL    NOT NULL DEFAULT 0,
    ok                INTEGER NOT NULL DEFAULT 1,
    cache_hit_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_miss_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(ts);
"""


class MetricsStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    # -- 生命周期 ---------------------------------------------------------
    def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        # 老库补列（缓存命中率）
        columns = {row[1] for row in conn.execute("PRAGMA table_info(llm_usage)")}
        if "cache_hit_tokens" not in columns:
            conn.execute(
                "ALTER TABLE llm_usage ADD COLUMN cache_hit_tokens INTEGER NOT NULL DEFAULT 0"
            )
        if "cache_miss_tokens" not in columns:
            conn.execute(
                "ALTER TABLE llm_usage ADD COLUMN cache_miss_tokens INTEGER NOT NULL DEFAULT 0"
            )
        conn.commit()
        self._conn = conn

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()

    def _require(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("MetricsStore 尚未 init")
        return self._conn

    # -- 写入 -------------------------------------------------------------
    def record(
        self,
        *,
        kind: str,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        cache_hit_tokens: int = 0,
        cache_miss_tokens: int = 0,
        duration_ms: float = 0.0,
        ok: bool = True,
        ts: float | None = None,
    ) -> None:
        """记录一次模型调用；ts 可注入，便于测试/补录。"""
        prompt_tokens = max(0, int(prompt_tokens))
        completion_tokens = max(0, int(completion_tokens))
        total = max(0, int(total_tokens)) or (prompt_tokens + completion_tokens)
        stamp = float(ts) if ts is not None else datetime.now().timestamp()
        conn = self._require()
        with conn:
            conn.execute(
                "INSERT INTO llm_usage(ts, kind, model, prompt_tokens, "
                "completion_tokens, total_tokens, duration_ms, ok, "
                "cache_hit_tokens, cache_miss_tokens) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    stamp,
                    str(kind),
                    str(model),
                    prompt_tokens,
                    completion_tokens,
                    total,
                    float(max(0.0, duration_ms)),
                    1 if ok else 0,
                    max(0, int(cache_hit_tokens)),
                    max(0, int(cache_miss_tokens)),
                ),
            )

    # -- 查询 -------------------------------------------------------------
    @staticmethod
    def _day_start(offset_days: int = 0) -> float:
        day = datetime.now() - timedelta(days=offset_days)
        return day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def today_summary(self) -> dict[str, Any]:
        conn = self._require()
        start = self._day_start(0)
        row = conn.execute(
            "SELECT COUNT(*) AS calls, "
            "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
            "COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
            "COALESCE(SUM(total_tokens), 0) AS total_tokens, "
            "COALESCE(AVG(duration_ms), 0) AS avg_duration_ms, "
            "COALESCE(SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END), 0) AS errors, "
            "COALESCE(SUM(cache_hit_tokens), 0) AS cache_hit_tokens, "
            "COALESCE(SUM(cache_miss_tokens), 0) AS cache_miss_tokens "
            "FROM llm_usage WHERE ts >= ?",
            (start,),
        ).fetchone()
        cache_hit = int(row[6] or 0)
        cache_miss = int(row[7] or 0)
        cache_total = cache_hit + cache_miss
        return {
            "date": datetime.fromtimestamp(start).strftime("%Y-%m-%d"),
            "calls": int(row[0] or 0),
            "prompt_tokens": int(row[1] or 0),
            "completion_tokens": int(row[2] or 0),
            "total_tokens": int(row[3] or 0),
            "avg_duration_ms": round(float(row[4] or 0), 1),
            "errors": int(row[5] or 0),
            "cache_hit_tokens": cache_hit,
            "cache_miss_tokens": cache_miss,
            "cache_hit_rate": round(cache_hit / cache_total, 3) if cache_total else 0.0,
        }

    def daily(self, days: int = 7) -> list[dict[str, Any]]:
        """最近 N 天（含今天）的按天聚合，缺失的日期补 0。"""
        days = max(1, min(90, int(days)))
        conn = self._require()
        start = self._day_start(days - 1)
        rows = conn.execute(
            "SELECT ts, prompt_tokens, completion_tokens, total_tokens, "
            "duration_ms, ok, cache_hit_tokens, cache_miss_tokens "
            "FROM llm_usage WHERE ts >= ? ORDER BY ts ASC",
            (start,),
        ).fetchall()

        buckets: dict[str, dict[str, Any]] = {}
        for offset in range(days - 1, -1, -1):
            key = datetime.fromtimestamp(self._day_start(offset)).strftime("%Y-%m-%d")
            buckets[key] = {
                "date": key,
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "duration_ms": 0.0,
                "errors": 0,
                "cache_hit_tokens": 0,
                "cache_miss_tokens": 0,
            }
        for ts, prompt, completion, total, duration, ok, cache_hit, cache_miss in rows:
            key = datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
            bucket = buckets.get(key)
            if bucket is None:
                continue
            bucket["calls"] += 1
            bucket["prompt_tokens"] += int(prompt or 0)
            bucket["completion_tokens"] += int(completion or 0)
            bucket["total_tokens"] += int(total or 0)
            bucket["duration_ms"] += float(duration or 0)
            if not ok:
                bucket["errors"] += 1
            bucket["cache_hit_tokens"] += int(cache_hit or 0)
            bucket["cache_miss_tokens"] += int(cache_miss or 0)
        result = []
        for bucket in buckets.values():
            calls = bucket["calls"]
            bucket["avg_duration_ms"] = (
                round(bucket["duration_ms"] / calls, 1) if calls else 0.0
            )
            bucket.pop("duration_ms")
            cache_total = bucket["cache_hit_tokens"] + bucket["cache_miss_tokens"]
            bucket["cache_hit_rate"] = (
                round(bucket["cache_hit_tokens"] / cache_total, 3)
                if cache_total
                else 0.0
            )
            result.append(bucket)
        return result

    # -- 维护 -------------------------------------------------------------
    def prune(self, keep_days: int = 30) -> int:
        conn = self._require()
        cutoff = datetime.now().timestamp() - max(1, int(keep_days)) * 86400
        with conn:
            cursor = conn.execute("DELETE FROM llm_usage WHERE ts < ?", (cutoff,))
        return int(cursor.rowcount or 0)
