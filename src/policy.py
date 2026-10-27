"""决策层：这条消息要不要回？回之前要不要限流？回复内容怎么洗？

「像群友」的关键不在于模型多强，而在于——大多数人说话时它闭嘴。
所以这里把触发拆成几条明确规则，方便调参。
"""
from __future__ import annotations

import random
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from config import ChatConfig
from onebot import Incoming


@dataclass(slots=True)
class Decision:
    reply: bool
    reason: str = ""


class AmbientGate:
    """控制"没人叫它时主动插话"的频率，避免变成刷屏机器人。"""

    def __init__(self, chance: float, min_interval: float):
        self.chance = max(0.0, min(1.0, chance))
        self.min_interval = max(0.0, min_interval)
        self._last: dict[int, float] = {}

    def allow(
        self, group_id: int, *, rng: random.Random | None = None, now: float | None = None
    ) -> bool:
        if self.chance <= 0:
            return False
        current = time.time() if now is None else now
        if current - self._last.get(group_id, -1e18) < self.min_interval:
            return False
        if (rng or random).random() >= self.chance:
            return False
        self._last[group_id] = current
        return True

    def note_reply(self, group_id: int, now: float | None = None) -> None:
        """被正常叫到时也刷新计时，避免刚回完又随机插话。"""
        self._last[group_id] = time.time() if now is None else now


class RateLimiter:
    """冷却 + 每分钟上限，按 session（群 / 用户）计。"""

    def __init__(self) -> None:
        self._last: dict[str, float] = {}
        self._times: dict[str, deque[float]] = defaultdict(deque)

    def allow(
        self,
        key: str,
        *,
        cooldown: float,
        per_minute: int,
        now: float | None = None,
    ) -> bool:
        current = time.time() if now is None else now
        bucket = self._times[key]
        while bucket and current - bucket[0] > 60:
            bucket.popleft()
        if per_minute > 0 and len(bucket) >= per_minute:
            return False
        if current - self._last.get(key, -1e18) < cooldown:
            return False
        bucket.append(current)
        self._last[key] = current
        return True


def decide(
    incoming: Incoming,
    cfg: ChatConfig,
    bot_message_ids: dict[str, float],
    limiter: RateLimiter,
    ambient: AmbientGate,
    *,
    rng: random.Random | None = None,
    now: float | None = None,
) -> Decision:
    """返回这条消息要不要回复、以及命中的触发原因（用于日志）。"""
    filter_cfg = cfg.filter
    if incoming.is_self:
        return Decision(False, "self-message")

    if incoming.user_id in filter_cfg.blacklist_users:
        return Decision(False, "user-blacklisted")
    if filter_cfg.blocked_words and any(
        word and word in incoming.text for word in filter_cfg.blocked_words
    ):
        return Decision(False, "blocked-word")

    if incoming.kind == "group":
        group_id = incoming.group_id or 0
        if not cfg.group.enabled:
            return Decision(False, "group-disabled")
        if filter_cfg.whitelist_groups and group_id not in filter_cfg.whitelist_groups:
            return Decision(False, "group-not-whitelisted")
        if group_id in filter_cfg.blacklist_groups:
            return Decision(False, "group-blacklisted")

        reason = ""
        if incoming.at_self:
            reason = "at"
        elif not cfg.group.always_reply_at and incoming.reply_to and incoming.reply_to in bot_message_ids:
            reason = "reply-to-bot"
        else:
            lowered = incoming.text.lower()
            if not cfg.group.always_reply_at and any(
                word and word.lower() in lowered for word in cfg.group.wake_words
            ):
                reason = "wake-word"

        if not reason:
            if ambient.allow(group_id, rng=rng, now=now):
                reason = "ambient"
            else:
                return Decision(False, "no-trigger")

        if incoming.at_self and cfg.group.always_reply_at:
            return Decision(True, "at")

        if not limiter.allow(
            f"group:{group_id}",
            cooldown=cfg.group.cooldown,
            per_minute=cfg.group.per_minute,
            now=now,
        ):
            return Decision(False, "rate-limited")
        return Decision(True, reason)

    # 私聊
    if not cfg.private.enabled:
        return Decision(False, "private-disabled")
    if cfg.private.whitelist and incoming.user_id not in cfg.private.whitelist:
        return Decision(False, "private-not-whitelisted")
    if not limiter.allow(
        f"user:{incoming.user_id}",
        cooldown=cfg.private.cooldown,
        per_minute=cfg.private.per_minute,
        now=now,
    ):
        return Decision(False, "rate-limited")
    return Decision(True, "private")


# ---------------------------------------------------------------------------
# 输出清洗：QQ 不渲染 Markdown，模型却爱输出，所以统一洗成纯文本再发
# ---------------------------------------------------------------------------

_CODE_FENCE = re.compile(r"```[A-Za-z0-9_+-]*\n?(.*?)```", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]*)`")
_LINK = re.compile(r"\[([^\]]*)\]\((?:[^)]*)\)")
_BOLD = re.compile(r"(\*\*|__)(.+?)\1", re.S)
_STRIKE = re.compile(r"~~(.+?)~~", re.S)
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*", re.M)
_QUOTE = re.compile(r"^[ \t]{0,3}>[ \t]?", re.M)
_BULLET = re.compile(r"^[ \t]{0,3}[-*+][ \t]+", re.M)
_HR = re.compile(r"^[ \t]{0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$", re.M)
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
_TRAILING_SPACES = re.compile(r"[ \t]+\n")
_MULTI_NEWLINES = re.compile(r"\n{3,}")
_SENTENCE_END = re.compile(r"(?<=[。！？!?；;…~\n])")
# 固定开场/结尾（“主人，”“喵～”这类模板）直接清掉
_FIXED_OPENING = re.compile(
    r"^(?:(?:主人|喵呜|呜喵|喵|瞄)[\s，,、～~…!！。:：]*)+"
)
_CAT_TAIL = re.compile(r"[\s，,、]*(?:喵|瞄)[～~!！。…\s]*$")
# 模型在文字里“假装发图”（实际发图由系统另外做），这类括号动作要清掉
_FAKE_SEND = re.compile(
    r"[（(]\s*(?:发送|附上|配|给你发)[^（）()\n]{0,24}(?:图片|表情包?)\s*[）)]"
    r"|^\s*(?:发送|附图|配图)[^\n]{0,24}(?:图片|表情包?)[。.!！]?\s*$",
    re.M,
)


def clean_reply(text: str, *, bot_name: str = "", max_chars: int = 0) -> str:
    """去掉 Markdown 语法、多余空白、模型自报家门的"大肥鱼："前缀。"""
    if not text:
        return ""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _CODE_FENCE.sub(lambda match: match.group(1), cleaned)
    cleaned = _INLINE_CODE.sub(r"\1", cleaned)
    cleaned = re.sub(r"\[([^\]]*)\]\((https?://[^\s)]+)\)", r"\1 \2", cleaned)
    cleaned = _LINK.sub(r"\1", cleaned)
    cleaned = _BOLD.sub(r"\2", cleaned)
    cleaned = _STRIKE.sub(r"\1", cleaned)
    cleaned = _HEADING.sub("", cleaned)
    cleaned = _QUOTE.sub("", cleaned)
    cleaned = _BULLET.sub("", cleaned)
    cleaned = _HR.sub("", cleaned)
    cleaned = _ZERO_WIDTH.sub("", cleaned)
    cleaned = _FAKE_SEND.sub("", cleaned)
    cleaned = _FIXED_OPENING.sub("", cleaned)
    cleaned = _CAT_TAIL.sub("", cleaned)
    cleaned = _TRAILING_SPACES.sub("\n", cleaned)
    cleaned = _MULTI_NEWLINES.sub("\n\n", cleaned).strip()

    # 模型有时会模仿聊天记录格式，加一句"大肥鱼："，去掉
    if bot_name:
        pattern = re.compile(rf"^\s*{re.escape(bot_name)}\s*[:：]\s*")
        cleaned = pattern.sub("", cleaned)
    # 去掉包裹整体的引号
    if len(cleaned) >= 2 and cleaned[0] in "\"“「" and cleaned[-1] in "\"”」":
        cleaned = cleaned[1:-1].strip()

    if max_chars > 0 and len(cleaned) > max_chars:
        cut = cleaned[:max_chars]
        last_stop = max(cut.rfind(ch) for ch in "。！？!?…；;")
        if last_stop >= max_chars * 0.5:
            cut = cut[: last_stop + 1]
        cleaned = cut.rstrip() + "…"
    return cleaned.strip()


def split_text(text: str, limit: int = 160) -> list[str]:
    """按句子切成若干条，单条不超过 limit，避免一条消息太长被折叠。"""
    text = (text or "").strip()
    if not text:
        return []
    if limit <= 0:
        return [text]
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    buffer = ""
    for piece in _SENTENCE_END.split(text):
        if not piece:
            continue
        if len(buffer) + len(piece) <= limit:
            buffer += piece
            continue
        if buffer.strip():
            chunks.append(buffer.strip())
            buffer = ""
        while len(piece) > limit:
            chunks.append(piece[:limit].strip())
            piece = piece[limit:]
        buffer = piece
    if buffer.strip():
        chunks.append(buffer.strip())
    return [chunk for chunk in chunks if chunk]


class RepeatGuard:
    """连续复读保护：同一个人短时间内反复发同一句话，就先不接。

    归一化会去掉空白和标点，所以「在吗」「在吗！」「在吗。。。」算同一句。
    """

    def __init__(self, limit: int = 3, window: float = 60.0):
        self.limit = max(0, int(limit))
        self.window = max(1.0, float(window))
        self._data: dict[tuple[str, int], deque[tuple[str, float]]] = defaultdict(deque)

    @staticmethod
    def normalize(text: str) -> str:
        cleaned = re.sub(r"\s+", "", str(text or ""))
        cleaned = re.sub(r"[，。！？!?,.~～、；;：:…·\-—]+", "", cleaned)
        return cleaned.lower()[:120]

    def should_skip(
        self, session_id: str, user_id: int, text: str, now: float | None = None
    ) -> bool:
        """记一条消息，返回是否应该跳过回复。"""
        if self.limit <= 0:
            return False
        norm = self.normalize(text)
        if not norm:
            return False
        key = (str(session_id), int(user_id))
        current = time.time() if now is None else now
        bucket = self._data[key]
        while bucket and current - bucket[0][1] > self.window:
            bucket.popleft()
        repeated = sum(1 for item, _ in bucket if item == norm)
        bucket.append((norm, current))
        if len(self._data) > 500:
            for item_key, items in list(self._data.items()):
                if not items or current - items[-1][1] > self.window:
                    self._data.pop(item_key, None)
        return repeated + 1 >= self.limit

    def reset(self) -> None:
        self._data.clear()
