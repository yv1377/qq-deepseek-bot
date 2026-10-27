"""定时提醒：把「明天提醒我交报告」变成真的会响的提醒。

- `parse_reminder()` / `parse_remind_time()`：把自然语言时间解析成时间戳；
- `ReminderScheduler`：定时检查到期的提醒，交给 ChatService 发出去。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta

log = logging.getLogger("reminder")

_HALF = re.compile(r"半\s*个?\s*小时")
_REL = re.compile(r"(\d+)\s*(分钟|分|小时|钟头)\s*(?:后|之后|以后|内)")
_CLOCK = re.compile(
    r"(今天|明天|后天|今晚|明晚)?\s*"
    r"(早上|上午|中午|下午|傍晚|晚上|夜里)?\s*"
    r"(\d{1,2})\s*[点时]\s*(半|\d{1,2}\s*分?)?"
)
_DATE = re.compile(r"(今天|明天|后天|今晚|明晚)")

_EVENING = {"下午", "傍晚", "晚上", "夜里"}


def _clock_due(match: re.Match, current: float) -> float:
    date_word = match.group(1) or ""
    period = match.group(2) or ""
    hour = int(match.group(3))
    minute_text = (match.group(4) or "").strip()

    minute = 30 if minute_text.startswith("半") else 0
    if minute_text and not minute_text.startswith("半"):
        digits = re.findall(r"\d+", minute_text)
        minute = int(digits[0]) if digits else 0

    if period in _EVENING and hour < 12:
        hour += 12
    if period == "中午" and hour < 11:
        hour += 12
    if date_word in {"今晚", "明晚"} and hour < 12:
        hour += 12
    hour = max(0, min(23, hour))
    minute = max(0, min(59, minute))

    base = datetime.fromtimestamp(current)
    target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if date_word in {"明天", "明晚"}:
        target += timedelta(days=1)
    elif date_word == "后天":
        target += timedelta(days=2)
    elif target.timestamp() <= current:
        # 没说具体日期（或说的今天已经过了），就顺延到明天
        target += timedelta(days=1)
    return target.timestamp()


def find_time(text: str, now: float | None = None) -> tuple[float, int, int] | None:
    """在文本里找时间表达，返回 (到期时间戳, 起始位置, 结束位置)。"""
    body = text or ""
    current = time.time() if now is None else now
    if not body.strip():
        return None

    match = _HALF.search(body)
    if match:
        return current + 1800, match.start(), match.end()

    match = _REL.search(body)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        seconds = amount * 3600 if unit in {"小时", "钟头"} else amount * 60
        return current + seconds, match.start(), match.end()

    match = _CLOCK.search(body)
    if match:
        return _clock_due(match, current), match.start(), match.end()

    match = _DATE.search(body)
    if match and match.group(1) in {"明天", "后天", "明晚"}:
        days = 2 if match.group(1) == "后天" else 1
        base = datetime.fromtimestamp(current)
        target = (base + timedelta(days=days)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
        return target.timestamp(), match.start(), match.end()
    return None


def parse_remind_time(text: str, now: float | None = None) -> float | None:
    """只取时间；解析不了返回 None。"""
    found = find_time(text, now)
    return found[0] if found else None


def parse_reminder(
    text: str, now: float | None = None
) -> tuple[float, str] | None:
    """返回 (到期时间戳, 去掉时间词后的提醒内容)。"""
    found = find_time(text, now)
    if found is None:
        return None
    due, start, end = found
    content = (text[:start] + " " + text[end:]).strip(" ，,。；;：:、\t")
    return due, content


def format_due(due: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(due))


class ReminderScheduler:
    """后台每分钟检查一次到期的提醒。"""

    def __init__(self, service, interval: float = 30.0):
        self.service = service
        self.interval = max(5.0, float(interval))
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self.run())
        log.info("提醒调度器已启动（每 %.0f 秒检查一次）", self.interval)

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
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                return
            try:
                await self.tick_once()
            except Exception:
                log.exception("提醒检查出错")

    async def tick_once(self, now: float | None = None) -> list[int]:
        """检查一轮到期提醒；返回成功发出的提醒 id（便于测试）。"""
        current = time.time() if now is None else now
        due = await self.service.memory.due_reminders(current)
        sent: list[int] = []
        for item in due:
            reminder_id = int(item.get("id") or 0)
            try:
                ok = await self.service.send_reminder(item)
            except Exception:
                log.exception("提醒发送失败：%s", reminder_id)
                continue
            if not ok:
                continue
            await self.service.memory.mark_reminder_done(reminder_id)
            sent.append(reminder_id)
        return sent
