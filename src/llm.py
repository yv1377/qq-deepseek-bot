"""DeepSeek 客户端（OpenAI 兼容的 chat/completions 接口）。

- 超时 / 429 / 5xx 自动重试，指数退避；
- 支持 function calling（工具调用），是否启用由配置决定；
- 后台摘要复用同一个客户端，用更低温度和更小的 max_tokens。
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Callable

import httpx

from config import DeepSeekConfig

log = logging.getLogger("llm")

UsageListener = Callable[[dict[str, Any]], None]

SUMMARY_SYSTEM = (
    "你是群聊机器人的「上下文管家」。把【已有摘要】和【新增对话】合并成一段中文记忆，"
    "供之后的聊天接话、找回上下文用。按下面四节输出，每节 1~4 条短句，总长不超过 700 字：\n"
    "## 长期事实\n"
    "人物、称呼、关系、偏好、约定、被明确要求记住的事；新旧信息冲突时以新增对话为准。\n"
    "## 未完成的事\n"
    "答应了还没做、聊到一半没结论、接下来要跟进的事（写清是谁的事）。没有就写「无」。\n"
    "## 关键原话\n"
    "最多 3 条值得原样记住的短句，格式「昵称：原话」；没有就写「无」。\n"
    "## 最近话题\n"
    "1~2 条最近在聊什么、反复出现的梗，方便下次接话。\n"
    "要求：不编造，不写寒暄和重复内容，丢掉单张图片的细节描述。"
    "这是一段会被压缩的检查点，不是聊天记录，不要输出对话以外的东西。"
)

MEMORY_SYSTEM = (
    "你是群聊机器人的「记忆管家」。阅读最近的对话，只抽取**跨会话依然有用**的稳定信息。\n"
    "全程用中文思考，不要输出英文分析。\n"
    "输出规则：每条一行、以「- 」开头、不超过 40 字、最多 6 条；不要标题、编号、解释。\n"
    "格式固定两种：\n"
    "- [群] 跟本会话/本群相关的长期事实（约定、氛围、群梗、共同事项）\n"
    "- [人] QQ号|昵称|关于这个人的长期事实（称呼、身份、稳定偏好、在忙什么、人物关系）\n"
    "值得记：群友的称呼/身份、稳定偏好（爱吃什么、玩什么）、明确约定（约好一起做什么）、"
    "长期在忙的事（在准备考试/在找工作）、人物关系。\n"
    "不要记：本次聊天具体发生了什么（谁发了什么图、谁说了哪句话）、一次性的情绪和玩笑、"
    "临时话题、复述对方原话、你已经知道的常识。\n"
    "涉及具体某个人时优先用 [人] 行，尽量从说话人标签里拿到 QQ 号。\n"
    "拿不准就别记。没有值得长期记的内容，只输出 [NONE]。"
)

DISTILL_SYSTEM = (
    "你是群聊机器人的「记忆整理员」。把一批零散笔记合并成更少、更稳定的长期画像。\n"
    "要求：按人物/主题归并，去掉一次性和重复内容，只保留以后还用得上的信息。\n"
    "输出：每条一行、以「- 」开头、不超过 50 字；合并后不超过 {limit} 条；"
    "每条尽量带上是谁的事；不要标题、编号、解释。"
)


class LLMError(RuntimeError):
    """模型调用失败。"""


class DeepSeekClient:
    def __init__(self, cfg: DeepSeekConfig, http: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(cfg.timeout, connect=15.0),
            headers={"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {},
        )
        self._usage_listener: UsageListener | None = None
        if not cfg.api_key:
            log.warning("没有配置 DeepSeek API Key，模型调用会一直失败")

    def set_usage_listener(self, listener: UsageListener | None) -> None:
        """Phase 4.3：注册用量回调（MetricsStore.record）。"""
        self._usage_listener = listener

    def _notify_usage(self, *, kind: str, usage: dict[str, Any], duration_ms: float) -> None:
        if self._usage_listener is None:
            return
        details = usage.get("prompt_tokens_details") or {}
        cache_hit = int(
            usage.get("prompt_cache_hit_tokens")
            or details.get("cached_tokens")
            or 0
        )
        cache_miss = int(usage.get("prompt_cache_miss_tokens") or 0)
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        if cache_miss <= 0 and cache_hit > 0:
            cache_miss = max(0, prompt_tokens - cache_hit)
        record = {
            "kind": kind,
            "model": self.cfg.model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
            "cache_hit_tokens": cache_hit,
            "cache_miss_tokens": cache_miss,
            "duration_ms": duration_ms,
            "ok": True,
        }
        try:
            self._usage_listener(record)
        except Exception:  # 统计失败不能影响聊天
            log.exception("记录用量失败")

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        kind: str = "chat",
        on_usage: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """调用一次 chat/completions，返回 assistant message 字典。"""
        started = time.perf_counter()
        url = f"{self.cfg.base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": self.cfg.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        last_error: Exception | None = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                response = await self._http.post(url, json=payload)
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning("DeepSeek 网络错误（第 %s 次）：%s", attempt + 1, exc)
            else:
                if response.status_code == 200:
                    data = response.json()
                    duration_ms = (time.perf_counter() - started) * 1000
                    usage = data.get("usage") or {}
                    self._notify_usage(kind=kind, usage=usage, duration_ms=duration_ms)
                    if on_usage is not None:
                        try:
                            on_usage(usage)
                        except Exception:  # 预算统计失败不能影响聊天
                            log.exception("用量回调失败")
                    try:
                        return data["choices"][0]["message"]
                    except (KeyError, IndexError, TypeError) as exc:
                        raise LLMError(f"DeepSeek 返回格式异常：{str(data)[:300]}") from exc
                if response.status_code == 401:
                    raise LLMError("DeepSeek API Key 无效或未配置（检查 .env）")
                if response.status_code in {429, 500, 502, 503, 504}:
                    last_error = LLMError(f"HTTP {response.status_code}")
                    log.warning(
                        "DeepSeek 暂时不可用（HTTP %s，第 %s 次）",
                        response.status_code,
                        attempt + 1,
                    )
                else:
                    raise LLMError(
                        f"DeepSeek 返回 HTTP {response.status_code}：{response.text[:300]}"
                    )

            if attempt < self.cfg.max_retries:
                await asyncio.sleep(1.6**attempt + random.random() * 0.5)

        raise LLMError(f"DeepSeek 调用失败：{last_error}")

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        kind: str = "chat",
        on_usage: Callable[[dict[str, Any]], None] | None = None,
    ) -> str:
        """只要纯文本回复。"""
        message = await self.complete(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            kind=kind,
            on_usage=on_usage,
        )
        return str(message.get("content") or "").strip()

    async def summarize(
        self,
        old_summary: str,
        transcript: str,
        *,
        on_usage: Callable[[dict[str, Any]], None] | None = None,
    ) -> str:
        """把「旧摘要 + 新对话」合并成新摘要（有长度上限，避免摘要越滚越大）。"""
        user_content = (
            f"【已有摘要】\n{old_summary.strip()[:2000] or '（暂无）'}\n\n"
            f"【新增对话】\n{transcript[:6000]}"
        )
        return await self.chat(
            [
                {"role": "system", "content": SUMMARY_SYSTEM},
                {"role": "user", "content": user_content},
            ],
            temperature=0.2,
            max_tokens=700,
            kind="summary",
            on_usage=on_usage,
        )

    async def consolidate(
        self,
        transcript: str,
        existing_notes: str = "",
        *,
        on_usage: Callable[[dict[str, Any]], None] | None = None,
    ) -> str:
        """记忆管家：从最近对话里抽取值得长期记住的事实（一行一条）。"""
        user_content = (
            f"【已经记住的内容】\n{existing_notes.strip()[:1500] or '（暂无）'}\n\n"
            f"【最近的对话】\n{transcript[:5000]}"
        )
        return await self.chat(
            [
                {"role": "system", "content": MEMORY_SYSTEM},
                {"role": "user", "content": user_content},
            ],
            temperature=0.2,
            max_tokens=400,
            kind="memory",
            on_usage=on_usage,
        )

    async def distill(
        self,
        notes: str,
        *,
        limit: int = 8,
        on_usage: Callable[[dict[str, Any]], None] | None = None,
    ) -> str:
        """把大量零散笔记压缩成少量长期画像（记忆治理用）。"""
        max_items = max(1, int(limit))
        user_content = (
            f"【需要合并的笔记】\n{notes[:8000]}\n\n"
            f"【要求】合并后不超过 {max_items} 条，保留最稳定的信息。"
        )
        return await self.chat(
            [
                {"role": "system", "content": DISTILL_SYSTEM.format(limit=max_items)},
                {"role": "user", "content": user_content},
            ],
            temperature=0.1,
            max_tokens=500,
            kind="memory",
            on_usage=on_usage,
        )

    async def care_line(
        self,
        name: str,
        kind: str,
        detail: str,
        *,
        on_usage: Callable[[dict[str, Any]], None] | None = None,
    ) -> str:
        """写一句贴合具体事件的主动关心话。"""
        user_content = (
            f"群友「{name}」最近说：「{detail}」（类型：{kind}）。\n"
            "写一句 20~40 字的关心话：像朋友随口问一句，自然、不鸡汤、不用 emoji、不要引号。"
        )
        return await self.chat(
            [
                {
                    "role": "system",
                    "content": "你是群聊机器人，说话自然、有点黏人但不肉麻。",
                },
                {"role": "user", "content": user_content},
            ],
            temperature=0.9,
            max_tokens=120,
            kind="care",
            on_usage=on_usage,
        )

    async def aclose(self) -> None:
        await self._http.aclose()
