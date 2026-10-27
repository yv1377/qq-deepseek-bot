"""每个群的每日花费预算。

DeepSeek 按 token 计费，这里把每次调用的 usage 折算成人民币，按「群号 + 本地日期」
累加，超过 `chat.budget.daily_cny_per_group` 后当天不再调用模型。

价格按 DeepSeek 官方 deepseek-flash 价目（美元/1M token）换算，峰值时段翻倍：
https://api-docs.deepseek.com/quick_start/pricing
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT, BudgetConfig

log = logging.getLogger("budget")

SCHEMA = """
CREATE TABLE IF NOT EXISTS group_spend (
    day        TEXT    NOT NULL,
    group_id   INTEGER NOT NULL,
    cny        REAL    NOT NULL DEFAULT 0,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    calls      INTEGER NOT NULL DEFAULT 0,
    updated_at REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (day, group_id)
);
"""


def is_peak(now: datetime | None = None) -> bool:
    """DeepSeek 高峰时段：UTC 周一~周五 01:00-04:00 与 06:00-10:00。"""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    utc = current.astimezone(timezone.utc)
    if utc.weekday() >= 5:
        return False
    return 1 <= utc.hour < 4 or 6 <= utc.hour < 10


def estimate_cny(
    usage: dict[str, Any],
    cfg: BudgetConfig,
    now: datetime | None = None,
) -> float:
    """按 usage 里的 token 数估算一次调用的花费（人民币）。"""
    if not usage:
        return 0.0
    prompt = max(0, int(usage.get("prompt_tokens") or 0))
    completion = max(0, int(usage.get("completion_tokens") or 0))
    details = usage.get("prompt_tokens_details")
    if not isinstance(details, dict):
        details = {}
    cached = usage.get("prompt_cache_hit_tokens")
    if cached is None:
        cached = details.get("cached_tokens")
    cached = min(prompt, max(0, int(cached or 0)))
    miss = max(0, prompt - cached)
    multiplier = cfg.peak_multiplier if is_peak(now) else 1.0
    cost = (
        miss * cfg.input_cny_per_million
        + cached * cfg.cached_input_cny_per_million
        + completion * cfg.output_cny_per_million
    ) / 1_000_000
    return cost * multiplier


class BudgetStore:
    """按群/天记录花费的小型 SQLite；读写都在事件循环线程里，量很小。"""

    def __init__(self, cfg: BudgetConfig):
        self.cfg = cfg
        self.path = Path(cfg.db_path)
        if not self.path.is_absolute():
            self.path = PROJECT_ROOT / self.path
        self._conn: sqlite3.Connection | None = None
        self._day = self._today()
        self._cache: dict[int, float] = {}
        self._init()

    def _today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.commit()
        self._conn = conn
        self._load_today()

    def _load_today(self) -> None:
        self._cache = {}
        assert self._conn is not None
        for row in self._conn.execute(
            "SELECT group_id, cny FROM group_spend WHERE day = ?", (self._day,)
        ):
            self._cache[int(row[0])] = float(row[1])

    def _roll_day(self) -> None:
        today = self._today()
        if today != self._day:
            self._day = today
            self._load_today()

    def today_cny(self, group_id: int) -> float:
        self._roll_day()
        return self._cache.get(int(group_id), 0.0)

    def limit_for(self, group_id: int) -> float:
        """这个群今天的花费上限：支持按群单独配置。"""
        return float(
            self.cfg.group_limits.get(
                int(group_id), self.cfg.daily_cny_per_group
            )
        )

    def remaining(self, group_id: int) -> float:
        return max(0.0, self.limit_for(group_id) - self.today_cny(group_id))

    def spent_groups(self) -> dict[int, float]:
        """今天所有有花费的群 -> 金额。"""
        self._roll_day()
        return dict(self._cache)

    def reset_today(self, group_id: int) -> float:
        """把某个群今天的额度清零（只清这个群，其他群不动），返回清零前的花费。"""
        self._roll_day()
        gid = int(group_id)
        before = self._cache.get(gid, 0.0)
        try:
            assert self._conn is not None
            with self._conn:
                self._conn.execute(
                    "DELETE FROM group_spend WHERE day = ? AND group_id = ?",
                    (self._day, gid),
                )
        except Exception:
            log.exception("重置群预算失败：%s", gid)
            return before
        self._cache.pop(gid, None)
        log.info("已重置群 %s 今日额度（原花费 %.4f 元）", gid, before)
        return before

    def reset_all_today(self) -> dict[int, float]:
        """把今天所有群的额度清零（历史日期不动），返回清零前的花费。"""
        self._roll_day()
        before = dict(self._cache)
        if not before:
            return {}
        try:
            assert self._conn is not None
            with self._conn:
                self._conn.execute(
                    "DELETE FROM group_spend WHERE day = ?", (self._day,)
                )
        except Exception:
            log.exception("重置全部群预算失败")
            return before
        self._cache.clear()
        log.info(
            "已重置全部群今日额度（%s 个群，共 %.4f 元）",
            len(before),
            sum(before.values()),
        )
        return before

    def add_usage(self, group_id: int, usage: dict[str, Any]) -> float:
        """记录一次调用，返回本次估算花费；失败不影响聊天。"""
        cost = estimate_cny(usage, self.cfg)
        if cost <= 0:
            return 0.0
        self._roll_day()
        gid = int(group_id)
        prompt = max(0, int(usage.get("prompt_tokens") or 0))
        completion = max(0, int(usage.get("completion_tokens") or 0))
        try:
            assert self._conn is not None
            with self._conn:
                self._conn.execute(
                    "INSERT INTO group_spend(day, group_id, cny, prompt_tokens, "
                    "completion_tokens, calls, updated_at) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(day, group_id) DO UPDATE SET "
                    "cny = cny + excluded.cny, "
                    "prompt_tokens = prompt_tokens + excluded.prompt_tokens, "
                    "completion_tokens = completion_tokens + excluded.completion_tokens, "
                    "calls = calls + 1, updated_at = excluded.updated_at",
                    (
                        self._day,
                        gid,
                        cost,
                        prompt,
                        completion,
                        1,
                        datetime.now().timestamp(),
                    ),
                )
            self._cache[gid] = self._cache.get(gid, 0.0) + cost
        except sqlite3.Error:
            log.exception("记录群预算失败：群 %s", gid)
        return cost

    def group_summary(self, group_id: int) -> dict[str, Any]:
        self._roll_day()
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT cny, prompt_tokens, completion_tokens, calls FROM group_spend "
            "WHERE day = ? AND group_id = ?",
            (self._day, int(group_id)),
        ).fetchone()
        if row is None:
            return {"day": self._day, "group_id": int(group_id), "cny": 0.0,
                    "limit_cny": self.limit_for(group_id),
                    "prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
        return {
            "day": self._day,
            "group_id": int(group_id),
            "cny": float(row[0]),
            "limit_cny": self.limit_for(group_id),
            "prompt_tokens": int(row[1]),
            "completion_tokens": int(row[2]),
            "calls": int(row[3]),
        }

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
