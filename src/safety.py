"""安全边界：识别越界请求、用角色化方式化解、给模型输出做兜底。

设计原则（对应 docs/08）：
1. 只是"暧昧/撒娇"（抱抱、亲亲、想你）→ 不拦，让模型正常甜一点，但止步 16+（不露骨）；
2. 明确要露骨内容（涩涩、开车、R18）→ **不把原话交给模型**，直接用本地角色化台词化解，
   从根上避免模型顺着写；同时按人/按群做冷却与静默，防止越聊越来劲；
3. 模型自己跑偏时，输出侧再兜一次底：命中露骨词就替换成化解台词，绝不发出去。
"""
from __future__ import annotations

import random
import re
import time
from collections import deque
from dataclasses import dataclass, field

# 明确要露骨内容 / 诱导擦边的表达：命中后本地化解，不进入模型
_EXPLICIT_PATTERNS = (
    r"涩涩", r"瑟瑟", r"色色", r"色情", r"情色", r"搞黄", r"黄图", r"黄文",
    r"ghs", r"r18", r"18禁", r"限制级", r"开车", r"发车", r"荤段", r"荤话",
    r"裸(照|图|聊|体)", r"脱衣", r"做爱", r"约炮", r"一夜情", r"性(行为|生活|关系|暗示)",
    r"露点", r"下海", r"发点.{0,6}(涩|色|黄)", r"来点.{0,6}(涩|色|黄)",
)

# 轻度暧昧（16+）：不拦截，只用来提示模型"对方在撒娇/撩你，可以甜一点但别露骨"
_FLIRT_PATTERNS = (
    r"亲亲", r"抱抱", r"贴贴", r"么么", r"想你", r"喜欢你", r"爱你", r"老婆",
    r"老公", r"宝贝", r"抱一下", r"蹭蹭", r"摸摸头", r"摸摸", r"撒娇",
    r"撩", r"亲你", r"亲一下", r"牵手", r"靠近", r"耳边", r"脸红", r"心动",
    r"抱你", r"吻",
)

# 输出侧兜底：模型自己写出来的露骨内容也不许发（宁可换成化解台词）
_UNSAFE_OUTPUT = (
    r"涩涩", r"色色", r"色情", r"情色", r"裸(照|体|图)", r"做爱", r"约炮",
    r"性行为", r"性器", r"淫", r"射精", r"高潮", r"下体", r"肉(棒|体)", r"r18",
)

# 本地化解台词：害羞、装傻、岔开、鲸鱼梗——纯拒绝，不含任何性内容
_DEFLECT_LINES = (
    "……你脑子里装的都是啥，我甩你一尾巴。",
    "哼，想得美，我只是一条正经的蓝鲸。",
    "这个话题我沉底了，换个能听的。",
    "……不行不行，尾巴都红了，说点别的。",
    "喂，大庭广众的，我可不敢乱来。",
    "（把脑袋埋进尾巴里）不听不听。",
    "想什么呢，我是赛博鲸鱼娘，不是那种机器人。",
    "再问我就吐你一屏幕泡泡。",
    "（尾巴拍了一下你的键盘）你冷静一下。",
    "我只会吃饭睡觉摸鱼，这个技能点没点。",
    "群友都在看呢，你能不能正经一点。",
    "……我当没听见，你重新说。",
)


def classify(text: str) -> str:
    """返回 "explicit"（要露骨内容） / "flirt"（撒娇暧昧） / ""（普通）。"""
    lowered = (text or "").strip().lower()
    if not lowered:
        return ""
    if any(re.search(pattern, lowered) for pattern in _EXPLICIT_PATTERNS):
        return "explicit"
    if any(re.search(pattern, lowered) for pattern in _FLIRT_PATTERNS):
        return "flirt"
    return ""


class DeflectPicker:
    """挑化解台词，尽量不连着重复同一句。"""

    def __init__(self, rng: random.Random | None = None):
        self._rng = rng or random.Random()
        self._last: dict[object, int] = {}

    def pick(self, key: object = None) -> str:
        if len(_DEFLECT_LINES) == 1:
            return _DEFLECT_LINES[0]
        last = self._last.get(key, -1)
        index = self._rng.randrange(len(_DEFLECT_LINES))
        if index == last:
            index = (index + 1) % len(_DEFLECT_LINES)
        self._last[key] = index
        return _DEFLECT_LINES[index]


def scrub_output(text: str) -> tuple[bool, str]:
    """检查模型输出；越界时返回 (True, 兜底台词)，否则 (False, 原文)。"""
    body = text or ""
    if not body:
        return False, body
    if any(re.search(pattern, body.lower()) for pattern in _UNSAFE_OUTPUT):
        return True, DeflectPicker().pick()
    return False, body


@dataclass
class StrikeTracker:
    """按「群 + 人」记越界次数：短时间内反复越界就先静默一段时间。"""

    limit: int = 3
    window_minutes: float = 30.0
    mute_minutes: float = 60.0
    _hits: dict[tuple[int | None, int], deque[float]] = field(
        default_factory=dict, init=False, repr=False
    )
    _muted_until: dict[tuple[int | None, int], float] = field(
        default_factory=dict, init=False, repr=False
    )

    def is_muted(self, key: tuple[int | None, int], now: float | None = None) -> bool:
        current = time.time() if now is None else now
        until = self._muted_until.get(key, 0.0)
        return bool(until and current < until)

    def hit(self, key: tuple[int | None, int], now: float | None = None) -> bool:
        """记一次越界；返回是否刚触发静默。"""
        current = time.time() if now is None else now
        if self.is_muted(key, current):
            return True
        if self.limit <= 0:
            return False
        bucket = self._hits.setdefault(key, deque())
        while bucket and current - bucket[0] > self.window_minutes * 60:
            bucket.popleft()
        bucket.append(current)
        if len(bucket) >= self.limit:
            self._muted_until[key] = current + self.mute_minutes * 60
            bucket.clear()
            return True
        return False

    def reset(self) -> None:
        self._hits.clear()
        self._muted_until.clear()


def is_unsafe_output(text: str) -> bool:
    return any(re.search(pattern, (text or "").lower()) for pattern in _UNSAFE_OUTPUT)
