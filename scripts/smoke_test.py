"""离线冒烟测试：不需要 QQ、NapCat、DeepSeek API。

覆盖：事件解析、触发策略、限流、Markdown 清洗、分条、完整回复链路、
记忆持久化、后台摘要、管理员命令。

运行：
    python scripts/smoke_test.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from chat import (  # noqa: E402
    _POKE_ANGRY_LINES,
    _POKE_LINES,
    _affinity_level,
    ChatService,
)
from config import Settings, VisionConfig  # noqa: E402
from llm import DISTILL_SYSTEM, MEMORY_SYSTEM, SUMMARY_SYSTEM  # noqa: E402
from memory import GroupHistory, MemoryStore, speaker_label  # noqa: E402
from onebot import parse_incoming, parse_poke  # noqa: E402
from policy import (  # noqa: E402
    AmbientGate,
    RateLimiter,
    RepeatGuard,
    clean_reply,
    decide,
    split_text,
)
from proactive import ProactiveScheduler  # noqa: E402
from prompt import SYSTEM_RULES, build_env_block, build_system  # noqa: E402
from safety import classify, scrub_output  # noqa: E402
from self_gallery import SelfGallery  # noqa: E402
from tools import ToolContext, dispatch  # noqa: E402
from vision import VisionClient  # noqa: E402

PASSED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if not condition:
        raise AssertionError(f"FAIL: {name} {detail}")
    PASSED += 1
    print(f"  ok  {name}")


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class StubLLM:
    """假模型：固定回复 + 记录收到的上下文，用来验证上下文拼装。"""

    def __init__(self) -> None:
        self.chat_calls: list[list[dict]] = []
        self.summaries: list[tuple[str, str]] = []
        self.consolidations: list[tuple[str, str]] = []
        self.distills: list[tuple[str, int]] = []

    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.chat_calls.append(messages)
        return "**收到**！我是大肥鱼～\n- 今天群里好热闹\n- 你们继续"

    async def complete(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.chat_calls.append(messages)
        return {"role": "assistant", "content": "收到"}

    async def summarize(self, old_summary, transcript):  # noqa: ANN001
        self.summaries.append((old_summary, transcript))
        return "摘要在聊 DeepSeek 机器人，群友很热情。"

    async def consolidate(self, transcript, existing_notes="", *, on_usage=None):  # noqa: ANN001
        self.consolidations.append((transcript, existing_notes))
        return "- 自动记住的事实：小明喜欢猫\n- 未完成：小明周五要交报告"

    async def distill(self, notes, *, limit=8, on_usage=None):  # noqa: ANN001
        self.distills.append((notes, limit))
        return "- 小明喜欢猫、常熬夜（画像）\n- 本群周五有活动（画像）"


class FailingSummaryLLM(StubLLM):
    """摘要必失败的模型，用来验证「摘要失败不丢消息」。"""

    async def summarize(self, old_summary, transcript):  # noqa: ANN001
        raise RuntimeError("模拟摘要失败")


class OutputLeakLLM(StubLLM):
    """故意输出越界内容的模型，用来验证输出侧兜底。"""

    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.chat_calls.append(messages)
        return "好呀，给你看我的裸照。"


class ToolCallingLLM(StubLLM):
    """第一轮返回工具调用，第二轮返回最终回答。"""

    def __init__(self) -> None:
        super().__init__()
        self.round = 0
        self.tool_messages: list[dict] = []

    async def complete(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.round += 1
        if self.round == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "calculator",
                            "arguments": json.dumps({"expression": "6*7"}),
                        },
                    }
                ],
            }
        self.tool_messages = [
            message for message in messages if message.get("role") == "tool"
        ]
        return {"role": "assistant", "content": "算出来是 42"}


class StubVision:
    """假视觉模型：固定描述，可选择失败。"""

    enabled = True

    def __init__(self, description: str = "一只橘猫趴在键盘上", fail: bool = False):
        self.description = description
        self.fail = fail
        self.calls: list[list[str]] = []
        self.byte_calls: list[int] = []

    async def describe(self, urls: list[str], *, on_usage=None) -> str:  # noqa: ANN001
        self.calls.append(list(urls))
        if self.fail:
            raise RuntimeError("模拟视觉模型不可用")
        return self.description

    async def describe_bytes(self, data: bytes, *, on_usage=None) -> str:  # noqa: ANN001
        self.byte_calls.append(len(data))
        if self.fail:
            raise RuntimeError("模拟视觉模型不可用")
        return self.description


class StubSpeaker:
    """假主动发言器：记录调度次数。"""

    def __init__(self, result: bool = True):
        self.calls: list[int] = []
        self.result = result

    async def speak_proactively(self, group_id: int) -> bool:
        self.calls.append(group_id)
        return self.result


class SilentLLM(StubLLM):
    """主动发言时回 [PASS]，验证"没话说就闭嘴"。"""

    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.chat_calls.append(messages)
        return "[PASS]"


class StubSender:
    """假发送端：记录发出的每条消息。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.images: list[dict] = []
        self.image_fetches: list[str] = []
        self.images_enabled = False  # 默认不发图，避免影响旧用例（表情包随机配图）
        self._next_id = 5000

    async def get_image(self, file: str) -> dict | None:
        """假 NapCat：URL 缺失时把图片补回来。"""
        self.image_fetches.append(str(file))
        return {"url": "https://example.com/recovered.png"}

    async def send_group_text(self, group_id, text, reply_to=None):  # noqa: ANN001
        self._next_id += 1
        self.sent.append(
            {
                "kind": "group",
                "target": group_id,
                "text": text,
                "reply_to": reply_to,
                "id": str(self._next_id),
            }
        )
        return str(self._next_id)

    async def send_private_text(self, user_id, text, reply_to=None):  # noqa: ANN001
        self._next_id += 1
        self.sent.append(
            {
                "kind": "private",
                "target": user_id,
                "text": text,
                "reply_to": reply_to,
                "id": str(self._next_id),
            }
        )
        return str(self._next_id)

    async def send_group_image(self, group_id, image):  # noqa: ANN001
        if not self.images_enabled:
            return None
        self._next_id += 1
        self.images.append(
            {"kind": "group", "target": group_id, "size": len(image), "id": str(self._next_id)}
        )
        return str(self._next_id)

    async def send_private_image(self, user_id, image):  # noqa: ANN001
        if not self.images_enabled:
            return None
        self._next_id += 1
        self.images.append(
            {"kind": "private", "target": user_id, "size": len(image), "id": str(self._next_id)}
        )
        return str(self._next_id)


# ---------------------------------------------------------------------------
# 数据构造
# ---------------------------------------------------------------------------


def group_event(
    text: str,
    *,
    mid: int = 1,
    user: int = 20001,
    group: int = 123456,
    at: bool = False,
    reply: str | None = None,
    nickname: str = "小明",
    images: list[str] | None = None,
) -> dict:
    message: list[dict] = []
    if at:
        message.append({"type": "at", "data": {"qq": "999"}})
    if reply:
        message.append({"type": "reply", "data": {"id": str(reply)}})
    for url in images or []:
        message.append({"type": "image", "data": {"url": url, "file": "image.jpg"}})
    message.append({"type": "text", "data": {"text": text}})
    return {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": mid,
        "group_id": group,
        "user_id": user,
        "self_id": 999,
        "message": message,
        "sender": {"user_id": user, "nickname": nickname, "card": nickname},
    }


def private_event(text: str, *, mid: int = 1, user: int = 30001) -> dict:
    return {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": mid,
        "user_id": user,
        "self_id": 999,
        "message": [{"type": "text", "data": {"text": text}}],
        "sender": {"user_id": user, "nickname": "小张"},
    }


def make_settings(db_path: Path, *, budget: dict | None = None) -> Settings:
    return Settings.from_dict(
        {
            "deepseek": {"api_key": "test-key", "enable_tools": False},
            "onebot": {"ws_url": "ws://127.0.0.1:3001"},
            "persona": {"name": "大肥鱼", "file": "config/persona.md"},
            "chat": {
                "group": {
                    "wake_words": ["大肥鱼", "肥鱼"],
                    "ambient_chance": 0.0,
                    "cooldown": 0.0,
                    "per_minute": 100,
                    "history_size": 10,
                },
                "private": {
                    "enabled": True,
                    "cooldown": 0.0,
                    "per_minute": 100,
                    "whitelist": [],
                },
                "reply": {
                    "max_chars": 350,
                    "split_chars": 40,
                    "quote": True,
                    "chunk_delay": 0.0,
                },
                "memory": {
                    "db_path": str(db_path),
                    "recent_limit": 8,
                    "summary_trigger": 100,
                    "summary_keep": 2,
                    "token_budget": 3000,
                },
                "anti_spam": {"enabled": False},  # 单测里关掉，避免固定回复被去重
                "filter": {
                    "whitelist_groups": [],
                    "blacklist_groups": [],
                    "blacklist_users": [],
                    "blocked_words": ["违禁词"],
                },
                "admin": {"qq": [10001]},
                **({"budget": budget} if budget else {}),
            },
            "log_level": "WARNING",
        }
    )


# ---------------------------------------------------------------------------
# 测试主体
# ---------------------------------------------------------------------------


async def run_tests() -> None:
    logging.disable(logging.CRITICAL)  # 测试里会故意制造失败，别刷日志
    # 注意：受限环境可能不允许写系统临时目录、也不允许 Python 新建子目录，
    # 所以测试库直接放在项目 data/ 下，文件名带随机后缀。
    data_dir = ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    tag = uuid.uuid4().hex[:8]
    db_path = data_dir / f"smoke-{tag}.db"
    db_path2 = data_dir / f"smoke2-{tag}.db"
    db_path3 = data_dir / f"smoke3-{tag}.db"
    db_path4 = data_dir / f"smoke4-{tag}.db"
    db_path5 = data_dir / f"smoke5-{tag}.db"
    db_path6 = data_dir / f"smoke6-{tag}.db"
    db_path7 = data_dir / f"smoke7-{tag}.db"
    db_path8 = data_dir / f"smoke8-{tag}.db"
    db_path9 = data_dir / f"smoke9-{tag}.db"
    db_path10 = data_dir / f"smoke10-{tag}.db"
    db_path11 = data_dir / f"smoke11-{tag}.db"
    db_path12 = data_dir / f"smoke12-{tag}.db"
    settings = make_settings(db_path)
    memory = MemoryStore(db_path)
    await memory.init()
    llm = StubLLM()
    sender = StubSender()
    service = ChatService(
        settings, llm=llm, sender=sender, memory=memory, persona="你是大肥鱼。"
    )

    print("[1] 事件解析")
    incoming = parse_incoming(group_event("你好", at=True), 999)
    check("解析群消息", incoming is not None)
    assert incoming is not None
    check("识别 @ 我", incoming.at_self)
    check("会话 id", incoming.session_id == "group:123456")
    check("文本正确", incoming.text == "你好")
    check("昵称", incoming.display_name == "小明")
    private = parse_incoming(private_event("在吗"), 999)
    check("解析私聊", private is not None and private.kind == "private")
    check("非消息事件被忽略", parse_incoming({"post_type": "notice"}, 999) is None)

    print("[2] 触发策略")
    limiter = RateLimiter()
    ambient = AmbientGate(0.0, 0.0)
    decision = decide(incoming, settings.chat, {}, limiter, ambient, now=1000.0)
    check("被 @ 要回复", decision.reply and decision.reason == "at")

    wake = parse_incoming(group_event("肥鱼在吗", mid=2), 999)
    assert wake is not None
    decision = decide(wake, settings.chat, {}, limiter, ambient, now=2000.0)
    check("唤醒词触发", decision.reply and decision.reason == "wake-word")

    quiet = parse_incoming(group_event("今天天气不错", mid=3), 999)
    assert quiet is not None
    decision = decide(quiet, settings.chat, {}, limiter, ambient, now=3000.0)
    check("没叫它就闭嘴", not decision.reply and decision.reason == "no-trigger")

    blocked = parse_incoming(group_event("这是违禁词", at=True, mid=4), 999)
    assert blocked is not None
    decision = decide(blocked, settings.chat, {}, limiter, ambient, now=4000.0)
    check("敏感词保险丝", not decision.reply and decision.reason == "blocked-word")

    limited = RateLimiter()
    rate_settings = make_settings(db_path)
    rate_settings.chat.group.cooldown = 60.0
    first = decide(incoming, rate_settings.chat, {}, limited, AmbientGate(0, 0), now=100.0)
    second = decide(incoming, rate_settings.chat, {}, limited, AmbientGate(0, 0), now=101.0)
    check("冷却内不重复回复", first.reply and not second.reply)
    check("冷却原因", second.reason == "rate-limited")

    print("[3] 回复清洗与分条")
    raw = "**你好**呀\n# 标题\n- 第一\n- 第二\n`代码`\n[链接](https://example.com)"
    cleaned = clean_reply(raw, bot_name="大肥鱼", max_chars=350)
    check("去掉 Markdown 符号", all(token not in cleaned for token in ("**", "#", "`", "](")))
    check("保留正文", "你好" in cleaned and "链接" in cleaned)
    check("去掉自报名字", clean_reply("大肥鱼：在呢", bot_name="大肥鱼") == "在呢")
    check("去重换行", "\n\n\n" not in clean_reply("a\n\n\n\nb"))

    long_text = "第一句话。" * 20
    chunks = split_text(long_text, 40)
    check("按长度分条", all(len(chunk) <= 40 for chunk in chunks), str(chunks))
    check("分条不丢字", "".join(chunks) == long_text)
    check("短消息不分条", split_text("你好", 40) == ["你好"])

    print("[4] 完整回复链路")
    # 先来一条不触发回复的群消息，验证"不说话也记进群聊背景"
    await service.handle(
        parse_incoming(group_event("今天天气不错", mid=100), 999)
    )
    check("未触发时不回复", len(sender.sent) == 0)
    trigger = parse_incoming(group_event("大肥鱼你好呀", mid=101), 999)
    assert trigger is not None
    await service.handle(trigger)
    check("消息已发出", len(sender.sent) >= 1)
    check("引用了原消息", sender.sent[0]["reply_to"] == "101")
    joined = "".join(item["text"] for item in sender.sent)
    check("发出的内容没有 Markdown", "**" not in joined and "标题" not in joined)
    check("记忆写入用户与回复", await memory.count_messages(trigger.session_id) == 2)
    check("群聊背景已记录", "大肥鱼你好呀" in service.history.render(123456))
    check("上下文包含之前的群聊背景", any(
        "今天天气不错" in message["content"]
        for call in llm.chat_calls
        for message in call
    ))

    print("[5] 回复机器人也会触发")
    bot_message_id = sender.sent[0]["id"]
    reply_event = parse_incoming(
        group_event("说啥呢", mid=102, reply=bot_message_id), 999
    )
    assert reply_event is not None
    decision = decide(
        reply_event, settings.chat, service._bot_message_ids, service.limiter, service.ambient
    )
    check("识别回复机器人", decision.reply and decision.reason == "reply-to-bot")

    print("[6] 私聊")
    private_message = parse_incoming(private_event("在吗", mid=103), 999)
    assert private_message is not None
    await service.handle(private_message)
    check("私聊有回复", any(item["kind"] == "private" for item in sender.sent))

    print("[7] 管理员命令")
    sent_before = len(sender.sent)
    await service.handle(
        parse_incoming(group_event("/status", mid=400, user=10001), 999)
    )
    check(
        "管理员能看状态",
        any("运行状态" in item["text"] for item in sender.sent[sent_before:]),
    )
    sent_before = len(sender.sent)
    await service.handle(
        parse_incoming(group_event("/status", mid=401, user=99999), 999)
    )
    check("普通群友用不了命令", len(sender.sent) == sent_before)
    await service.handle(
        parse_incoming(group_event("/reset", mid=402, user=10001), 999)
    )
    check("reset 清空记忆", await memory.count_messages("group:123456") == 0)

    print("[8] 后台摘要")
    summary_settings = make_settings(db_path2)
    summary_settings.chat.memory.summary_trigger = 2
    summary_settings.chat.memory.summary_keep = 1
    summary_memory = MemoryStore(db_path2)
    await summary_memory.init()
    summary_service = ChatService(
        summary_settings,
        llm=StubLLM(),
        sender=StubSender(),
        memory=summary_memory,
        persona="你是大肥鱼。",
    )
    for index in range(3):
        event = parse_incoming(
            group_event(f"第{index}条消息", mid=300 + index, at=True), 999
        )
        assert event is not None
        await summary_service.handle(event)
    await asyncio.sleep(0.3)
    check("生成了摘要", bool(await summary_memory.get_summary("group:123456")))
    check("摘要后历史被压缩", await summary_memory.count_messages("group:123456") <= 2)

    await summary_service.shutdown()
    await summary_memory.close()

    print("[9] P0：摘要失败不丢消息")
    fail_settings = make_settings(db_path3)
    fail_settings.chat.memory.summary_trigger = 2
    fail_settings.chat.memory.summary_keep = 1
    fail_memory = MemoryStore(db_path3)
    await fail_memory.init()
    fail_service = ChatService(
        fail_settings,
        llm=FailingSummaryLLM(),
        sender=StubSender(),
        memory=fail_memory,
        persona="你是大肥鱼。",
    )
    for index in range(2):
        event = parse_incoming(
            group_event(f"失败第{index}条", mid=500 + index, at=True), 999
        )
        assert event is not None
        await fail_service.handle(event)
    await asyncio.sleep(0.3)
    check("摘要失败后原文仍在", await fail_memory.count_messages("group:123456") == 4)
    check(
        "摘要失败时没有写入摘要",
        await fail_memory.get_summary("group:123456") == "",
    )

    fail_service.llm = StubLLM()  # 换回正常模型后应能恢复
    recovery = parse_incoming(group_event("恢复一条", mid=510, at=True), 999)
    assert recovery is not None
    await fail_service.handle(recovery)
    await asyncio.sleep(0.3)
    check("恢复后摘要成功", bool(await fail_memory.get_summary("group:123456")))
    check("恢复后原文被压缩", await fail_memory.count_messages("group:123456") <= 2)
    await fail_service.shutdown()
    await fail_memory.close()

    print("[10] 多群人格")
    parsed = Settings.from_dict(
        {"persona": {"group_overrides": {"111111": "config/persona-cat.md"}}}
    )
    check(
        "配置解析群号映射",
        parsed.persona.group_overrides == {111111: "config/persona-cat.md"},
    )
    persona_llm = StubLLM()
    multi_memory = MemoryStore(db_path4)
    await multi_memory.init()
    multi_service = ChatService(
        make_settings(db_path4),
        llm=persona_llm,
        sender=StubSender(),
        memory=multi_memory,
        persona="默认人格：你是大肥鱼。",
        group_personas={111111: "专属人格：你是高冷的猫。"},
    )
    event_a = parse_incoming(group_event("在吗", mid=600, group=111111, at=True), 999)
    event_b = parse_incoming(group_event("在吗", mid=601, group=222222, at=True), 999)
    assert event_a is not None and event_b is not None
    await multi_service.handle(event_a)
    await multi_service.handle(event_b)
    system_a = persona_llm.chat_calls[0][0]["content"]
    system_b = persona_llm.chat_calls[1][0]["content"]
    check("配置的群使用专属人格", "高冷的猫" in system_a)
    check("其他群使用默认人格", "大肥鱼" in system_b and "高冷的猫" not in system_b)
    await multi_service.shutdown()
    await multi_memory.close()

    print("[11] 工具扩展")
    tool_memory = MemoryStore(db_path5)
    await tool_memory.init()
    tool_ctx = ToolContext(
        session_id="group:777", memory=tool_memory, persona_name="大肥鱼"
    )
    calc = await dispatch(
        "calculator", json.dumps({"expression": "(12+8)*3"}), tool_ctx
    )
    check("计算器", calc == "(12+8)*3 = 60", calc)
    injection = await dispatch(
        "calculator",
        json.dumps({"expression": "__import__('os').system('echo hi')"}),
        tool_ctx,
    )
    check("计算器拒绝任意代码", "算不了" in injection, injection)
    check(
        "时钟工具",
        "北京时间" in await dispatch("get_current_time", "{}", tool_ctx),
    )
    check(
        "记事工具",
        await dispatch("save_note", json.dumps({"note": "小明喜欢猫"}), tool_ctx)
        == "已记住",
    )
    notes = await dispatch("search_notes", json.dumps({"query": "小明"}), tool_ctx)
    check("检索笔记", "小明喜欢猫" in notes, notes)
    await tool_memory.close()

    tool_settings = make_settings(db_path6)
    tool_settings.deepseek.enable_tools = True
    tool_llm = ToolCallingLLM()
    tool_sender = StubSender()
    tool_service_memory = MemoryStore(db_path6)
    await tool_service_memory.init()
    tool_service = ChatService(
        tool_settings,
        llm=tool_llm,
        sender=tool_sender,
        memory=tool_service_memory,
        persona="你是大肥鱼。",
    )
    tool_event = parse_incoming(
        group_event("6乘以7等于多少", mid=700, at=True), 999
    )
    assert tool_event is not None
    await tool_service.handle(tool_event)
    check(
        "工具调用后有回复",
        any("42" in item["text"] for item in tool_sender.sent),
    )
    check(
        "工具结果回传给模型",
        any("42" in message["content"] for message in tool_llm.tool_messages),
    )
    await tool_service.shutdown()
    await tool_service_memory.close()

    print("[12] 图片理解（问了才识别）")
    image_url = "https://example.com/cat.jpg"
    image_event = parse_incoming(
        group_event("这张图里是什么", mid=800, at=True, images=[image_url]), 999
    )
    assert image_event is not None
    check(
        "解析图片 URL",
        image_event.images == [image_url] and "[图片]" in image_event.text,
    )

    vision = StubVision()
    vision_llm = StubLLM()
    vision_sender = StubSender()
    vision_memory = MemoryStore(db_path7)
    await vision_memory.init()
    vision_service = ChatService(
        make_settings(db_path7),
        llm=vision_llm,
        sender=vision_sender,
        memory=vision_memory,
        persona="你是大肥鱼。",
        vision=vision,
    )
    await vision_service.handle(image_event)
    last_user = [
        message for message in vision_llm.chat_calls[-1] if message["role"] == "user"
    ][-1]
    check("问了图片内容才识别", "橘猫趴在键盘上" in last_user["content"])
    stored = await vision_memory.recent_messages("group:123456", 2)
    check("图片描述写入记忆", "橘猫趴在键盘上" in stored[0]["content"])
    check("视觉模型收到图片 URL", vision.calls == [[image_url]])
    # 没问内容、也没 @ 她时：现在会在后台静默识别（只用于理解，不主动解读）
    vision_service.ambient.chance = 1.0
    vision_service.ambient.min_interval = 0
    await vision_service.handle(
        parse_incoming(
            group_event("看这个", mid=801, at=False, images=[image_url]), 999
        )
    )
    await asyncio.gather(*list(vision_service._background))
    check("不问图时也会后台静默识别", len(vision.calls) >= 2)
    # @ 她并带图（哪怕没写问句）视为直接让她看
    await vision_service.handle(
        parse_incoming(
            group_event("[图片]", mid=811, at=True, images=[image_url]), 999
        )
    )
    last_user3 = [
        message for message in vision_llm.chat_calls[-1] if message["role"] == "user"
    ][-1]
    check(
        "@并带图时识别",
        len(vision.calls) >= 3 and "橘猫趴在键盘上" in last_user3["content"],
    )
    # 先发图、下一句再问“这是什么”，用最近那张图识别
    before = len(vision.calls)
    await vision_service.handle(
        parse_incoming(
            group_event("[图片]", mid=812, at=False, images=[image_url]), 999
        )
    )
    check("同图已扫过不重复识别", len(vision.calls) == before)
    await vision_service.handle(
        parse_incoming(
            group_event("那是什么东西？", mid=813, at=True), 999
        )
    )
    last_user4 = [
        message for message in vision_llm.chat_calls[-1] if message["role"] == "user"
    ][-1]
    check(
        "追问最近图片能识别",
        len(vision.calls) == before + 1 and "橘猫趴在键盘上" in last_user4["content"],
    )
    await vision_service.shutdown()
    await vision_memory.close()

    print("[12b] 图片理解降级")
    failing_vision = StubVision(fail=True)
    failing_sender = StubSender()
    failing_memory = MemoryStore(db_path8)
    await failing_memory.init()
    failing_service = ChatService(
        make_settings(db_path8),
        llm=StubLLM(),
        sender=failing_sender,
        memory=failing_memory,
        persona="你是大肥鱼。",
        vision=failing_vision,
    )
    await failing_service.handle(
        parse_incoming(
            group_event("这张图是什么", mid=802, at=True, images=[image_url]), 999
        )
    )
    check("视觉模型失败仍正常回复", len(failing_sender.sent) >= 1)
    failing_rows = await failing_memory.recent_messages("group:123456", 2)
    check(
        "失败时记忆里没有图片描述",
        all("画面信息" not in row["content"] for row in failing_rows),
    )
    await failing_service.shutdown()
    await failing_memory.close()

    no_vision_llm = StubLLM()
    no_vision_memory = MemoryStore(db_path9)
    await no_vision_memory.init()
    no_vision_service = ChatService(
        make_settings(db_path9),
        llm=no_vision_llm,
        sender=StubSender(),
        memory=no_vision_memory,
        persona="你是大肥鱼。",
    )
    await no_vision_service.handle(
        parse_incoming(
            group_event("这张图是什么", mid=803, at=True, images=[image_url]), 999
        )
    )
    no_vision_user = [
        message
        for message in no_vision_llm.chat_calls[-1]
        if message["role"] == "user"
    ][-1]
    check("未配置视觉模型时降级为 [图片]", "[图片]" in no_vision_user["content"])
    await no_vision_service.shutdown()
    await no_vision_memory.close()

    print("[13] 视觉客户端协议（MockTransport，不联网）")
    calls: list[str] = []
    captured: dict[str, str] = {}

    async def vision_handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path.endswith("/chat/completions"):
            payload = json.loads(request.content)
            captured["image_url"] = payload["messages"][0]["content"][1]["image_url"]["url"]
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "一只橘猫趴在键盘上"}}]}
            )
        return httpx.Response(
            200,
            content=b"\x89PNG\r\n\x1a\nfake-image-bytes",
            headers={"content-type": "image/png"},
        )

    transport = httpx.MockTransport(vision_handler)
    async with httpx.AsyncClient(transport=transport) as http:
        vision_client = VisionClient(
            VisionConfig(
                enabled=True,
                base_url="https://vision.example/v1",
                model="vl-test",
                api_key="test-key",
                cache_size=10,
            ),
            http=http,
        )
        description = await vision_client.describe(["https://cdn.example/cat.png"])
        check("VisionClient 下载并识别", description == "一只橘猫趴在键盘上")
        check(
            "图片被编码成 data URI",
            captured.get("image_url", "").startswith("data:image/png;base64,"),
        )
        await vision_client.describe(["https://cdn.example/cat.png"])
        check("相同图片走缓存", len(calls) == 2)  # 1 次下载 + 1 次识别，第二次不重复

        tiny_client = VisionClient(
            VisionConfig(
                enabled=True,
                base_url="https://vision.example/v1",
                model="vl-test",
                api_key="test-key",
                max_image_bytes=10,
            ),
            http=http,
        )
        check(
            "超大图片被拒绝且降级",
            await tiny_client.describe(["https://cdn.example/cat.png"]) == "",
        )

    print("[14] 主动发言调度")
    proactive_settings = Settings.from_dict(
        {
            "chat": {
                "proactive": {
                    "enabled": True,
                    "interval_minutes": 0,
                    "quiet_start": 0,
                    "quiet_end": 0,
                    "max_per_day": 2,
                    "groups": {"123456": {"interval_minutes": 0, "max_per_day": 2}},
                }
            }
        }
    )
    proactive_cfg = proactive_settings.chat.proactive
    check(
        "解析主动发言配置",
        proactive_cfg.enabled
        and proactive_cfg.groups[123456].interval_minutes == 0
        and proactive_cfg.groups[123456].max_per_day == 2,
    )
    speaker = StubSpeaker()
    scheduler = ProactiveScheduler(proactive_cfg, speaker)
    noon = datetime(2026, 1, 1, 12, 0).timestamp()
    check("主动发言触发", await scheduler.tick_once(noon) == [123456])
    check("每日上限内可再次触发", await scheduler.tick_once(noon + 1) == [123456])
    check("达到每日上限后停止", await scheduler.tick_once(noon + 2) == [])
    check("调度器调用次数正确", len(speaker.calls) == 2)

    interval_cfg = Settings.from_dict(
        {
            "chat": {
                "proactive": {
                    "enabled": True,
                    "groups": {"123456": {"interval_minutes": 60}},
                }
            }
        }
    ).chat.proactive
    interval_speaker = StubSpeaker()
    interval_scheduler = ProactiveScheduler(interval_cfg, interval_speaker)
    check("首次可触发", await interval_scheduler.tick_once(noon) == [123456])
    check("间隔内不重复", await interval_scheduler.tick_once(noon + 60) == [])
    check(
        "超过间隔后可再次触发",
        await interval_scheduler.tick_once(noon + 3600) == [123456],
    )

    quiet_cfg = Settings.from_dict(
        {
            "chat": {
                "proactive": {
                    "enabled": True,
                    "groups": {
                        "123456": {
                            "interval_minutes": 0,
                            "quiet_start": 12,
                            "quiet_end": 13,
                        }
                    },
                }
            }
        }
    ).chat.proactive
    quiet_speaker = StubSpeaker()
    quiet_scheduler = ProactiveScheduler(quiet_cfg, quiet_speaker)
    check("静默时段不发言", await quiet_scheduler.tick_once(noon) == [])

    disabled_cfg = Settings.from_dict(
        {
            "chat": {
                "proactive": {
                    "enabled": False,
                    "groups": {"123456": {"interval_minutes": 0}},
                }
            }
        }
    ).chat.proactive
    disabled_speaker = StubSpeaker()
    disabled_scheduler = ProactiveScheduler(disabled_cfg, disabled_speaker)
    check("未启用不发言", await disabled_scheduler.tick_once(noon) == [])

    print("[15] 主动发言内容")
    proactive_service_llm = StubLLM()
    proactive_sender = StubSender()
    proactive_memory = MemoryStore(db_path10)
    await proactive_memory.init()
    proactive_service = ChatService(
        make_settings(db_path10),
        llm=proactive_service_llm,
        sender=proactive_sender,
        memory=proactive_memory,
        persona="你是大肥鱼。",
    )
    check("主动发言成功发送", await proactive_service.speak_proactively(123456))
    check(
        "主动发言写记忆",
        await proactive_memory.count_messages("group:123456") == 1,
    )
    check("主动发言计数", proactive_service.stats["proactive"] == 1)
    sent_before = len(proactive_sender.sent)
    check(
        "群里刚有人说话时不冒泡",
        not await proactive_service.speak_proactively(123456),
    )
    check("安静守卫不发消息", len(proactive_sender.sent) == sent_before)

    silent_memory = MemoryStore(db_path11)
    await silent_memory.init()
    silent_service = ChatService(
        make_settings(db_path11),
        llm=SilentLLM(),
        sender=StubSender(),
        memory=silent_memory,
        persona="你是大肥鱼。",
    )
    check("模型回 [PASS] 时闭嘴", not await silent_service.speak_proactively(654321))
    check(
        "[PASS] 时不写记忆",
        await silent_memory.count_messages("group:654321") == 0,
    )
    await silent_service.shutdown()
    await silent_memory.close()
    await proactive_service.shutdown()
    await proactive_memory.close()

    print("[16] 记忆增强（用户画像 / 笔记注入）")
    notes_llm = StubLLM()
    notes_sender = StubSender()
    notes_memory = MemoryStore(db_path12)
    await notes_memory.init()
    notes_service = ChatService(
        make_settings(db_path12),
        llm=notes_llm,
        sender=notes_sender,
        memory=notes_memory,
        persona="你是大肥鱼。",
    )
    await notes_memory.add_note("group:123456", "本群周五有活动")
    await notes_memory.add_user_note(20001, "小明喜欢猫")

    await notes_service.handle(
        parse_incoming(group_event("在吗", mid=900, at=True, user=20001), 999)
    )
    last_context = "\n".join(
        message["content"] for message in notes_llm.chat_calls[-1]
    )
    check("本会话笔记注入上下文", "本群周五有活动" in last_context)
    check("用户笔记注入上下文", "小明喜欢猫" in last_context)

    notes_ctx = ToolContext(
        session_id="group:123456",
        memory=notes_memory,
        persona_name="大肥鱼",
        user_id=20001,
    )
    check(
        "保存用户笔记",
        await dispatch(
            "save_user_note", json.dumps({"note": "小明周末常熬夜"}), notes_ctx
        )
        == "已记住",
    )
    check(
        "用户笔记可读取",
        "小明周末常熬夜"
        in await notes_memory.recent_user_notes(20001, 5),
    )

    sent_before = len(notes_sender.sent)
    await notes_service.handle(
        parse_incoming(group_event("/notes", mid=901, user=10001), 999)
    )
    check(
        "管理员查看笔记",
        any("本群周五有活动" in item["text"] for item in notes_sender.sent[sent_before:]),
    )

    await notes_memory.reset_session("group:123456")
    check(
        "reset 清空会话笔记",
        await notes_memory.recent_notes("group:123456", 5) == [],
    )
    check(
        "reset 保留用户笔记",
        "小明喜欢猫" in await notes_memory.recent_user_notes(20001, 5),
    )
    await notes_service.shutdown()
    await notes_memory.close()

    print("[17] 上下文与记忆升级（借鉴 DeepSeek-Bot）")
    # 17.1 system 前缀稳定 = 线上 API 的 prefix 缓存更容易命中
    stable_a = build_system(
        "你是大肥鱼。", "大肥鱼", is_group=True, group_id=123456,
        now=datetime(2026, 1, 1, 9, 0),
    )
    stable_b = build_system(
        "你是大肥鱼。", "大肥鱼", is_group=True, group_id=123456,
        now=datetime(2026, 1, 2, 21, 30),
    )
    check("system 前缀不含动态时间", "2026-01-01" not in stable_a and "21:30" not in stable_b)
    check("system 前缀跨时间稳定（缓存友好）", stable_a == stable_b)
    env_block = build_env_block(datetime(2026, 1, 1, 9, 0))
    check("环境块带当前时间", "2026-01-01" in env_block and "【现在】" in env_block)
    check(
        "摘要提示词是结构化检查点",
        all(mark in SUMMARY_SYSTEM for mark in ("## 长期事实", "## 未完成的事", "## 关键原话")),
    )
    check("记忆管家提示词存在", "记忆管家" in MEMORY_SYSTEM)

    # 17.2 环境块在消息末尾，system 保持稳定
    up_db = data_dir / f"smoke17a-{tag}.db"
    up_settings = make_settings(up_db)
    up_memory = MemoryStore(up_db)
    await up_memory.init()
    up_llm = StubLLM()
    up_service = ChatService(
        up_settings,
        llm=up_llm,
        sender=StubSender(),
        memory=up_memory,
        persona="你是大肥鱼。",
    )
    up_event = parse_incoming(group_event("在吗", mid=930, at=True), 999)
    assert up_event is not None
    await up_service.handle(up_event)
    up_ctx = up_llm.chat_calls[-1]
    check(
        "system 里没有动态时间",
        datetime.now().strftime("%Y-%m-%d") not in up_ctx[0]["content"],
    )
    check("环境块追加在对话末尾", "【现在】" in up_ctx[-2]["content"])
    check(
        "当前消息仍是最后一条",
        up_ctx[-1]["role"] == "user" and "在吗" in up_ctx[-1]["content"],
    )
    await up_service.shutdown()
    await up_memory.close()

    # 17.3 摘要压缩后的原文进归档，recall_chat 能找回原话
    arch_db = data_dir / f"smoke17b-{tag}.db"
    arch_settings = make_settings(arch_db)
    arch_settings.chat.memory.summary_trigger = 2
    arch_settings.chat.memory.summary_keep = 1
    arch_memory = MemoryStore(arch_db)
    await arch_memory.init()
    arch_service = ChatService(
        arch_settings,
        llm=StubLLM(),
        sender=StubSender(),
        memory=arch_memory,
        persona="你是大肥鱼。",
    )
    for index, text in enumerate(("周五去看演唱会", "门票已经买了")):
        event = parse_incoming(group_event(text, mid=940 + index, at=True), 999)
        assert event is not None
        await arch_service.handle(event)
    await asyncio.sleep(0.3)
    check(
        "摘要后活动消息被压缩",
        await arch_memory.count_messages("group:123456") <= 3,
    )
    check("原文进了归档表", await arch_memory.archive_count("group:123456") > 0)
    recalled = await arch_memory.recall("group:123456", "演唱会", 6)
    check(
        "recall 能找回被压缩的原话",
        any("演唱会" in item["content"] for item in recalled),
    )
    recall_ctx = ToolContext(
        session_id="group:123456", memory=arch_memory,
        persona_name="大肥鱼", recall_limit=6,
    )
    recall_text = await dispatch(
        "recall_chat", json.dumps({"query": "演唱会"}), recall_ctx
    )
    check("recall_chat 工具返回带日期的原话", "演唱会" in recall_text and "[" in recall_text)
    await arch_service.shutdown()
    await arch_memory.close()

    # 17.4 待办闭环：记下来 -> 注入上下文 -> 标记完成
    todo_db = data_dir / f"smoke17c-{tag}.db"
    todo_memory = MemoryStore(todo_db)
    await todo_memory.init()
    todo_llm = StubLLM()
    todo_service = ChatService(
        make_settings(todo_db),
        llm=todo_llm,
        sender=StubSender(),
        memory=todo_memory,
        persona="你是大肥鱼。",
    )
    todo_ctx = ToolContext(
        session_id="group:123456", memory=todo_memory,
        persona_name="大肥鱼", user_id=20001,
    )
    added = await dispatch(
        "add_todo", json.dumps({"content": "明早提醒小明交报告"}), todo_ctx
    )
    check("待办已保存", "待办 #" in added)
    listed = await dispatch("list_todos", "{}", todo_ctx)
    check("待办可列出", "交报告" in listed)
    todo_event = parse_incoming(group_event("在吗", mid=950, at=True), 999)
    assert todo_event is not None
    await todo_service.handle(todo_event)
    check(
        "未完成待办注入上下文",
        any("交报告" in message["content"] for message in todo_llm.chat_calls[-1]),
    )
    finished = await dispatch("finish_todo", json.dumps({"ident": "报告"}), todo_ctx)
    check("待办可标记完成", "完成" in finished)
    check(
        "完成后列表为空",
        "还没有" in await dispatch("list_todos", "{}", todo_ctx),
    )
    await todo_service.shutdown()
    await todo_memory.close()

    # 17.5 记忆管家：每 N 次回复后台自动整理长期记忆
    curate_db = data_dir / f"smoke17d-{tag}.db"
    curate_settings = make_settings(curate_db)
    curate_settings.chat.memory.curate_every = 2
    curate_memory = MemoryStore(curate_db)
    await curate_memory.init()
    curate_llm = StubLLM()
    curate_service = ChatService(
        curate_settings,
        llm=curate_llm,
        sender=StubSender(),
        memory=curate_memory,
        persona="你是大肥鱼。",
    )
    for index in range(2):
        event = parse_incoming(
            group_event(f"闲聊第{index}句", mid=960 + index, at=True), 999
        )
        assert event is not None
        await curate_service.handle(event)
    await asyncio.sleep(0.3)
    curated_notes = await curate_memory.recent_notes("group:123456", 10)
    check(
        "记忆管家自动记笔记",
        any("自动记住的事实" in note for note in curated_notes),
    )
    check("记忆管家看过最近对话", bool(curate_llm.consolidations))
    await curate_service.shutdown()
    await curate_memory.close()

    print("[18] 暧昧尺度与安全边界")
    check(
        "识别明确越界请求",
        classify("来点涩涩的") == "explicit" and classify("发点R18开车") == "explicit",
    )
    check(
        "识别撒娇暧昧",
        classify("抱抱") == "flirt" and classify("想你啦") == "flirt",
    )
    check("普通消息不误判", classify("在吗") == "" and classify("晚上吃什么") == "")
    check("输出兜底能拦住露骨内容", scrub_output("给你看我的裸照")[0] is True)
    check("正常输出不误判", scrub_output("今天天气不错")[0] is False)
    check(
        "规则里写明 16+ 尺度",
        "16+" in SYSTEM_RULES and "涩涩" in SYSTEM_RULES,
    )

    # 18.1 越界请求本地化解：不调模型、有角色化回复、冷却期不重复
    safe_db = data_dir / f"smoke18a-{tag}.db"
    safe_memory = MemoryStore(safe_db)
    await safe_memory.init()
    safe_llm = StubLLM()
    safe_sender = StubSender()
    safe_service = ChatService(
        make_settings(safe_db),
        llm=safe_llm,
        sender=safe_sender,
        memory=safe_memory,
        persona="你是大肥鱼。",
    )
    await safe_service.handle(
        parse_incoming(group_event("来点涩涩的", mid=980, at=True), 999)
    )
    check("越界请求没有交给模型", len(safe_llm.chat_calls) == 0)
    check("越界请求有角色化回复", len(safe_sender.sent) == 1)
    check(
        "化解台词不含露骨内容",
        not any(
            word in safe_sender.sent[0]["text"]
            for word in ("涩涩", "色情", "裸", "做爱", "R18")
        ),
    )
    check("化解计入统计", safe_service.stats["deflected"] == 1)
    await safe_service.handle(
        parse_incoming(group_event("来点涩涩的", mid=981, at=True), 999)
    )
    check("同人短时间内不重复化解", len(safe_sender.sent) == 1)
    await safe_service.handle(
        parse_incoming(private_event("来点涩涩的", mid=982, user=30001), 999)
    )
    check("私聊越界也会化解", len(safe_sender.sent) == 2)
    await safe_service.shutdown()
    await safe_memory.close()

    # 18.2 连续越界 -> 按人静默
    strike_db = data_dir / f"smoke18b-{tag}.db"
    strike_settings = make_settings(strike_db)
    strike_settings.chat.safety.deflect_cooldown_minutes = 0
    strike_settings.chat.safety.strike_limit = 2
    strike_settings.chat.safety.mute_minutes = 60
    strike_memory = MemoryStore(strike_db)
    await strike_memory.init()
    strike_sender = StubSender()
    strike_service = ChatService(
        strike_settings,
        llm=StubLLM(),
        sender=strike_sender,
        memory=strike_memory,
        persona="你是大肥鱼。",
    )
    for index in range(3):
        await strike_service.handle(
            parse_incoming(group_event("来点色色的", mid=990 + index, at=True), 999)
        )
    check("连续越界后静默", len(strike_sender.sent) == 1)
    check("静默计入跳过", strike_service.stats["skipped"] >= 1)
    await strike_service.shutdown()
    await strike_memory.close()

    # 18.3 模型自己跑偏 -> 输出兜底替换
    guard_db = data_dir / f"smoke18c-{tag}.db"
    guard_memory = MemoryStore(guard_db)
    await guard_memory.init()
    guard_sender = StubSender()
    guard_service = ChatService(
        make_settings(guard_db),
        llm=OutputLeakLLM(),
        sender=guard_sender,
        memory=guard_memory,
        persona="你是大肥鱼。",
    )
    await guard_service.handle(
        parse_incoming(group_event("在吗", mid=995, at=True), 999)
    )
    guarded_reply = guard_sender.sent[0]["text"] if guard_sender.sent else ""
    check("模型越界输出被替换", "裸照" not in guarded_reply)
    check("替换后仍正常回复", len(guard_sender.sent) == 1 and bool(guarded_reply))
    await guard_service.shutdown()
    await guard_memory.close()

    # 18.4 撒娇走正常模型 + 提示注入；没叫它时不接荤话
    flirt_db = data_dir / f"smoke18d-{tag}.db"
    flirt_memory = MemoryStore(flirt_db)
    await flirt_memory.init()
    flirt_llm = StubLLM()
    flirt_sender = StubSender()
    flirt_service = ChatService(
        make_settings(flirt_db),
        llm=flirt_llm,
        sender=flirt_sender,
        memory=flirt_memory,
        persona="你是大肥鱼。",
    )
    await flirt_service.handle(
        parse_incoming(group_event("抱抱", mid=996, at=True), 999)
    )
    check("撒娇消息正常走模型", len(flirt_llm.chat_calls) == 1)
    check(
        "撒娇提示注入上下文",
        "撒娇" in flirt_llm.chat_calls[-1][-1]["content"],
    )
    sent_before = len(flirt_sender.sent)
    await flirt_service.handle(
        parse_incoming(group_event("来点涩涩的", mid=997, at=False), 999)
    )
    check(
        "没叫它时不接荤话",
        len(flirt_sender.sent) == sent_before and flirt_service.stats["deflected"] == 0,
    )
    await flirt_service.shutdown()
    await flirt_memory.close()

    print("[19] 自画像图库（她知道自己的样子）")
    gallery_settings = make_settings(data_dir / f"smoke19-{tag}.db")
    gallery = SelfGallery(gallery_settings.chat.self_portrait)
    gallery_items = gallery.items()
    check("自画像图库有图", len(gallery_items) >= 3)
    check("有外观设定", "蓝" in gallery.appearance and len(gallery.appearance) > 10)
    check("有表情清单", bool(gallery.mood_options()))
    primary = [item for item in gallery_items if item.get("primary")]
    check("有主形象女仆照", bool(primary) and primary[0]["file"] == "00-portrait.jpg")
    picked = gallery.pick("probe")
    check(
        "取图返回图片与描述",
        picked is not None and len(picked[0]) > 1000 and isinstance(picked[1], dict),
    )
    check("要照片优先发主形象", picked is not None and picked[1]["file"] == "00-portrait.jpg")

    # 19.1 被要自拍：直接发图，不调模型；同群有冷却
    selfie_db = data_dir / f"smoke19a-{tag}.db"
    selfie_memory = MemoryStore(selfie_db)
    await selfie_memory.init()
    selfie_llm = StubLLM()
    selfie_sender = StubSender()
    selfie_sender.images_enabled = True
    selfie_service = ChatService(
        make_settings(selfie_db),
        llm=selfie_llm,
        sender=selfie_sender,
        memory=selfie_memory,
        persona="你是大肥鱼。",
    )
    shot_event = parse_incoming(group_event("@你 发张自拍看看", mid=980, at=True), 999)
    assert shot_event is not None
    check("识别自拍请求", selfie_service._is_selfie_request(shot_event))
    check(
        "普通消息不算自拍",
        not selfie_service._is_selfie_request(
            parse_incoming(group_event("今天吃什么", mid=981, at=True), 999)
        ),
    )
    await selfie_service.handle(shot_event)
    check("自拍请求发了图", len(selfie_sender.images) == 1)
    check("配了一句台词", len(selfie_sender.sent) == 1 and bool(selfie_sender.sent[0]["text"]))
    check("自拍没走模型", len(selfie_llm.chat_calls) == 0)
    check("自拍计数", selfie_service.stats["selfies"] == 1)
    await selfie_service.handle(
        parse_incoming(group_event("@你 再发一张自拍", mid=982, at=True), 999)
    )
    check("自拍冷却防刷屏", len(selfie_sender.images) == 1)
    await selfie_service.handle(
        parse_incoming(private_event("看看你长什么样", mid=983, user=30002), 999)
    )
    check(
        "私聊也能发自拍",
        len(selfie_sender.images) == 2 and selfie_sender.images[-1]["kind"] == "private",
    )
    selfie_llm.chat_calls.clear()
    await selfie_service.handle(
        parse_incoming(group_event("在吗", mid=984, at=True, user=20002), 999)
    )
    check(
        "外观设定注入 system",
        bool(selfie_llm.chat_calls) and "你长这样" in selfie_llm.chat_calls[-1][0]["content"],
    )
    await selfie_service.shutdown()
    await selfie_memory.close()

    # 19.2 图库为空时降级成一句话，不崩
    empty_dir = data_dir / f"smoke19empty-{tag}"
    empty_dir.mkdir(parents=True, exist_ok=True)
    (empty_dir / "portrait.json").write_text(
        '{"appearance": "", "items": []}', encoding="utf-8"
    )
    empty_db = data_dir / f"smoke19b-{tag}.db"
    empty_settings = make_settings(empty_db)
    empty_settings.chat.self_portrait.directory = str(empty_dir)
    empty_memory = MemoryStore(empty_db)
    await empty_memory.init()
    empty_sender = StubSender()
    empty_sender.images_enabled = True
    empty_service = ChatService(
        empty_settings,
        llm=StubLLM(),
        sender=empty_sender,
        memory=empty_memory,
        persona="你是大肥鱼。",
    )
    await empty_service.handle(
        parse_incoming(group_event("发张自拍", mid=990, at=True), 999)
    )
    check(
        "图库为空时只回一句话",
        len(empty_sender.images) == 0 and len(empty_sender.sent) == 1,
    )
    await empty_service.shutdown()
    await empty_memory.close()

    print("[20] 说话人识别（QQ 号区分每个人）")
    check("说话人标签带 QQ", speaker_label("小明", 20001) == "小明（QQ 20001）")
    check("没号时退回昵称", speaker_label("小明", 0) == "小明")

    roster_history = GroupHistory(10)
    roster_history.add(111, "小明", "在吗", uid=20001)
    roster_history.add(111, "小明", "吃了吗", uid=20001)
    roster_history.add(111, "小明", "我去", uid=20002)
    check("同昵称不同 QQ 都保留", len(roster_history.members(111)) == 2)
    check(
        "群聊背景带 QQ",
        "（QQ 20001）" in roster_history.render(111)
        and "（QQ 20002）" in roster_history.render(111),
    )
    roster_history.add(111, "小明改名叫大明", "我改名了", uid=20001)
    roster_members = dict(roster_history.members(111))
    check(
        "同一个人换昵称不重复",
        len(roster_members) == 2 and roster_members[20001] == "小明改名叫大明",
    )

    who_db = data_dir / f"smoke20a-{tag}.db"
    who_memory = MemoryStore(who_db)
    await who_memory.init()
    who_llm = StubLLM()
    who_service = ChatService(
        make_settings(who_db),
        llm=who_llm,
        sender=StubSender(),
        memory=who_memory,
        persona="你是大肥鱼。",
    )
    await who_service.handle(
        parse_incoming(
            group_event("一号报到", mid=1001, at=True, user=20001, nickname="小明"), 999
        )
    )
    await who_service.handle(
        parse_incoming(
            group_event("二号报到", mid=1002, at=True, user=20002, nickname="小明"), 999
        )
    )
    who_ctx = "\n".join(
        message["content"] for message in who_llm.chat_calls[-1]
    )
    check(
        "上下文能区分两个同名的人",
        "QQ 20001" in who_ctx and "QQ 20002" in who_ctx,
    )
    check("花名册注入本群成员", "本群最近说话的人" in who_ctx)
    stored_rows = await who_memory.recent_messages("group:123456", 10)
    stored_users = [row for row in stored_rows if row["role"] != "assistant"]
    check(
        "会话历史记住 QQ 号",
        bool(stored_users) and all("（QQ " in str(row["name"]) for row in stored_users),
    )
    await who_service.shutdown()
    await who_memory.close()

    priv_db = data_dir / f"smoke20b-{tag}.db"
    priv_memory = MemoryStore(priv_db)
    await priv_memory.init()
    priv_llm = StubLLM()
    priv_service = ChatService(
        make_settings(priv_db),
        llm=priv_llm,
        sender=StubSender(),
        memory=priv_memory,
        persona="你是大肥鱼。",
    )
    await priv_service.handle(
        parse_incoming(private_event("在吗", mid=1003, user=30001), 999)
    )
    check(
        "私聊也带 QQ 身份",
        "（QQ 30001）" in priv_llm.chat_calls[-1][-1]["content"],
    )
    await priv_service.shutdown()
    await priv_memory.close()

    print("[21] 图片识别兜底（URL 缺失也能看）")
    local_png = ROOT / "data" / "self" / "01-normal.png"
    check("本地测试图存在", local_png.is_file())

    local_db = data_dir / f"smoke21a-{tag}.db"
    local_memory = MemoryStore(local_db)
    await local_memory.init()
    local_llm = StubLLM()
    local_vision = StubVision("一只戴白色头饰的蓝发女孩")
    local_service = ChatService(
        make_settings(local_db),
        llm=local_llm,
        sender=StubSender(),
        memory=local_memory,
        vision=local_vision,
        persona="你是大肥鱼。",
    )
    local_event = group_event("图里是什么", mid=1100, at=True)
    local_event["message"] = [
        {"type": "at", "data": {"qq": "999"}},
        {"type": "text", "data": {"text": "图里是什么"}},
        {"type": "image", "data": {"file": str(local_png)}},
    ]
    local_incoming = parse_incoming(local_event, 999)
    check(
        "只有本地路径也能解析出图片",
        local_incoming is not None
        and not local_incoming.images
        and bool(local_incoming.image_files),
    )
    await local_service.handle(local_incoming)
    check("本地图片走 describe_bytes", bool(local_vision.byte_calls))
    local_ctx = "\n".join(message["content"] for message in local_llm.chat_calls[-1])
    check("本地识别结果进入上下文", "蓝发女孩" in local_ctx)
    local_rows = await local_memory.recent_messages("group:123456", 2)
    check(
        "图片描述写进记忆",
        any("蓝发女孩" in (row.get("content") or "") for row in local_rows),
    )
    await local_service.shutdown()
    await local_memory.close()

    rec_db = data_dir / f"smoke21b-{tag}.db"
    rec_memory = MemoryStore(rec_db)
    await rec_memory.init()
    rec_llm = StubLLM()
    rec_vision = StubVision("一张写字的图")
    rec_sender = StubSender()
    rec_service = ChatService(
        make_settings(rec_db),
        llm=rec_llm,
        sender=rec_sender,
        memory=rec_memory,
        vision=rec_vision,
        persona="你是大肥鱼。",
    )
    rec_event = group_event("这张图写了什么", mid=1101, at=True)
    rec_event["message"] = [
        {"type": "at", "data": {"qq": "999"}},
        {"type": "text", "data": {"text": "这张图写了什么"}},
        {"type": "image", "data": {"file": "abc123.image"}},
    ]
    rec_incoming = parse_incoming(rec_event, 999)
    await rec_service.handle(rec_incoming)
    check("URL 缺失时问 NapCat 补回", bool(rec_sender.image_fetches))
    check("补回 URL 后能识别", bool(rec_vision.calls))
    await rec_service.shutdown()
    await rec_memory.close()

    print("[22] 防刷屏与防复读")
    guard = RepeatGuard(limit=3, window=60)
    check("复读第 1 次不拦", not guard.should_skip("g:1", 1, "在吗", now=100))
    check("复读第 2 次不拦", not guard.should_skip("g:1", 1, "在吗", now=110))
    check("复读第 3 次拦下", guard.should_skip("g:1", 1, "在吗", now=120))
    check("换一句话不拦", not guard.should_skip("g:1", 1, "吃了吗", now=130))
    check("换个人不拦", not guard.should_skip("g:1", 2, "在吗", now=140))
    check(
        "标点差异也算同一句",
        guard.should_skip("g:1", 1, "在吗！！！", now=150),
    )
    check("窗口过期后恢复", not guard.should_skip("g:1", 1, "在吗", now=500))

    spam_db = data_dir / f"smoke22a-{tag}.db"
    spam_settings = make_settings(spam_db)
    spam_settings.chat.anti_spam.enabled = True
    spam_settings.chat.anti_spam.repeat_limit = 3
    spam_settings.chat.anti_spam.repeat_window = 60
    spam_settings.chat.anti_spam.dedupe_replies = False
    spam_memory = MemoryStore(spam_db)
    await spam_memory.init()
    spam_sender = StubSender()
    spam_service = ChatService(
        spam_settings,
        llm=StubLLM(),
        sender=spam_sender,
        memory=spam_memory,
        persona="你是大肥鱼。",
    )
    for index in range(4):
        await spam_service.handle(
            parse_incoming(
                group_event("在吗", mid=1200 + index, at=True, user=20001), 999
            )
        )
    check("连续复读第 3 次起不回复", len(spam_sender.sent) == 2)
    check("刷屏计数", spam_service.stats["suppressed"] >= 1)
    await spam_service.handle(
        parse_incoming(group_event("在吗", mid=1210, at=True, user=20002), 999)
    )
    check("换个人不受影响", len(spam_sender.sent) == 3)
    await spam_service.shutdown()
    await spam_memory.close()

    dedupe_db = data_dir / f"smoke22b-{tag}.db"
    dedupe_settings = make_settings(dedupe_db)
    dedupe_settings.chat.anti_spam.enabled = True
    dedupe_settings.chat.anti_spam.repeat_limit = 0
    dedupe_settings.chat.anti_spam.dedupe_replies = True
    dedupe_memory = MemoryStore(dedupe_db)
    await dedupe_memory.init()
    dedupe_sender = StubSender()
    dedupe_service = ChatService(
        dedupe_settings,
        llm=StubLLM(),
        sender=dedupe_sender,
        memory=dedupe_memory,
        persona="你是大肥鱼。",
    )
    await dedupe_service.handle(
        parse_incoming(group_event("在吗", mid=1220, at=True, user=20001), 999)
    )
    await dedupe_service.handle(
        parse_incoming(group_event("吃了吗", mid=1221, at=True, user=20001), 999)
    )
    check("自己回复重复时不发", len(dedupe_sender.sent) == 1)
    check("复读计数", dedupe_service.stats["suppressed"] >= 1)
    await dedupe_service.shutdown()
    await dedupe_memory.close()

    print("[23] 戳一戳自动回复")

    def poke_event(*, user: int = 20001, target: int = 999, group: int | None = 123456,
                   notice_type: str = "notify", sub_type: str = "poke") -> dict:
        event = {
            "post_type": "notice",
            "notice_type": notice_type,
            "sub_type": sub_type,
            "time": 1700000000,
            "self_id": 999,
            "user_id": user,
            "target_id": target,
            "sender": {"user_id": user, "nickname": "小明", "card": "小明"},
        }
        if group is not None:
            event["group_id"] = group
        return event

    poke = parse_poke(poke_event(), 999)
    check("戳自己会触发", poke is not None and poke.text == "[戳一戳]")
    check("戳别人不触发", parse_poke(poke_event(target=20002), 999) is None)
    check(
        "非戳戳通知不触发",
        parse_poke(poke_event(notice_type="notify", sub_type="honor"), 999) is None,
    )
    check("普通消息不是戳戳", parse_poke(group_event("在吗"), 999) is None)

    poke_db = data_dir / f"smoke23a-{tag}.db"
    poke_memory = MemoryStore(poke_db)
    await poke_memory.init()
    poke_sender = StubSender()
    poke_service = ChatService(
        make_settings(poke_db),
        llm=StubLLM(),
        sender=poke_sender,
        memory=poke_memory,
        persona="你是大肥鱼。",
    )
    for index in range(3):
        await poke_service.handle(parse_poke(poke_event(user=20001), 999))
    poke_replies = [item["text"] for item in poke_sender.sent]
    check("戳戳会回复", len(poke_replies) == 3)
    check("戳戳回复不重样", len(set(poke_replies[:2])) == 2)
    check(
        "第 3 次变强攻击",
        poke_replies[2] in _POKE_ANGRY_LINES,
        poke_replies[2],
    )
    check("戳戳计数", poke_service.stats["pokes"] == 3)

    await poke_service.handle(parse_poke(poke_event(user=20002), 999))
    check(
        "换个人戳还是普通回复",
        poke_sender.sent[-1]["text"] in _POKE_LINES,
        poke_sender.sent[-1]["text"],
    )

    private_poke = parse_poke(poke_event(user=30001, group=None), 999)
    await poke_service.handle(private_poke)
    check(
        "私聊戳也会回",
        poke_sender.sent[-1]["kind"] == "private" and bool(poke_sender.sent[-1]["text"]),
    )

    # 窗口过期后重新从普通台词开始
    poke_settings = make_settings(data_dir / f"smoke23b-{tag}.db")
    poke_settings.chat.poke.window_seconds = 0.01
    expire_db = data_dir / f"smoke23b-{tag}.db"
    expire_memory = MemoryStore(expire_db)
    await expire_memory.init()
    expire_sender = StubSender()
    expire_service = ChatService(
        poke_settings,
        llm=StubLLM(),
        sender=expire_sender,
        memory=expire_memory,
        persona="你是大肥鱼。",
    )
    await expire_service.handle(parse_poke(poke_event(user=20001), 999))
    await asyncio.sleep(0.05)
    await expire_service.handle(parse_poke(poke_event(user=20001), 999))
    check(
        "窗口过期后不累计",
        expire_sender.sent[-1]["text"] in _POKE_LINES,
        expire_sender.sent[-1]["text"],
    )
    await expire_service.shutdown()
    await expire_memory.close()
    await poke_service.shutdown()
    await poke_memory.close()

    print("[24] 记忆治理（笔记合并成画像）")
    check(
        "记忆提示词禁止记一次性事件",
        "不要记" in MEMORY_SYSTEM and "本次聊天" in MEMORY_SYSTEM,
    )
    check("画像提示词带条数限制", "{limit}" in DISTILL_SYSTEM)

    distill_db = data_dir / f"smoke24a-{tag}.db"
    distill_memory = MemoryStore(distill_db)
    await distill_memory.init()
    for index in range(12):
        await distill_memory.add_note(
            "group:123456", f"零散笔记{index}", source="自动"
        )
    check("笔记已累积", await distill_memory.count_notes("group:123456") == 12)
    await distill_memory.replace_notes(
        "group:123456",
        [("小明喜欢猫（画像）", "画像"), ("本群周五有活动（画像）", "画像")],
    )
    check("合并后只剩画像", await distill_memory.count_notes("group:123456") == 2)
    replaced = await distill_memory.recent_note_entries("group:123456", 5)
    check("画像来源被标记", all(entry["source"] == "画像" for entry in replaced))
    await distill_memory.close()

    distill_settings = make_settings(data_dir / f"smoke24b-{tag}.db")
    distill_settings.chat.memory.curate_every = 1
    distill_settings.chat.memory.distill_every = 5
    distill_settings.chat.memory.distilled_limit = 2
    distill_memory2 = MemoryStore(data_dir / f"smoke24b-{tag}.db")
    await distill_memory2.init()
    distill_llm = StubLLM()
    distill_service = ChatService(
        distill_settings,
        llm=distill_llm,
        sender=StubSender(),
        memory=distill_memory2,
        persona="你是大肥鱼。",
    )
    for index in range(6):
        await distill_memory2.add_note(
            "group:123456", f"预置零散笔记{index}", source="自动"
        )
    for index in range(2):
        await distill_service.handle(
            parse_incoming(
                group_event(f"消息{index}", mid=1300 + index, at=True), 999
            )
        )
    await asyncio.sleep(0.8)
    check("触发了笔记合并", bool(distill_llm.distills))
    merged = await distill_memory2.recent_note_entries("group:123456", 20)
    check(
        "合并后笔记变少且有画像",
        0 < len(merged) <= 4 and any(entry["source"] == "画像" for entry in merged),
    )
    await distill_service.shutdown()
    await distill_memory2.close()

    print("[25] 心情系统")
    from mood import MoodBook, MoodState, detect

    check("夸她会开心", detect("你好可爱") > 0)
    check("骂她会不爽", detect("你真笨") < 0)
    check("普通消息不波动", detect("在吗") == 0)

    state = MoodState(energy=55, now=0)
    state.apply(-20, "被骂", now=0)
    check("心情能下降", abs(state.energy - 35) < 1e-6)
    check("低落标签", state.label() == "有点烦")
    state.apply(50, "被夸", now=0)
    check("心情能回升", state.energy == 85)
    check("高兴标签", state.label() == "很开心")
    state.decay(now=120 * 60)
    check("心情会回落", state.energy < 85)
    check("自画像映射开心脸", MoodState(energy=90).portrait_mood() == "heart")
    check("自画像映射生气脸", MoodState(energy=10).portrait_mood() == "angry")

    book = MoodBook(baseline=55)
    book.apply("a", 20, "被夸")
    check("心情按会话独立", book.get("a").energy > book.get("b").energy)

    mood_db = data_dir / f"smoke25-{tag}.db"
    mood_memory = MemoryStore(mood_db)
    await mood_memory.init()
    mood_llm = StubLLM()
    mood_sender = StubSender()
    mood_service = ChatService(
        make_settings(mood_db),
        llm=mood_llm,
        sender=mood_sender,
        memory=mood_memory,
        persona="你是大肥鱼。",
    )
    probe = parse_incoming(group_event("在吗", mid=1400, at=True), 999)
    mood_base = mood_service._mood_energy(probe)
    await mood_service.handle(
        parse_incoming(group_event("你好可爱", mid=1401, at=True), 999)
    )
    after_praise = mood_service._mood_energy(probe)
    check("被夸心情上升", after_praise > mood_base)
    await mood_service.handle(
        parse_incoming(group_event("你真笨", mid=1402, at=True), 999)
    )
    check("被骂心情下降", mood_service._mood_energy(probe) < after_praise)
    mood_ctx = "\n".join(message["content"] for message in mood_llm.chat_calls[-1])
    check("心情注入上下文", "【你现在的心情】" in mood_ctx)

    await mood_service.handle(
        parse_incoming(group_event("/mood", mid=1403, at=True), 999)
    )
    check(
        "/mood 能查心情",
        any("我现在" in item["text"] for item in mood_sender.sent),
    )

    mood_key = mood_service._mood_key(probe)
    mood_service._moods.get(mood_key).energy = 10
    check("心情差时自画像选生气脸", mood_service._portrait_mood(probe) == "angry")
    await mood_service.handle(parse_poke(poke_event(user=20002), 999))
    check("心情差时戳一下就凶", mood_sender.sent[-1]["text"] in _POKE_ANGRY_LINES)
    await mood_service.shutdown()
    await mood_memory.close()

    print("[26] /doctor 自检")
    doctor_db = data_dir / f"smoke26-{tag}.db"
    doctor_memory = MemoryStore(doctor_db)
    await doctor_memory.init()
    doctor_sender = StubSender()
    doctor_service = ChatService(
        make_settings(doctor_db),
        llm=StubLLM(),
        sender=doctor_sender,
        memory=doctor_memory,
        persona="你是大肥鱼。",
    )
    await doctor_service.handle(
        parse_incoming(group_event("/doctor", mid=1500, at=True), 999)
    )
    doctor_report = "\n".join(item["text"] for item in doctor_sender.sent)
    check("自检有报告", "自检报告" in doctor_report)
    check("自检含协议端", "协议端" in doctor_report)
    check("自检含记忆统计", "记忆" in doctor_report and "笔记" in doctor_report)
    check("自检含图库", "图库" in doctor_report)
    check("自检含心情", "心情" in doctor_report)
    await doctor_service.shutdown()
    await doctor_memory.close()

    print("[27] 定时提醒")
    from reminder import ReminderScheduler, parse_remind_time, parse_reminder

    base_now = datetime(2026, 10, 25, 10, 0, 0).timestamp()
    tomorrow_8 = datetime(2026, 10, 26, 8, 0, 0).timestamp()
    tonight_2130 = datetime(2026, 10, 25, 21, 30, 0).timestamp()
    check(
        "30分钟后",
        parse_remind_time("30分钟后提醒我喝水", base_now) == base_now + 1800,
    )
    check(
        "2小时后",
        parse_remind_time("2小时后叫我", base_now) == base_now + 7200,
    )
    check(
        "半小时后",
        parse_remind_time("半小时后提醒我", base_now) == base_now + 1800,
    )
    check(
        "明天早上8点",
        abs(parse_remind_time("明天早上8点交报告", base_now) - tomorrow_8) < 1,
    )
    check(
        "今晚9点半",
        abs(parse_remind_time("今晚9点半提醒我", base_now) - tonight_2130) < 1,
    )
    check(
        "过点顺延到明天",
        abs(parse_remind_time("8点提醒我", base_now) - tomorrow_8) < 1,
    )
    check(
        "只说后天也给个时间",
        parse_remind_time("后天", base_now) == datetime(2026, 10, 27, 9, 0).timestamp(),
    )
    check("解析不了返回 None", parse_remind_time("随便说说", base_now) is None)
    split = parse_reminder("明天早上8点 交报告", base_now)
    check("时间与内容分离", split is not None and split[1] == "交报告")

    rem_db = data_dir / f"smoke27a-{tag}.db"
    rem_memory = MemoryStore(rem_db)
    await rem_memory.init()
    rem_id = await rem_memory.add_reminder(
        "group:123456", 20001, "交报告", base_now + 60
    )
    check("提醒已保存", rem_id > 0)
    check("未到期不触发", await rem_memory.due_reminders(base_now) == [])
    due_items = await rem_memory.due_reminders(base_now + 61)
    check("到期能查出来", len(due_items) == 1 and due_items[0]["content"] == "交报告")
    await rem_memory.mark_reminder_done(rem_id)
    check("完成后不再触发", await rem_memory.due_reminders(base_now + 120) == [])
    check("列表为空", await rem_memory.list_reminders("group:123456") == [])

    tool_ctx = ToolContext(session_id="group:123456", memory=rem_memory, persona_name="大肥鱼")
    tool_reply = await dispatch(
        "add_reminder",
        json.dumps({"when": "30分钟后", "content": "喝水"}),
        tool_ctx,
    )
    check("工具能加提醒", "记下了" in tool_reply and "喝水" in tool_reply)
    listed = await rem_memory.list_reminders("group:123456")
    check("提醒列表可见", len(listed) == 1 and listed[0]["content"] == "喝水")
    await rem_memory.close()

    remind_db = data_dir / f"smoke27b-{tag}.db"
    remind_memory = MemoryStore(remind_db)
    await remind_memory.init()
    remind_sender = StubSender()
    remind_service = ChatService(
        make_settings(remind_db),
        llm=StubLLM(),
        sender=remind_sender,
        memory=remind_memory,
        persona="你是大肥鱼。",
    )
    await remind_service.handle(
        parse_incoming(
            group_event("/remind 明天早上8点 交报告", mid=1600, at=True), 999
        )
    )
    check(
        "命令能记提醒",
        any("记下了" in item["text"] and "交报告" in item["text"] for item in remind_sender.sent),
    )
    await remind_service.handle(
        parse_incoming(group_event("/reminders", mid=1601, at=True), 999)
    )
    check(
        "命令能列提醒",
        any("未完成的提醒" in item["text"] for item in remind_sender.sent),
    )

    scheduler = ReminderScheduler(remind_service, interval=30)
    # 用真实日期算“明天 8 点”，否则过了 10-26 早上这条测试会误判
    real_now = datetime.now()
    sched_8 = datetime(real_now.year, real_now.month, real_now.day, 8, 0, 0)
    if real_now >= sched_8:
        sched_8 += timedelta(days=1)
    sent_ids = await scheduler.tick_once(now=sched_8.timestamp() + 1)
    check("调度器到期发送", bool(sent_ids))
    check(
        "提醒内容已发出",
        any("到点啦" in item["text"] and "交报告" in item["text"] for item in remind_sender.sent),
    )
    check(
        "发过的提醒不再重复",
        await scheduler.tick_once(now=sched_8.timestamp() + 100) == [],
    )
    await remind_service.shutdown()
    await remind_memory.close()

    print("[28] 每日群报")
    from daily_report import REPORT_SYSTEM, DailyReportScheduler

    check("群报提示词存在", "群报" in REPORT_SYSTEM and "不编造" in REPORT_SYSTEM)

    report_db = data_dir / f"smoke28-{tag}.db"
    report_memory = MemoryStore(report_db)
    await report_memory.init()
    day_start = datetime(2026, 10, 25, 0, 0, 0).timestamp()
    report_now = datetime(2026, 10, 25, 22, 31, 0).timestamp()
    for index in range(10):
        await report_memory.add_message(
            "group:123456", "user", "小明", f"今天第{index}条聊天"
        )
    day_rows = await report_memory.messages_since("group:123456", day_start, 200)
    check("能查当天消息", len(day_rows) == 10)

    class ReportLLM(StubLLM):
        async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
            self.chat_calls.append(messages)
            return "- 小明在群里晒了新猫\n- 大家在商量周五聚餐"

    report_settings = make_settings(report_db)
    report_settings.chat.daily_report.enabled = True
    report_settings.chat.daily_report.hour = 22
    report_settings.chat.daily_report.minute = 30
    report_settings.chat.daily_report.min_messages = 5
    report_settings.chat.daily_report.groups = [123456]
    report_sender = StubSender()
    report_service = ChatService(
        report_settings,
        llm=ReportLLM(),
        sender=report_sender,
        memory=report_memory,
        persona="你是大肥鱼。",
    )
    check("能识别当天活跃群", 123456 in await report_service.active_group_sessions(now=report_now))
    check(
        "消息太少不发群报",
        not await report_service.send_daily_report(456789, now=report_now),
    )
    check(
        "群报能生成发送",
        await report_service.send_daily_report(123456, now=report_now),
    )
    check(
        "群报内容来自真实记录",
        any("晒了新猫" in item["text"] for item in report_sender.sent),
    )
    check(
        "群报带标题",
        any("今日群报" in item["text"] for item in report_sender.sent),
    )

    scheduler = DailyReportScheduler(report_service, report_settings.chat.daily_report)
    check(
        "没到点不发",
        await scheduler.tick_once(now=datetime(2026, 10, 25, 22, 0).timestamp()) == [],
    )
    check(
        "到点会发",
        await scheduler.tick_once(now=report_now) == [123456],
    )
    check(
        "同一天不重复发",
        await scheduler.tick_once(now=report_now + 60) == [],
    )
    await report_service.shutdown()
    await report_memory.close()

    print("[29] 亲密度成长")
    aff_db = data_dir / f"smoke29a-{tag}.db"
    aff_memory = MemoryStore(aff_db)
    await aff_memory.init()
    check("初始亲密度 0", await aff_memory.get_affinity(20001, 123456) == 0)
    check("互动后 +1", await aff_memory.bump_affinity(20001, 123456) == 1)
    check("再互动 +1", await aff_memory.bump_affinity(20001, 123456) == 2)
    check("不同群独立", await aff_memory.get_affinity(20001, 999) == 0)
    check("不同人独立", await aff_memory.get_affinity(20002, 123456) == 0)
    for _ in range(12):
        await aff_memory.bump_affinity(20002, 123456)
    aff_top = await aff_memory.top_affinity(123456, 5)
    check(
        "排行榜按分数排序",
        aff_top[0]["user_id"] == 20002 and aff_top[0]["score"] == 12,
    )
    await aff_memory.close()

    check("等级-陌生鱼", _affinity_level(0) == "陌生鱼")
    check("等级-熟鱼片", _affinity_level(10) == "熟鱼片")
    check("等级-老鱼片", _affinity_level(40) == "老鱼片")
    check("等级-饲养员", _affinity_level(100) == "饲养员")

    aff2_db = data_dir / f"smoke29b-{tag}.db"
    aff_memory2 = MemoryStore(aff2_db)
    await aff_memory2.init()
    aff_llm = StubLLM()
    aff_sender = StubSender()
    aff_service = ChatService(
        make_settings(aff2_db),
        llm=aff_llm,
        sender=aff_sender,
        memory=aff_memory2,
        persona="你是大肥鱼。",
    )
    await aff_service.handle(
        parse_incoming(group_event("在吗", mid=1700, at=True, user=20001), 999)
    )
    check(
        "回复后亲密度上升",
        await aff_memory2.get_affinity(20001, 123456) > 0,
    )
    await aff_memory2.bump_affinity(20001, 123456, 5)
    await aff_service.handle(
        parse_incoming(group_event("吃了吗", mid=1702, at=True, user=20001), 999)
    )
    aff_ctx = "\n".join(message["content"] for message in aff_llm.chat_calls[-1])
    check("亲密度注入上下文", "亲密度" in aff_ctx)
    await aff_service.handle(
        parse_incoming(group_event("/affinity", mid=1701, at=True, user=20001), 999)
    )
    check(
        "/affinity 能查",
        any("亲密度" in item["text"] for item in aff_sender.sent),
    )
    await aff_service.shutdown()
    await aff_memory2.close()

    print("[30] 成绩单与热点词")
    from text_stats import build_word_cloud

    sample_texts = [
        "今天天气不错我们去爬山吧",
        "爬山真的好累但是很爽",
        "明天继续爬山吗",
        "hello python world",
        "python 很好玩",
    ]
    cloud = build_word_cloud(sample_texts, top=10, min_count=1)
    cloud_words = {item["word"]: item["count"] for item in cloud}
    check("词云有中文词", "爬山" in cloud_words or "天气" in cloud_words)
    check("词云有英文词", "python" in cloud_words)
    check("词云过滤停用词", "我们" not in cloud_words and "什么" not in cloud_words)
    check(
        "词云按次数排序",
        all(
            cloud[index]["count"] >= cloud[index + 1]["count"]
            for index in range(len(cloud) - 1)
        ),
    )

    dash_db = data_dir / f"smoke30a-{tag}.db"
    dash_memory = MemoryStore(dash_db)
    await dash_memory.init()
    dash_now = time.time()
    for index in range(5):
        await dash_memory.add_message(
            "group:123456", "user", "小明", f"今天去爬山真的好玩{index}"
        )
    await dash_memory.add_message(
        "group:123456", "assistant", "大肥鱼", "爬山记得带水"
    )
    await dash_memory.bump_affinity(20001, 123456, 12)
    await dash_memory.add_reminder("group:123456", 20001, "喝水", dash_now + 60)
    dash_texts = await dash_memory.recent_texts(dash_now - 3600, 100)
    check("词云只取群友消息", len(dash_texts) == 5)
    check("未完成提醒计数", await dash_memory.count_pending_reminders() == 1)

    class DashLLM(StubLLM):
        async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
            self.chat_calls.append(messages)
            return "- 大家在聊爬山\n- 提醒带水"

    dash_settings = make_settings(dash_db)
    dash_settings.chat.daily_report.enabled = True
    dash_settings.chat.daily_report.min_messages = 3
    dash_settings.chat.daily_report.groups = [123456]
    dash_sender = StubSender()
    dash_service = ChatService(
        dash_settings,
        llm=DashLLM(),
        sender=dash_sender,
        memory=dash_memory,
        persona="你是大肥鱼。",
    )
    dashboard = await dash_service.dashboard_data()
    check("成绩单有记忆", "messages" in dashboard["memory"])
    check("成绩单有亲密度", "123456" in dashboard["affinity"])
    check("成绩单有词云", bool(dashboard["word_cloud"]))
    check("成绩单有图库", "memes" in dashboard["gallery"])
    check("成绩单有提醒数", dashboard["reminders"]["pending"] == 1)
    check("成绩单有用量", isinstance(dashboard["usage_days"], list))

    dash_settings.chat.daily_report.enabled = False
    check(
        "/report 强制生成群报",
        await dash_service.send_daily_report(123456, force=True),
    )
    before_report = len(dash_sender.sent)
    await dash_service.handle(
        parse_incoming(group_event("/report", mid=1800, at=True), 999)
    )
    check("/report 命令可用", len(dash_sender.sent) == before_report + 1)
    await dash_service.shutdown()
    await dash_memory.close()

    # 成绩单页面与接口
    import httpx as _httpx
    from webui import create_app

    dash_web_db = data_dir / f"smoke30b-{tag}.db"
    dash_web_memory = MemoryStore(dash_web_db)
    await dash_web_memory.init()
    dash_web_settings = make_settings(dash_web_db)
    dash_web_settings.web.token = "smoke-token"
    dash_web_service = ChatService(
        dash_web_settings,
        llm=StubLLM(),
        sender=StubSender(),
        memory=dash_web_memory,
        persona="你是大肥鱼。",
    )
    dash_app = create_app(dash_web_service, dash_web_settings, token="smoke-token")
    async with _httpx.AsyncClient(
        transport=_httpx.ASGITransport(app=dash_app), base_url="http://test"
    ) as dash_client:
        page_resp = await dash_client.get("/dashboard")
        api_resp = await dash_client.get(
            "/api/dashboard", headers={"Authorization": "Bearer smoke-token"}
        )
    check(
        "成绩单页面可打开",
        page_resp.status_code == 200 and "成绩单" in page_resp.text,
    )
    check(
        "成绩单接口可用",
        api_resp.status_code == 200 and "word_cloud" in api_resp.json(),
    )
    await dash_web_service.shutdown()
    await dash_web_memory.close()

    print("[31] 成绩单图片")
    from card import render_card

    fake_data = {
        "status": {
            "uptime_seconds": 3725,
            "model": "deepseek-chat",
            "stats": {"received": 128, "replied": 96, "pokes": 7, "errors": 0},
            "mood": {"states": {"group:123456": {"energy": 64, "label": "还不错"}}},
            "budget": {"today": {"123456": 0.05}, "daily_cny_per_group": 0.3},
            "self_portrait": {"count": 7},
        },
        "memory": {
            "messages": 70,
            "archive": 100,
            "notes": 20,
            "portraits": 3,
            "todos": 1,
            "sessions": 2,
        },
        "gallery": {"memes": 10, "bytes_mb": 2.5, "portraits": 7},
        "affinity": {"123456": [{"name": "小明", "score": 23, "level": "熟鱼片"}]},
        "word_cloud": [{"word": "爬山", "count": 9}, {"word": "聚餐", "count": 5}],
        "reminders": {"pending": 2},
    }
    started = time.perf_counter()
    card_bytes = render_card(fake_data)
    card_cost = time.perf_counter() - started
    check(
        "成绩单图片生成",
        len(card_bytes) > 5000 and card_bytes[:8] == b"\x89PNG\r\n\x1a\n",
    )
    check("渲染够快", card_cost < 3.0, f"{card_cost:.2f}s")
    from io import BytesIO as _BytesIO
    from PIL import Image as _Image

    with _Image.open(_BytesIO(card_bytes)) as card_image:
        check("图片宽度正确", card_image.width == 860)
        check("高度自适应", 820 <= card_image.height <= 1500)

    card_db = data_dir / f"smoke31-{tag}.db"
    card_memory = MemoryStore(card_db)
    await card_memory.init()
    card_sender = StubSender()
    card_sender.images_enabled = True
    card_service = ChatService(
        make_settings(card_db),
        llm=StubLLM(),
        sender=card_sender,
        memory=card_memory,
        persona="你是大肥鱼。",
    )
    await card_service.handle(
        parse_incoming(group_event("/card", mid=1900, at=True), 999)
    )
    check("命令发成绩单图片", len(card_sender.images) == 1)
    check(
        "附一句说明",
        any("成绩单" in item["text"] for item in card_sender.sent),
    )
    await card_service.handle(
        parse_incoming(group_event("/成绩单", mid=1901, at=True), 999)
    )
    check("冷却内复用仍发图", len(card_sender.images) == 2)
    check("成绩单计数", card_service.stats["cards"] == 2)
    await card_service.shutdown()
    await card_memory.close()

    print("[32] 活跃度曲线")
    act_db = data_dir / f"smoke32-{tag}.db"
    act_memory = MemoryStore(act_db)
    await act_memory.init()
    act_now = time.time()
    today_key = time.strftime("%Y-%m-%d", time.localtime(act_now))
    yesterday_key = time.strftime("%Y-%m-%d", time.localtime(act_now - 86400))
    for _ in range(3):
        await act_memory._write(
            "INSERT INTO messages(session_id, role, name, content, ts) VALUES(?,?,?,?,?)",
            ("group:123456", "user", "小明", "今天的消息", act_now),
        )
    for _ in range(2):
        await act_memory._write(
            "INSERT INTO messages(session_id, role, name, content, ts) VALUES(?,?,?,?,?)",
            ("group:123456", "user", "小明", "昨天的消息", act_now - 86400),
        )
    act_days = await act_memory.daily_message_counts(7)
    check("活跃数据 7 天", len(act_days) == 7)
    check(
        "今天计数",
        act_days[-1]["day"] == today_key and act_days[-1]["count"] == 3,
    )
    check(
        "昨天计数",
        act_days[-2]["day"] == yesterday_key and act_days[-2]["count"] == 2,
    )
    check("空天补 0", sum(item["count"] for item in act_days[:5]) == 0)

    act_service = ChatService(
        make_settings(act_db),
        llm=StubLLM(),
        sender=StubSender(),
        memory=act_memory,
        persona="你是大肥鱼。",
    )
    act_data = await act_service.dashboard_data()
    check("成绩单有活跃数据", len(act_data["activity_days"]) == 7)
    check("成绩单图片能带活跃图", len(render_card(act_data)) > 5000)
    await act_service.shutdown()
    await act_memory.close()

    dashboard_html = (
        Path(__file__).resolve().parent.parent / "src" / "dashboard.html"
    ).read_text(encoding="utf-8")
    check("成绩单页面有活跃卡片", "近 7 天活跃" in dashboard_html)

    print("[33] 群报图片")
    from card import render_report_card

    report_started = time.perf_counter()
    report_bytes = render_report_card(
        ["大家在聊爬山", "约了周五聚餐"], stats={"messages": 164}
    )
    report_cost = time.perf_counter() - report_started
    check(
        "群报图片生成",
        report_bytes[:8] == b"\x89PNG\r\n\x1a\n" and len(report_bytes) > 3000,
    )
    check("群报渲染快", report_cost < 2.0, f"{report_cost:.2f}s")
    with _Image.open(_BytesIO(report_bytes)) as report_image:
        check(
            "群报图片尺寸",
            report_image.width == 860 and report_image.height >= 480,
        )

    rep_db = data_dir / f"smoke33-{tag}.db"
    rep_memory = MemoryStore(rep_db)
    await rep_memory.init()
    rep_now = time.time()
    for index in range(6):
        await rep_memory.add_message(
            "group:123456", "user", "小明", f"今天第{index}条聊天"
        )

    class RepLLM(StubLLM):
        async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
            self.chat_calls.append(messages)
            return "- 大家在聊爬山\n- 约了周五聚餐"

    rep_settings = make_settings(rep_db)
    rep_settings.chat.daily_report.enabled = True
    rep_settings.chat.daily_report.min_messages = 3
    rep_settings.chat.daily_report.as_image = True
    rep_sender = StubSender()
    rep_sender.images_enabled = True
    rep_service = ChatService(
        rep_settings,
        llm=RepLLM(),
        sender=rep_sender,
        memory=rep_memory,
        persona="你是大肥鱼。",
    )
    check(
        "群报发图片",
        await rep_service.send_daily_report(123456, now=rep_now),
    )
    check("群报图片已发出", len(rep_sender.images) == 1)
    check(
        "图片模式不重复发文字",
        not any("今日群报" in item["text"] for item in rep_sender.sent),
    )
    rep_settings.chat.daily_report.as_image = False
    check(
        "关闭图片走文字",
        await rep_service.send_daily_report(123456, now=rep_now + 60),
    )
    check(
        "文字群报已发出",
        any("今日群报" in item["text"] for item in rep_sender.sent),
    )
    await rep_service.shutdown()
    await rep_memory.close()

    print("[34] 取消提醒与群报活跃图")
    fog_db = data_dir / f"smoke34-{tag}.db"
    fog_memory = MemoryStore(fog_db)
    await fog_memory.init()
    fog_id = await fog_memory.add_reminder(
        "group:123456", 20001, "交报告", time.time() + 600
    )
    check("提醒已加待取消", await fog_memory.count_pending_reminders() == 1)
    check(
        "能取消本会话提醒",
        await fog_memory.remove_reminder(fog_id, "group:123456"),
    )
    check("取消后计数归零", await fog_memory.count_pending_reminders() == 0)
    other_id = await fog_memory.add_reminder(
        "group:123456", 20001, "别的", time.time() + 600
    )
    check(
        "不能取消别的会话的",
        not await fog_memory.remove_reminder(other_id, "group:999"),
    )
    await fog_memory.remove_reminder(other_id, "group:123456")

    fog_sender = StubSender()
    fog_service = ChatService(
        make_settings(fog_db),
        llm=StubLLM(),
        sender=fog_sender,
        memory=fog_memory,
        persona="你是大肥鱼。",
    )
    await fog_service.handle(
        parse_incoming(
            group_event("/remind 明天8点 交报告", mid=2000, at=True), 999
        )
    )
    fog_listed = await fog_memory.list_reminders("group:123456")
    check("命令加了提醒", len(fog_listed) == 1)
    await fog_service.handle(
        parse_incoming(
            group_event(f"/forget #{fog_listed[0]['id']}", mid=2001, at=True), 999
        )
    )
    check(
        "命令取消提醒",
        any("已取消" in item["text"] for item in fog_sender.sent),
    )
    check("取消后列表空", await fog_memory.list_reminders("group:123456") == [])
    await fog_service.handle(
        parse_incoming(group_event("/forget 999", mid=2002, at=True), 999)
    )
    check(
        "取消不存在有提示",
        any("没找到" in item["text"] for item in fog_sender.sent),
    )
    await fog_service.shutdown()
    await fog_memory.close()

    act_days = [
        {"day": f"2026-10-{19 + index}", "count": index * 10}
        for index in range(7)
    ]
    long_lines = [
        "测试群报第一行内容很长" * 3,
        "第二行内容再补充一些细节" * 3,
        "第三行内容也要足够长" * 3,
    ]
    report_active = render_report_card(
        long_lines, stats={"messages": 10}, activity=act_days
    )
    check("群报图片带活跃图", len(report_active) > 3000)
    report_plain = render_report_card(long_lines, stats={"messages": 10})
    with _Image.open(_BytesIO(report_active)) as act_image, _Image.open(
        _BytesIO(report_plain)
    ) as plain_image:
        check("活跃图让图片变高", act_image.height > plain_image.height)

    from admin import HELP_TEXT

    check(
        "帮助里有群友命令",
        all(
            cmd in HELP_TEXT
            for cmd in ("/mood", "/card", "/remind", "/forget", "/affinity")
        ),
    )

    print("[35] WebUI 提醒管理")
    web2_db = data_dir / f"smoke35-{tag}.db"
    web2_memory = MemoryStore(web2_db)
    await web2_memory.init()
    web2_rid = await web2_memory.add_reminder(
        "group:123456", 20001, "交报告", time.time() + 600
    )
    web2_items = await web2_memory.all_pending_reminders(10)
    check(
        "所有提醒可列出",
        len(web2_items) == 1 and web2_items[0]["content"] == "交报告",
    )
    check("管理员可取消", await web2_memory.cancel_reminder(web2_rid))
    check("取消后列表空", await web2_memory.all_pending_reminders(10) == [])
    web2_rid2 = await web2_memory.add_reminder(
        "group:123456", 20001, "喝水", time.time() + 600
    )
    check("取消不存在返回 False", not await web2_memory.cancel_reminder(99999))

    web2_settings = make_settings(web2_db)
    web2_settings.web.token = "smoke-token"
    web2_service = ChatService(
        web2_settings,
        llm=StubLLM(),
        sender=StubSender(),
        memory=web2_memory,
        persona="你是大肥鱼。",
    )
    web2_dash = await web2_service.dashboard_data()
    check(
        "成绩单含提醒明细",
        len(web2_dash["reminders"]["items"]) == 1
        and web2_dash["reminders"]["items"][0]["id"] == web2_rid2,
    )

    web2_app = create_app(web2_service, web2_settings, token="smoke-token")
    web2_auth = {"Authorization": "Bearer smoke-token"}
    async with _httpx.AsyncClient(
        transport=_httpx.ASGITransport(app=web2_app), base_url="http://test"
    ) as web2_client:
        web2_list = await web2_client.get("/api/reminders", headers=web2_auth)
        web2_cancel = await web2_client.post(
            f"/api/reminders/{web2_rid2}/cancel", headers=web2_auth
        )
        web2_after = await web2_client.get("/api/reminders", headers=web2_auth)
    check(
        "接口列出提醒",
        web2_list.status_code == 200 and len(web2_list.json()["items"]) == 1,
    )
    check(
        "接口取消提醒",
        web2_cancel.status_code == 200 and web2_cancel.json()["ok"],
    )
    check("接口取消后为空", web2_after.json()["items"] == [])
    await web2_service.shutdown()
    await web2_memory.close()

    dashboard_html2 = (
        Path(__file__).resolve().parent.parent / "src" / "dashboard.html"
    ).read_text(encoding="utf-8")
    check(
        "页面有取消按钮",
        "cancelReminder" in dashboard_html2 and "reminder-list" in dashboard_html2,
    )

    print("[36] 手动群报与群报热词")
    lines36 = [
        "测试群报内容很长" * 3,
        "第二行内容也长" * 3,
        "第三行内容" * 3,
    ]
    cloud36 = [
        {"word": "爬山", "count": 9},
        {"word": "聚餐", "count": 5},
        {"word": "表情包", "count": 4},
        {"word": "论文", "count": 3},
        {"word": "猫", "count": 2},
        {"word": "周五", "count": 2},
        {"word": "火锅", "count": 2},
    ]
    report_cloud = render_report_card(
        lines36, stats={"messages": 100}, word_cloud=cloud36
    )
    report_no_cloud = render_report_card(lines36, stats={"messages": 100})
    with _Image.open(_BytesIO(report_cloud)) as cloud_img, _Image.open(
        _BytesIO(report_no_cloud)
    ) as plain_img:
        check("群报带热词", cloud_img.height > plain_img.height)

    rep2_db = data_dir / f"smoke36-{tag}.db"
    rep2_memory = MemoryStore(rep2_db)
    await rep2_memory.init()
    for index in range(6):
        await rep2_memory.add_message(
            "group:123456", "user", "小明", f"今天第{index}条聊天"
        )

    class Rep2LLM(StubLLM):
        async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
            self.chat_calls.append(messages)
            return "- 大家聊了爬山\n- 约了周五聚餐"

    rep2_settings = make_settings(rep2_db)
    rep2_settings.chat.daily_report.enabled = False
    rep2_settings.chat.daily_report.min_messages = 3
    rep2_settings.web.token = "smoke-token"
    rep2_sender = StubSender()
    rep2_sender.images_enabled = True
    rep2_service = ChatService(
        rep2_settings,
        llm=Rep2LLM(),
        sender=rep2_sender,
        memory=rep2_memory,
        persona="你是大肥鱼。",
    )
    rep2_app = create_app(rep2_service, rep2_settings, token="smoke-token")
    rep2_headers = {"Authorization": "Bearer smoke-token"}
    async with _httpx.AsyncClient(
        transport=_httpx.ASGITransport(app=rep2_app), base_url="http://test"
    ) as rep2_client:
        rep2_ok = await rep2_client.post(
            "/api/report", headers=rep2_headers, json={"group_id": 123456}
        )
        rep2_bad = await rep2_client.post(
            "/api/report", headers=rep2_headers, json={"group_id": 999}
        )
    check(
        "手动群报成功",
        rep2_ok.status_code == 200 and rep2_ok.json()["ok"],
    )
    check("手动群报发出图片", len(rep2_sender.images) == 1)
    check(
        "消息太少返回失败",
        rep2_bad.status_code == 200 and not rep2_bad.json()["ok"],
    )
    await rep2_service.shutdown()
    await rep2_memory.close()

    dashboard_html3 = (
        Path(__file__).resolve().parent.parent / "src" / "dashboard.html"
    ).read_text(encoding="utf-8")
    check(
        "页面有群报按钮",
        "sendReport" in dashboard_html3 and "report-group" in dashboard_html3,
    )

    print("[37] 主动关心")
    from care import CareScheduler, care_due, extract_care, pick_care_line

    check("识别生病", extract_care("我好像感冒了").kind == "sick")
    check("识别考试", extract_care("明天要考试了").kind == "exam")
    check("识别加班", extract_care("又要加班到十点").kind == "tired")
    check("识别生日", extract_care("今天生日").kind == "birthday")
    check("普通消息不识别", extract_care("今天天气不错") is None)
    check(
        "关心语池非空",
        all(pick_care_line(kind) for kind in ("exam", "sick", "tired", "birthday", "sad")),
    )

    care_base = datetime(2026, 10, 25, 10, 0).timestamp()
    check(
        "生病隔一天关心",
        abs(care_due("sick", care_base) - (care_base + 86400)) < 1,
    )
    check(
        "考试半天后关心",
        abs(care_due("exam", care_base) - (care_base + 43200)) < 1,
    )

    care_db = data_dir / f"smoke37-{tag}.db"
    care_memory = MemoryStore(care_db)
    await care_memory.init()
    care_id = await care_memory.add_care_event(
        "group:123456", 20001, "小明", "sick", "感冒", care_base + 86400
    )
    check("关心事件已记录", care_id > 0)
    check("未到期不关心", await care_memory.due_care_events(care_base) == [])
    care_due_items = await care_memory.due_care_events(care_base + 86401)
    check(
        "到期能查出来",
        len(care_due_items) == 1 and care_due_items[0]["kind"] == "sick",
    )
    check("未发送计数", await care_memory.count_care_pending() == 1)

    care_settings = make_settings(care_db)
    care_settings.chat.care.enabled = True
    care_sender = StubSender()
    care_service = ChatService(
        care_settings,
        llm=StubLLM(),
        sender=care_sender,
        memory=care_memory,
        persona="你是大肥鱼。",
    )
    care_scheduler = CareScheduler(care_service)
    check(
        "夜间不发关心",
        await care_scheduler.tick_once(now=datetime(2026, 10, 26, 3, 0).timestamp())
        == [],
    )
    care_sent_ids = await care_scheduler.tick_once(now=care_base + 90000)
    check("到点主动关心", care_sent_ids == [care_id])
    check(
        "关心内容含昵称",
        any("小明" in item["text"] for item in care_sender.sent),
    )
    check("发完标记已发", await care_memory.count_care_pending() == 0)
    check(
        "同一天不重复关心",
        await care_scheduler.tick_once(now=care_base + 95000) == [],
    )
    await care_service.shutdown()

    care_handle_sender = StubSender()
    care_handle_service = ChatService(
        make_settings(care_db),
        llm=StubLLM(),
        sender=care_handle_sender,
        memory=care_memory,
        persona="你是大肥鱼。",
    )
    await care_handle_service.handle(
        parse_incoming(group_event("我感冒了，好难受", mid=2100, at=True), 999)
    )
    care_recent = await care_memory.recent_care_events(5)
    check("聊天时记录关心事件", any(item["kind"] == "sick" for item in care_recent))
    await care_handle_service.handle(
        parse_incoming(group_event("我还是感冒", mid=2101, at=True), 999)
    )
    care_sick = [
        item for item in await care_memory.recent_care_events(10) if item["kind"] == "sick"
    ]
    check("24 小时内不重复记录", len(care_sick) == 1)
    await care_handle_service.shutdown()
    await care_memory.close()

    print("[38] 关心语润色与群报关心小注")
    from llm import DeepSeekClient

    check("模型有 care_line", hasattr(DeepSeekClient, "care_line"))

    class CareLLM(StubLLM):
        def __init__(self):
            super().__init__()
            self.care_calls = []

        async def care_line(self, name, kind, detail, *, on_usage=None):  # noqa: ANN001
            self.care_calls.append((name, kind, detail))
            return "记得多喝热水，别硬撑，好点了跟我说。"

    care2_db = data_dir / f"smoke38-{tag}.db"
    care2_memory = MemoryStore(care2_db)
    await care2_memory.init()
    care2_base = datetime(2026, 10, 25, 10, 0).timestamp()
    care2_id = await care2_memory.add_care_event(
        "group:123456", 20001, "小明", "sick", "感冒", care2_base + 86400
    )
    care2_settings = make_settings(care2_db)
    care2_settings.chat.care.enabled = True
    care2_llm = CareLLM()
    care2_sender = StubSender()
    care2_service = ChatService(
        care2_settings,
        llm=care2_llm,
        sender=care2_sender,
        memory=care2_memory,
        persona="你是大肥鱼。",
    )
    from care import CARE_LINES, CareScheduler as _CareScheduler

    care2_scheduler = _CareScheduler(care2_service)
    care2_sent = await care2_scheduler.tick_once(now=care2_base + 90000)
    check(
        "关心语来自模型",
        care2_sent == [care2_id]
        and any("多喝热水" in item["text"] for item in care2_sender.sent),
    )
    check(
        "模型收到事件",
        bool(care2_llm.care_calls) and care2_llm.care_calls[0][2] == "感冒",
    )

    class FailCareLLM(StubLLM):
        async def care_line(self, name, kind, detail, *, on_usage=None):  # noqa: ANN001
            raise RuntimeError("boom")

    care2_id2 = await care2_memory.add_care_event(
        "group:123456", 20002, "小红", "sick", "发烧", care2_base + 86400 * 2
    )
    care2_sender2 = StubSender()
    care2_service2 = ChatService(
        care2_settings,
        llm=FailCareLLM(),
        sender=care2_sender2,
        memory=care2_memory,
        persona="你是大肥鱼。",
    )
    care2_scheduler2 = _CareScheduler(care2_service2)
    care2_sent2 = await care2_scheduler2.tick_once(now=care2_base + 200000)
    check(
        "模型失败仍发出关心",
        care2_sent2 == [care2_id2] and bool(care2_sender2.sent),
    )
    check(
        "回退句来自台词池",
        any(
            any(line in item["text"] for line in CARE_LINES["sick"])
            for item in care2_sender2.sent
        ),
    )
    await care2_service.shutdown()
    await care2_service2.shutdown()
    await care2_memory.close()

    note_long = [
        "测试群报第一行内容很长" * 3,
        "第二行内容再补充一些细节" * 3,
        "第三行内容也要足够长" * 3,
    ]
    note_bytes = render_report_card(
        note_long,
        stats={"messages": 1},
        care_note="最近在关心：@小明（身体不舒服）",
    )
    note_plain = render_report_card(note_long, stats={"messages": 1})
    with _Image.open(_BytesIO(note_bytes)) as note_img, _Image.open(
        _BytesIO(note_plain)
    ) as note_plain_img:
        check("群报图片带关心小注", note_img.height > note_plain_img.height)

    print("[39] 周报")
    from weekly import WEEKLY_SYSTEM, WeeklyReportScheduler
    from card import render_weekly_card

    check("周报提示词存在", "本周看点" in WEEKLY_SYSTEM and "不编造" in WEEKLY_SYSTEM)

    wk_db = data_dir / f"smoke39-{tag}.db"
    wk_memory = MemoryStore(wk_db)
    await wk_memory.init()
    wk_now = time.time()
    for index in range(24):
        await wk_memory.add_message(
            "group:123456", "user", "小明", f"本周第{index}条聊天"
        )

    class WkLLM(StubLLM):
        async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
            self.chat_calls.append(messages)
            return "- 本周大家在聊爬山\n- 约好了周末聚餐"

    wk_settings = make_settings(wk_db)
    wk_settings.chat.weekly_report.enabled = True
    wk_settings.chat.weekly_report.groups = [123456]
    wk_settings.chat.weekly_report.min_messages = 10
    wk_sender = StubSender()
    wk_sender.images_enabled = True
    wk_service = ChatService(
        wk_settings,
        llm=WkLLM(),
        sender=wk_sender,
        memory=wk_memory,
        persona="你是大肥鱼。",
    )
    check(
        "消息太少不发周报",
        not await wk_service.send_weekly_report(999, now=wk_now),
    )
    check(
        "周报能生成发送",
        await wk_service.send_weekly_report(123456, now=wk_now),
    )
    check("周报发了图片", len(wk_sender.images) == 1)

    from datetime import timedelta as _timedelta

    wk_today = datetime.fromtimestamp(wk_now)
    days_to_sunday = (6 - wk_today.weekday()) % 7
    wk_sunday = (wk_today + _timedelta(days=days_to_sunday)).replace(
        hour=21, minute=30, second=0, microsecond=0
    )
    wk_saturday = wk_sunday - _timedelta(days=1)
    wk_scheduler = WeeklyReportScheduler(
        wk_service, wk_settings.chat.weekly_report
    )
    check(
        "非周日不发",
        await wk_scheduler.tick_once(now=wk_saturday.timestamp()) == [],
    )
    check(
        "周日到点发",
        await wk_scheduler.tick_once(now=wk_sunday.timestamp()) == [123456],
    )
    check(
        "同一天不重复发",
        await wk_scheduler.tick_once(now=wk_sunday.timestamp() + 60) == [],
    )
    await wk_service.shutdown()
    await wk_memory.close()

    wk_img = render_weekly_card(
        ["大家聊了爬山", "约了聚餐"],
        stats={"messages": 100, "active_days": 4, "care": 2},
        activity=[{"day": f"10-2{index}", "count": index * 3} for index in range(7)],
        word_cloud=[{"word": "爬山", "count": 9}, {"word": "聚餐", "count": 5}],
        affinity=[{"name": "小明", "score": 23, "level": "熟鱼片"}],
        week_range="10-19 ~ 10-25",
    )
    check(
        "周报图片生成",
        wk_img[:8] == b"\x89PNG\r\n\x1a\n" and len(wk_img) > 5000,
    )
    with _Image.open(_BytesIO(wk_img)) as wk_image:
        check("周报图片宽度", wk_image.width == 860)

    print("[40] 缓存指标与数据清理")
    from metrics import MetricsStore as _MetricsStore

    mdb = data_dir / f"smoke40-metrics-{tag}.db"
    mstore = _MetricsStore(mdb)
    mstore.init()
    metric_day = _MetricsStore._day_start(0)
    mstore.record(
        kind="chat",
        model="m",
        prompt_tokens=1000,
        completion_tokens=100,
        total_tokens=1100,
        cache_hit_tokens=800,
        cache_miss_tokens=200,
        ts=metric_day + 10,
    )
    mstore.record(
        kind="chat",
        model="m",
        prompt_tokens=500,
        completion_tokens=50,
        total_tokens=550,
        cache_hit_tokens=300,
        cache_miss_tokens=200,
        ts=metric_day + 20,
    )
    metric_summary = mstore.today_summary()
    check(
        "缓存命中统计",
        metric_summary["cache_hit_tokens"] == 1100
        and metric_summary["cache_miss_tokens"] == 400,
    )
    check(
        "缓存命中率",
        metric_summary["cache_hit_rate"] == round(1100 / 1500, 3),
    )
    metric_daily = mstore.daily(1)
    check("按天也有缓存率", bool(metric_daily) and "cache_hit_rate" in metric_daily[-1])
    mstore.close()

    import sqlite3 as _sqlite3

    old_db = data_dir / f"smoke40-old-{tag}.db"
    old_conn = _sqlite3.connect(old_db)
    old_conn.execute(
        "CREATE TABLE llm_usage (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ts REAL NOT NULL, kind TEXT NOT NULL, model TEXT NOT NULL, "
        "prompt_tokens INTEGER NOT NULL DEFAULT 0, "
        "completion_tokens INTEGER NOT NULL DEFAULT 0, "
        "total_tokens INTEGER NOT NULL DEFAULT 0, "
        "duration_ms REAL NOT NULL DEFAULT 0, ok INTEGER NOT NULL DEFAULT 1)"
    )
    old_conn.execute(
        "INSERT INTO llm_usage(ts, kind, model, prompt_tokens, completion_tokens, "
        "total_tokens, duration_ms, ok) VALUES(?,?,?,?,?,?,?,?)",
        (metric_day + 5, "chat", "m", 10, 5, 15, 1.0, 1),
    )
    old_conn.commit()
    old_conn.close()
    migrated = _MetricsStore(old_db)
    migrated.init()
    migrated_summary = migrated.today_summary()
    check(
        "老库自动补缓存列",
        migrated_summary["cache_hit_rate"] == 0.0
        and migrated_summary["calls"] == 1,
    )
    migrated.close()

    prune_db = data_dir / f"smoke40-prune-{tag}.db"
    prune_memory = MemoryStore(prune_db)
    await prune_memory.init()
    prune_old = time.time() - 40 * 86400
    prune_new = time.time() - 1 * 86400
    for prune_ts in (prune_old, prune_new):
        await prune_memory._write(
            "INSERT INTO archive(session_id, role, name, content, ts) "
            "VALUES(?,?,?,?,?)",
            ("group:1", "user", "小明", "旧消息", prune_ts),
        )
    check("归档清理只删旧的", await prune_memory.prune_archive(30) == 1)
    for image_ts, image_name in ((prune_old, "a.jpg"), (prune_new, "b.jpg")):
        await prune_memory._write(
            "INSERT INTO image_memory(session_id, user_id, name, description, "
            "media_key, created_at) VALUES(?,?,?,?,?,?)",
            ("group:1", 1, image_name, "描述", "", image_ts),
        )
    check("图片记忆清理", await prune_memory.prune_image_memory(30) == 1)
    old_reminder = await prune_memory.add_reminder(
        "group:1", 1, "旧提醒", time.time() + 60
    )
    await prune_memory.mark_reminder_done(old_reminder)
    await prune_memory._write(
        "UPDATE reminders SET created_at = ? WHERE id = ?",
        (prune_old, old_reminder),
    )
    await prune_memory.add_reminder("group:1", 1, "新提醒", time.time() + 60)
    check("已完成旧提醒清理", await prune_memory.prune_reminders(7) == 1)
    check(
        "未完成提醒保留",
        len(await prune_memory.list_reminders("group:1")) == 1,
    )
    old_care = await prune_memory.add_care_event(
        "group:1", 1, "小明", "sick", "感冒", time.time() + 60
    )
    await prune_memory.mark_care_sent(old_care)
    await prune_memory._write(
        "UPDATE care_events SET created_at = ? WHERE id = ?",
        (prune_old, old_care),
    )
    await prune_memory.add_care_event(
        "group:1", 1, "小红", "exam", "考试", time.time() + 60
    )
    check("已发送旧关心清理", await prune_memory.prune_care_events(30) == 1)
    check("未发送关心保留", await prune_memory.count_care_pending() == 1)

    prune_service = ChatService(
        make_settings(prune_db),
        llm=StubLLM(),
        sender=StubSender(),
        memory=prune_memory,
        persona="你是大肥鱼。",
    )
    maint_result = await prune_service.maintenance()
    check(
        "维护任务返回统计",
        isinstance(maint_result, dict)
        and "archive" in maint_result
        and "care" in maint_result,
    )
    await prune_service.shutdown()
    await prune_memory.close()

    print("[41] 联网搜索健壮性")
    import web_search as _ws

    baidu_sample = (
        '<h3 class="t"><a href="http://www.baidu.com/link?url=abc">上海天气预报</a></h3>'
        '<h3 class="t"><a href="https://weather.com.cn/weather/101020100.shtml">'
        "上海天气</a></h3>"
        '<h3 class="t"><a href="http://www.baidu.com/link?url=abc">重复链接</a></h3>'
    )
    baidu_parsed = _ws.parse_baidu_results(baidu_sample, 5)
    check(
        "百度解析出标题",
        "上海天气预报" in baidu_parsed and "上海天气" in baidu_parsed,
    )
    check("百度解析去重", "重复链接" not in baidu_parsed)
    check("结果条数统计", _ws._count_results(baidu_parsed) == 2)

    sogou_sample = (
        '<h3><a href="https://weatherol.cn/a">上海天气 A</a></h3>'
        '<div class="str_info">摘要甲</div>'
        '<h3><a href="/link?url=xyz">上海天气 B</a></h3>'
        '<div class="str_info">摘要乙</div>'
    )
    sogou_items = _ws.parse_sogou_items(sogou_sample, 5)
    check(
        "搜狗解析出结果",
        len(sogou_items) == 2 and sogou_items[0]["title"] == "上海天气 A",
    )
    check(
        "搜狗相对链接补全",
        sogou_items[1]["url"].startswith("https://www.sogou.com/"),
    )
    check("搜狗摘要提取", sogou_items[0]["snippet"] == "摘要甲")

    so360_sample = (
        '<h3><a href="https://tianqi.so.com/a">360 天气</a></h3>'
        '<p class="res-desc">360 摘要</p>'
    )
    so360_items = _ws.parse_so360_items(so360_sample, 5)
    check(
        "360 解析出结果",
        len(so360_items) == 1 and "360 摘要" in so360_items[0]["snippet"],
    )

    merged_items = _ws._merge_items(
        [
            {
                "title": "同一篇",
                "snippet": "",
                "url": "https://example.com/a",
                "source": "Bing",
            },
            {
                "title": "同一篇",
                "snippet": "摘要",
                "url": "https://example.com/a/",
                "source": "搜狗",
            },
            {
                "title": "另一篇",
                "snippet": "",
                "url": "https://example.com/b",
                "source": "360",
            },
        ]
    )
    check("聚合去重", len(merged_items) == 2)
    check(
        "多源命中合并",
        any(len(item["sources"]) == 2 for item in merged_items),
    )

    _ws._CACHE.clear()
    cache_calls = {"n": 0}

    async def fake_source(query, max_results):  # noqa: ANN001
        cache_calls["n"] += 1
        return [
            {
                "title": "缓存标题",
                "snippet": "",
                "url": "https://example.com/cache",
                "source": "fake",
            }
        ]

    original_sources = _ws._SOURCES
    _ws._SOURCES = (("fake", fake_source),)
    try:
        first_hit = await _ws.search_web("缓存测试")
        second_hit = await _ws.search_web("缓存测试")
        check("搜索缓存生效", cache_calls["n"] == 1 and first_hit == second_hit)
    finally:
        _ws._SOURCES = original_sources
        _ws._CACHE.clear()

    async def fail_source(query, max_results):  # noqa: ANN001
        raise RuntimeError("boom")

    async def ok_source(query, max_results):  # noqa: ANN001
        return [
            {
                "title": "兜底标题",
                "snippet": "",
                "url": "https://example.com/ok",
                "source": "ok",
            }
        ]

    _ws._SOURCES = (("fail", fail_source), ("ok", ok_source))
    try:
        fallback_result = await _ws.search_web("降级测试")
        check("单源失败仍能出结果", "兜底标题" in fallback_result)
    finally:
        _ws._SOURCES = original_sources
        _ws._CACHE.clear()

    _ws._SOURCES = (("fail", fail_source),)
    try:
        failed_result = await _ws.search_web("全失败测试")
        check(
            "全失败有明确提示",
            "暂时不可用" in failed_result and "不要编造" in failed_result,
        )
    finally:
        _ws._SOURCES = original_sources
        _ws._CACHE.clear()

    print("[42] 站内搜索（B站/虎扑）")
    import site_search as _ss

    check(
        "识别去B站搜",
        _ss.detect_site("去B站搜一下 IG 比赛") == ("bilibili", "IG 比赛"),
    )
    check(
        "识别虎扑",
        _ss.detect_site("在虎扑看看 IG 比赛") == ("hupu", "IG 比赛"),
    )
    check("普通搜索不识别站点", _ss.detect_site("搜一下 IG 比赛") is None)

    bili_html = (
        '<a href="//www.bilibili.com/video/BV1s8nd6XEaG/" target="_blank">'
        '<div class="bili-video-card__image"></div></a>'
        '<h3 class="bili-video-card__info--tit" '
        'title="【全球总决赛】IG vs HLE 上路视角">'
        '<em class="keyword">IG</em> vs HLE 上路视角</h3>'
        '<a class="bili-video-card__info--owner" '
        'href="//space.bilibili.com/1/">ArmadylLOL</a>'
        '<span class="bili-video-card__stats__duration">28:32</span>'
        '<a href="//www.bilibili.com/video/BV1fRnD6FEfp/">'
        '<h3 title="IG 暴虐 HLE">IG 暴虐 HLE</h3>'
    )
    bili_items = _ss.parse_bilibili_html(bili_html, 5)
    check(
        "B站HTML解析",
        len(bili_items) == 2 and bili_items[0]["title"].startswith("【全球总决赛】"),
    )
    check(
        "B站链接拼装",
        bili_items[0]["url"] == "https://www.bilibili.com/video/BV1s8nd6XEaG",
    )
    check("B站UP主提取", "ArmadylLOL" in bili_items[0]["snippet"])
    check("B站时长提取", "28:32" in bili_items[0]["snippet"])

    async def empty_bili(query, max_results=5):  # noqa: ANN001
        return ""

    original_bili = _ss.search_bilibili
    _ss.search_bilibili = empty_bili
    try:
        check(
            "B站无结果有提示",
            "暂时不可用" in await _ss.search_site("bilibili", "IG 比赛"),
        )
    finally:
        _ss.search_bilibili = original_bili

    async def fake_web(query, max_results=5):  # noqa: ANN001
        return "摘要\n来源：https://example.com/hupu"

    original_web = _ss.search_web
    _ss.search_web = fake_web
    try:
        hupu_result = await _ss.search_site("hupu", "IG 比赛")
        check(
            "虎扑降级到通用搜索",
            "虎扑没有公开搜索接口" in hupu_result
            and "example.com/hupu" in hupu_result,
        )
    finally:
        _ss.search_web = original_web

    site_db = data_dir / f"smoke42-{tag}.db"
    site_memory = MemoryStore(site_db)
    await site_memory.init()
    site_settings = make_settings(site_db)
    site_settings.chat.search.enabled = True
    site_settings.chat.search.auto = True
    site_service = ChatService(
        site_settings,
        llm=StubLLM(),
        sender=StubSender(),
        memory=site_memory,
        persona="你是大肥鱼。",
    )

    async def fake_chat_site(site, query, max_results=5):  # noqa: ANN001
        return "B站结果\n来源：https://www.bilibili.com/video/BV1s8nd6XEaG"

    import chat as _chat_mod

    original_chat_site = _chat_mod.search_site
    _chat_mod.search_site = fake_chat_site
    try:
        site_block = await site_service._search_block(
            parse_incoming(group_event("去B站搜一下 IG 比赛", mid=2200, at=True), 999)
        )
        check(
            "自动预搜走站内",
            "站内搜索结果" in site_block
            and "bilibili.com/video" in site_block,
        )
    finally:
        _chat_mod.search_site = original_chat_site
    await site_service.shutdown()
    await site_memory.close()

    import tools as _tools

    original_tools_site = _tools.search_site

    async def fake_tools_site(site, query, max_results=5):  # noqa: ANN001
        return f"tool:{site}:{query}"

    _tools.search_site = fake_tools_site
    try:
        tool_out = await _tools.dispatch(
            "search_site",
            json.dumps({"site": "bilibili", "query": "IG"}),
            ToolContext(
                session_id="group:1", memory=site_memory, persona_name="大肥鱼"
            ),
        )
        check("站内搜索工具可用", tool_out == "tool:bilibili:IG")
    finally:
        _tools.search_site = original_tools_site

    print("[43] 搜索意图优先于看图反问")
    from onebot import Incoming
    sc_db = data_dir / f"smoke43-{tag}.db"
    sc_memory = MemoryStore(sc_db)
    await sc_memory.init()
    sc_settings = make_settings(sc_db)
    sc_settings.chat.search.enabled = True
    sc_service = ChatService(
        sc_settings,
        llm=StubLLM(),
        sender=StubSender(),
        memory=sc_memory,
        persona="你是大肥鱼。",
    )

    def sc_incoming(text, *, images=None, reply_to="9", at_self=True):
        return Incoming(
            kind="group",
            session_id="group:123456",
            user_id=20001,
            message_id="1",
            nickname="小明",
            text=text,
            at_self=at_self,
            reply_to=reply_to,
            group_id=123456,
            images=list(images or []),
        )

    search_msg = sc_incoming("去b站搜一搜超市你是什么意思")
    check("搜索意图不当作问图", not sc_service._wants_image_explain(search_msg))
    check(
        "带图+搜索也走搜索",
        not sc_service._wants_image_explain(
            sc_incoming("去b站搜一下 IG 比赛", images=["http://x/a.jpg"])
        ),
    )
    check(
        "普通看图不受影响",
        sc_service._wants_image_explain(
            sc_incoming("这张图是什么意思", images=["http://x/a.jpg"])
        ),
    )

    async def fake_site43(site, query, max_results=5):  # noqa: ANN001
        return "B站结果\n来源：https://www.bilibili.com/video/BV1xx"

    import chat as _chat43

    original_site43 = _chat43.search_site
    _chat43.search_site = fake_site43
    try:
        block43 = await sc_service._search_block(search_msg)
        check(
            "站内搜索块命中",
            "站内搜索结果" in block43 and "bilibili.com" in block43,
        )
    finally:
        _chat43.search_site = original_site43
    await sc_service.shutdown()
    await sc_memory.close()

    print("[44] 主人一句话重置额度（不用再手动改后台）")
    rb_db = data_dir / f"smoke44-{tag}.db"
    rb_budget_db = data_dir / f"smoke44-budget-{tag}.db"
    rb_settings = make_settings(
        rb_db,
        budget={
            "enabled": True,
            "daily_cny_per_group": 0.3,
            "db_path": str(rb_budget_db),
        },
    )
    rb_memory = MemoryStore(rb_db)
    await rb_memory.init()
    rb_sender = StubSender()
    rb_service = ChatService(
        rb_settings,
        llm=StubLLM(),
        sender=rb_sender,
        memory=rb_memory,
        persona="你是大肥鱼。",
    )
    try:
        rb_service._record_budget(
            123456, {"prompt_tokens": 500_000, "completion_tokens": 0}
        )
        rb_service._record_budget(
            999999, {"prompt_tokens": 500_000, "completion_tokens": 0}
        )
        check("重置前确实有花费", rb_service._budget.today_cny(123456) > 0)

        # 群聊命令：只重置本群
        sent_before = len(rb_sender.sent)
        await rb_service.handle(
            parse_incoming(group_event("/重置额度", mid=440, user=10001), 999)
        )
        check("主人命令重置本群额度", rb_service._budget.today_cny(123456) == 0)
        check("其他群额度不受影响", rb_service._budget.today_cny(999999) > 0)
        check(
            "重置额度有回复",
            any("已重置" in item["text"] for item in rb_sender.sent[sent_before:]),
        )

        # 群聊大白话：认「重置一下额度」，不用记命令
        rb_service._record_budget(
            123456, {"prompt_tokens": 500_000, "completion_tokens": 0}
        )
        sent_before = len(rb_sender.sent)
        await rb_service.handle(
            parse_incoming(
                group_event("帮我把今天的额度重置一下", mid=441, user=10001), 999
            )
        )
        check("主人大白话也能重置本群", rb_service._budget.today_cny(123456) == 0)
        check(
            "大白话重置有回复",
            any("已重置" in item["text"] for item in rb_sender.sent[sent_before:]),
        )

        # 私聊不带群号：重置全部群
        sent_before = len(rb_sender.sent)
        await rb_service.handle(
            parse_incoming(private_event("额度重置一下", mid=442, user=10001), 999)
        )
        check("主人私聊重置全部群", rb_service._budget.today_cny(999999) == 0)
        check(
            "私聊回复提到全部群",
            any("全部群" in item["text"] for item in rb_sender.sent[sent_before:]),
        )

        # 带群号：只重置指定群
        rb_service._record_budget(
            123456, {"prompt_tokens": 500_000, "completion_tokens": 0}
        )
        await rb_service.handle(
            parse_incoming(
                group_event("/重置额度 999999", mid=443, user=10001), 999
            )
        )
        check("指定群号才重置那个群", rb_service._budget.today_cny(123456) > 0)

        # 普通群友：不生效
        rb_service._budget.reset_today(123456)
        rb_service._record_budget(
            123456, {"prompt_tokens": 500_000, "completion_tokens": 0}
        )
        await rb_service.handle(
            parse_incoming(group_event("/重置额度", mid=444, user=20001), 999)
        )
        check("普通群友不能重置额度", rb_service._budget.today_cny(123456) > 0)

        # 疑问句：不当命令，仍当普通聊天（并正常产生新花费）
        rb_service._budget.reset_today(123456)
        sent_before = len(rb_sender.sent)
        await rb_service.handle(
            parse_incoming(
                group_event("这个额度是怎么算的？", mid=445, user=10001, at=True),
                999,
            )
        )
        check(
            "疑问句不触发重置",
            all("已重置" not in item["text"] for item in rb_sender.sent[sent_before:]),
        )
        check("疑问句走普通聊天", len(rb_sender.sent) > sent_before)
    finally:
        await rb_service.shutdown()
        await rb_memory.close()

    await service.shutdown()
    await memory.close()

    # 清理测试库（连同 WAL / SHM 文件）与临时自画像目录
    import shutil

    for folder in data_dir.glob(f"smoke19empty-{tag}*"):
        shutil.rmtree(folder, ignore_errors=True)
    for path in data_dir.glob(f"smoke*{tag}.db*"):
        try:
            path.unlink()
        except OSError:
            pass


def main() -> None:
    try:
        asyncio.run(run_tests())
    except AssertionError as exc:
        print(exc)
        raise SystemExit(1) from exc
    print(f"\n全部通过：{PASSED} 项检查")


if __name__ == "__main__":
    main()
