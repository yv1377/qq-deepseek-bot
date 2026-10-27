"""周报：每周固定一天，把这一周的群里发生的事整理成一张周报图。

- `WEEKLY_SYSTEM`：生成「本周看点」的提示词；
- `WeeklyReportScheduler`：每天检查一次，到指定星期几+时间后给目标群各发一次。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

log = logging.getLogger("weekly")

WEEKLY_SYSTEM = (
    "你是群聊机器人的「周报编辑」。根据这一周的群聊记录，写 2~3 条本周看点。\n"
    "要求：每条一行、以「- 」开头、不超过 45 字；口语化，像群友在回顾这一周；\n"
    "只写记录里真实出现的事（话题、趣事、约好的事、谁在忙什么），不编造、不评价；\n"
    "没有值得写的内容就只输出 [NONE]。不要标题、编号或解释。"
)

WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


class WeeklyReportScheduler:
    """每天检查一次；只在配置的星期几、到点后发，每周每群一次。"""

    def __init__(self, service, cfg):
        self.service = service
        self.cfg = cfg
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._last_day: dict[int, str] = {}

    def start(self) -> None:
        if self._task is not None:
            return
        if not self.cfg.enabled:
            log.info("周报：关闭")
            return
        self._task = asyncio.create_task(self.run())
        log.info(
            "周报调度器已启动（每周%s %02d:%02d）",
            WEEKDAY_NAMES[self.cfg.weekday % 7],
            self.cfg.hour,
            self.cfg.minute,
        )

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
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=600)
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                return
            try:
                await self.tick_once()
            except Exception:
                log.exception("周报检查出错")

    async def tick_once(self, now: float | None = None) -> list[int]:
        """到点后给每个群发一次；返回本次成功发送的群号（便于测试）。"""
        if not self.cfg.enabled:
            return []
        current = time.time() if now is None else now
        moment = datetime.fromtimestamp(current)
        if moment.weekday() != self.cfg.weekday % 7:
            return []
        if (moment.hour, moment.minute) < (self.cfg.hour, self.cfg.minute):
            return []
        day_key = moment.strftime("%Y-%m-%d")
        targets: list[int] = list(self.cfg.groups)
        if not targets:
            targets = await self.service.active_group_sessions(now=current)
        sent: list[int] = []
        for group_id in targets:
            if self._last_day.get(group_id) == day_key:
                continue
            try:
                ok = await self.service.send_weekly_report(group_id, now=current)
            except Exception:
                log.exception("周报发送失败：%s", group_id)
                continue
            if not ok:
                continue
            self._last_day[group_id] = day_key
            sent.append(group_id)
        return sent
