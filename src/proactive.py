"""Phase 3.4：主动能力（低频、可控地自己说一句）。

默认关闭。设计上刻意保守：
- 只对 `chat.proactive.groups` 里显式列出的群生效；
- 静默时段（默认 0–7 点）不发言；
- 同群有最小间隔 + 每日上限；
- 模型可以回 [PASS] 放弃发言（群里没什么可说的就不硬说）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Protocol

from config import ProactiveConfig, ProactiveGroupConfig

log = logging.getLogger("proactive")


class ProactiveSpeaker(Protocol):
    async def speak_proactively(self, group_id: int) -> bool: ...


class ProactiveScheduler:
    def __init__(self, cfg: ProactiveConfig, speaker: ProactiveSpeaker):
        self.cfg = cfg
        self.speaker = speaker
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._last: dict[int, float] = {}
        self._count_day: dict[int, str] = {}
        self._count: dict[int, int] = {}

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled and bool(self.cfg.groups)

    def start(self) -> None:
        # 即使当前关闭也启动调度器：WebUI 可以在运行时打开，不需要重启
        if not self.cfg.groups:
            log.info("主动发言未配置群，跳过调度器")
            return
        if not self.cfg.enabled:
            log.info("主动发言当前关闭，调度器已就绪（WebUI 可开启）")
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def run(self) -> None:
        # 启动后先等一个完整间隔，避免刚上线就冒泡
        now = time.time()
        for group_id in self.cfg.groups:
            self._last.setdefault(group_id, now)
        log.info(
            "主动发言调度器已启动：%s 个群（当前%s），检查间隔 %.0f 秒",
            len(self.cfg.groups),
            "开启" if self.cfg.enabled else "关闭",
            self.cfg.check_interval,
        )
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.cfg.check_interval
                )
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                return
            try:
                await self.tick_once()
            except Exception:
                log.exception("主动发言检查出错")

    async def tick_once(self, now: float | None = None) -> list[int]:
        """检查一轮并触发；返回本次成功发言的群号（便于测试）。"""
        if not self.cfg.enabled:
            return []
        current = time.time() if now is None else now
        hour = datetime.fromtimestamp(current).hour
        spoken: list[int] = []
        for group_id, group_cfg in self.cfg.groups.items():
            if self._in_quiet(group_cfg, hour):
                continue
            if current - self._last.get(group_id, -1e18) < (
                max(0.0, group_cfg.interval_minutes) * 60
            ):
                continue
            if self._daily_count(group_id, current) >= max(0, group_cfg.max_per_day):
                continue
            try:
                sent = await self.speaker.speak_proactively(group_id)
            except Exception:
                log.exception("主动发言失败：%s", group_id)
                continue
            if not sent:
                continue
            self._last[group_id] = current
            self._bump(group_id, current)
            spoken.append(group_id)
        return spoken

    @staticmethod
    def _in_quiet(cfg: ProactiveGroupConfig, hour: int) -> bool:
        start = cfg.quiet_start % 24
        end = cfg.quiet_end % 24
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    @staticmethod
    def _day_key(ts: float) -> str:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")

    def _daily_count(self, group_id: int, ts: float) -> int:
        if self._count_day.get(group_id) != self._day_key(ts):
            return 0
        return self._count.get(group_id, 0)

    def _bump(self, group_id: int, ts: float) -> None:
        key = self._day_key(ts)
        if self._count_day.get(group_id) != key:
            self._count_day[group_id] = key
            self._count[group_id] = 0
        self._count[group_id] = self._count.get(group_id, 0) + 1
