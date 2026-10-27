"""每日群报：每天固定时间，把群里今天发生的事整理成 2-3 条简报。

- `REPORT_SYSTEM`：生成提示词（只根据真实记录，不编造）；
- `DailyReportScheduler`：每分钟检查一次，到点后给当天活跃的群各发一条（每天一次）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

log = logging.getLogger("report")

REPORT_SYSTEM = (
    "你是群聊机器人的「群报编辑」。根据今天的群聊记录，写 2~3 条今日群报。\n"
    "要求：每条一行、以「- 」开头、不超过 45 字；口语化，像群友在总结今天；\n"
    "只写记录里真实出现的事（话题、趣事、约好的事、谁在忙什么），不编造、不评价；\n"
    "没有值得写的内容就只输出 [NONE]。不要标题、编号或解释。"
)


class DailyReportScheduler:
    """每分钟检查一次；到点后把当天活跃的群报出去。"""

    def __init__(self, service, cfg):
        self.service = service
        self.cfg = cfg
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._last_day: dict[int, str] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    def start(self) -> None:
        if self._task is not None:
            return
        if not self.cfg.enabled:
            log.info("每日群报：关闭")
            return
        self._task = asyncio.create_task(self.run())
        log.info(
            "每日群报调度器已启动（每天 %02d:%02d，%s）",
            self.cfg.hour,
            self.cfg.minute,
            "指定群" if self.cfg.groups else "当天活跃群",
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
                await asyncio.wait_for(self._stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                return
            try:
                await self.tick_once()
            except Exception:
                log.exception("每日群报检查出错")

    async def tick_once(self, now: float | None = None) -> list[int]:
        """到点后给每个群发一次；返回本次成功发送的群号（便于测试）。"""
        if not self.cfg.enabled:
            return []
        current = time.time() if now is None else now
        moment = datetime.fromtimestamp(current)
        day_key = moment.strftime("%Y-%m-%d")
        if (moment.hour, moment.minute) < (self.cfg.hour, self.cfg.minute):
            return []

        targets: list[int] = list(self.cfg.groups)
        if not targets:
            targets = await self.service.active_group_sessions(now=current)
        sent: list[int] = []
        for group_id in targets:
            if self._last_day.get(group_id) == day_key:
                continue
            try:
                ok = await self.service.send_daily_report(group_id, now=current)
            except Exception:
                log.exception("每日群报发送失败：%s", group_id)
                continue
            if not ok:
                continue
            self._last_day[group_id] = day_key
            sent.append(group_id)
        return sent
