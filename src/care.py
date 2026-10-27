"""主动关心：从群聊里发现值得惦记的事，过一两天主动问一句。

- `extract_care()`：纯规则识别（考试/生病/加班/生日/情绪低落），不花模型钱；
- `CareScheduler`：每 10 分钟检查一次到期事件，白天才发；
- 防打扰：每人每 N 天最多关心一次、每群每天最多几条（在 ChatService 里判断）。
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time

log = logging.getLogger("care")

# 关键词 → 事件类型（顺序有意义：考试优先于其他）
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(考试|考完|考了|面试|答辩|考研|期末|月考|期中)"), "exam"),
    (re.compile(r"(感冒|发烧|生病|不舒服|头疼|牙疼|胃疼|拉肚子|住院|输液)"), "sick"),
    (re.compile(r"(加班|通宵|熬夜|赶工|ddl|deadline)", re.IGNORECASE), "tired"),
    (re.compile(r"(今天|明天|后天)?[^。！？\n]{0,4}生日"), "birthday"),
    (re.compile(r"(失恋|分手|吵架|难过|emo|想哭|哭了)"), "sad"),
]

# 事件之后多久关心（天）
_DELAY_DAYS: dict[str, float] = {
    "exam": 0.5,
    "sick": 1.0,
    "tired": 1.0,
    "birthday": 0.02,  # 几乎立刻（生日当天）
    "sad": 1.0,
}

CARE_LINES: dict[str, tuple[str, ...]] = {
    "exam": (
        "考完了吧？感觉怎么样，别太有压力。",
        "今天考试顺利吗？考完就好好休息一下。",
        "考试的事别太紧张，尽力就行，考完跟我说说。",
    ),
    "sick": (
        "昨天说不太舒服，今天好点没？多喝点水。",
        "身体怎么样了？别硬撑，难受就去看看。",
        "记得吃药，早点睡，好点了跟我说一声。",
    ),
    "tired": (
        "还在加班吗？别太晚，早点睡。",
        "最近辛苦了，忙完记得休息一下。",
        "别老熬夜，身体会抗议的。",
    ),
    "birthday": (
        "生日快乐！今天要开心一点。",
        "生日这天别太累，祝一切顺利。",
    ),
    "sad": (
        "心情好点了吗？想聊的话我都在。",
        "别一个人扛着，难受就说出来。",
    ),
}


class CareEvent:
    def __init__(self, kind: str, detail: str):
        self.kind = kind
        self.detail = detail


def extract_care(text: str) -> CareEvent | None:
    """从一句话里判断有没有值得关心的事（纯规则，不调模型）。"""
    body = (text or "").strip()
    if not body or len(body) > 200:
        return None
    for pattern, kind in _PATTERNS:
        match = pattern.search(body)
        if match:
            return CareEvent(kind=kind, detail=match.group(0)[:60])
    return None


def care_due(kind: str, created_at: float | None = None) -> float:
    """事件发生多久之后再关心。"""
    now = time.time() if created_at is None else created_at
    return now + _DELAY_DAYS.get(kind, 1.0) * 86400


def pick_care_line(kind: str) -> str:
    lines = CARE_LINES.get(kind) or CARE_LINES["sick"]
    return random.choice(lines)


class CareScheduler:
    """每 10 分钟检查一次到期的关心事件；只在 9:00~23:00 发。"""

    def __init__(self, service, interval: float = 600.0):
        self.service = service
        self.interval = max(60.0, float(interval))
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self.run())
        log.info("主动关心调度器已启动（每 %.0f 秒检查一次）", self.interval)

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
                log.exception("主动关心检查出错")

    async def tick_once(self, now: float | None = None) -> list[int]:
        """检查一轮到期关心；返回成功发出的 id（便于测试）。"""
        current = time.time() if now is None else now
        hour = time.localtime(current).tm_hour
        if not (9 <= hour < 23):
            return []
        due = await self.service.memory.due_care_events(current)
        sent: list[int] = []
        for item in due:
            care_id = int(item.get("id") or 0)
            try:
                ok = await self.service.send_care(item)
            except Exception:
                log.exception("发送关心失败：%s", care_id)
                continue
            if not ok:
                continue
            await self.service.memory.mark_care_sent(care_id)
            sent.append(care_id)
        return sent
