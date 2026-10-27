"""心情系统：她会被夸、被戳烦、被骂，也会慢慢平复。

设计得很轻：
- 每个群（和私聊）各有一份心情能量 0~100，平静时回到 baseline；
- 事件加减分：被夸 +6、被骂 -6、被戳 -4（连续戳额外扣）、正常聊天 +1；
- 心情影响三件事：回话语气（注入环境块）、自画像选哪张、被戳时是否直接开凶。
"""
from __future__ import annotations

import time

POSITIVE_WORDS = (
    "好可爱", "可爱", "乖", "喜欢你", "爱你", "厉害", "聪明", "棒", "好看",
    "温柔", "贴心", "辛苦了", "谢谢你", "好棒",
)
NEGATIVE_WORDS = (
    "傻", "笨", "滚", "废物", "闭嘴", "丑", "烦", "讨厌", "菜", "垃圾",
)


def detect(text: str) -> int:
    """按消息内容估一个心情增减（粗略但够用）。"""
    body = (text or "").strip()
    if not body:
        return 0
    delta = 0
    if any(word in body for word in POSITIVE_WORDS):
        delta += 6
    if any(word in body for word in NEGATIVE_WORDS):
        delta -= 6
    return delta


class MoodState:
    """一个会话的心情能量。"""

    def __init__(self, energy: float = 55.0, now: float | None = None):
        self.energy = max(0.0, min(100.0, float(energy)))
        self.updated = time.time() if now is None else now
        self.events: list[tuple[float, str, int]] = []

    def decay(self, *, baseline: float = 55.0, rate: float = 0.5,
              now: float | None = None, max_minutes: float = 240.0) -> None:
        """按分钟数向 baseline 回落。"""
        current = time.time() if now is None else now
        minutes = max(0.0, (current - self.updated) / 60.0)
        minutes = min(minutes, max_minutes)
        if minutes <= 0:
            return
        if self.energy > baseline:
            self.energy = max(baseline, self.energy - rate * minutes)
        elif self.energy < baseline:
            self.energy = min(baseline, self.energy + rate * minutes)
        self.updated = current

    def apply(self, delta: int, reason: str = "", *, baseline: float = 55.0,
              rate: float = 0.5, now: float | None = None) -> None:
        current = time.time() if now is None else now
        self.decay(baseline=baseline, rate=rate, now=current)
        self.energy = max(0.0, min(100.0, self.energy + delta))
        if reason:
            self.events.append((current, reason, delta))
            self.events = self.events[-6:]

    def label(self) -> str:
        if self.energy >= 80:
            return "很开心"
        if self.energy >= 60:
            return "还不错"
        if self.energy >= 40:
            return "平静"
        if self.energy >= 22:
            return "有点烦"
        return "很生气"

    def tone(self) -> str:
        if self.energy >= 80:
            return "你现在心情很好：可以更活泼、更甜一点，愿意多说两句。"
        if self.energy >= 60:
            return "你现在心情不错，正常发挥就好。"
        if self.energy >= 40:
            return "你现在心情平静，话少一点也没关系。"
        if self.energy >= 22:
            return "你现在有点烦：回话短一点、别太热情，可以不接无聊的梗。"
        return "你现在很烦躁：一句话怼回去就行，别解释、别撒娇。"

    def portrait_mood(self) -> str:
        """给自画像选图用（对应 portrait.json 里的 mood）。"""
        if self.energy >= 75:
            return "heart"
        if self.energy >= 55:
            return "normal"
        if self.energy >= 35:
            return "shy"
        return "angry"

    def snapshot(self) -> dict:
        return {
            "energy": round(self.energy, 1),
            "label": self.label(),
        }


class MoodBook:
    """按会话管理心情。"""

    def __init__(self, baseline: float = 55.0, decay_rate: float = 0.5):
        self.baseline = max(0.0, min(100.0, float(baseline)))
        self.decay_rate = max(0.01, float(decay_rate))
        self._states: dict[object, MoodState] = {}

    def get(self, key: object) -> MoodState:
        state = self._states.get(key)
        if state is None:
            state = MoodState(energy=self.baseline)
            self._states[key] = state
        return state

    def apply(self, key: object, delta: int, reason: str = "") -> MoodState:
        state = self.get(key)
        state.apply(
            delta,
            reason,
            baseline=self.baseline,
            rate=self.decay_rate,
        )
        return state

    def decay(self, key: object, now: float | None = None) -> MoodState:
        state = self.get(key)
        state.decay(baseline=self.baseline, rate=self.decay_rate, now=now)
        return state

    def snapshot(self, key: object) -> dict:
        return self.decay(key).snapshot()

    def snapshot_all(self) -> dict[str, dict]:
        return {str(key): self.decay(key).snapshot() for key in list(self._states)}

    def clear(self) -> None:
        self._states.clear()
