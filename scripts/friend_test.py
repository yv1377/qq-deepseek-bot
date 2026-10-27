"""群友模式、搜索和培养回归测试。"""
import random
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from config import PROJECT_ROOT, load_settings
from onebot import Incoming
from policy import AmbientGate, RateLimiter, decide, clean_reply
from tools import ToolContext, dispatch
from web_search import parse_results


class FriendTests(unittest.IsolatedAsyncioTestCase):
    def test_at_and_probability(self):
        cfg = load_settings(PROJECT_ROOT / "config/config.yaml").chat
        gate = AmbientGate(cfg.group.ambient_chance, cfg.group.ambient_min_interval)
        limiter = RateLimiter()
        msg = Incoming("group", "group:1", 2, "1", "小明", "肥鱼在吗", group_id=1)
        msg.at_self = True
        for _ in range(20):
            self.assertTrue(decide(msg, cfg, {}, limiter, gate, now=100).reply)
        msg.at_self = False
        rng = random.Random(42)
        count = sum(decide(msg, cfg, {}, limiter, gate, rng=rng, now=100).reply
                    for _ in range(10000))
        self.assertTrue(300 < count < 700, count)  # 当前 ambient_chance=5%
        msg.is_self = True
        self.assertFalse(decide(msg, cfg, {}, limiter, gate).reply)
        msg.is_self = False
        msg.at_self = True
        cfg.filter.blacklist_users = [2]
        self.assertFalse(decide(msg, cfg, {}, limiter, gate).reply)

    async def test_cultivation_scope_and_dedup(self):
        memory = AsyncMock()
        memory.recent_notes.return_value = []
        ctx = ToolContext("group:1", memory, "大肥鱼", 2)
        await dispatch("cultivate_personality", '{"preference":"少反问"}', ctx)
        memory.add_note.assert_awaited_once_with(
            "group:1", "[性格培养] 少反问", source="性格培养"
        )
        memory.recent_notes.return_value = ["[性格培养] 少反问"]
        await dispatch("cultivate_personality", '{"preference":"少反问"}', ctx)
        self.assertEqual(memory.add_note.await_count, 1)

    def test_search_and_links(self):
        result = parse_results(b'<rss><channel><item><title>Python</title><link>https://python.org/</link><description>Official</description></item></channel></rss>')
        self.assertIn("https://python.org/", result)
        self.assertIn("https://python.org/", clean_reply("[来源](https://python.org/)"))

    async def test_background_image_scan(self):
        """群友发图时后台静默识别，写进群聊背景，不回复。"""
        import asyncio
        from unittest.mock import AsyncMock
        from chat import ChatService
        from config import Settings

        class _Vision:
            enabled = True

            def __init__(self):
                self.calls = 0

            async def describe(self, urls, **kwargs):  # noqa: ANN001, ANN003
                self.calls += 1
                return "一只橘猫趴在键盘上，表情困惑"

        settings = Settings.from_dict(
            {"chat": {"group": {"ambient_chance": 0}, "memes": {"enabled": False}}}
        )
        vision = _Vision()
        sender = AsyncMock()
        service = ChatService(
            settings, llm=AsyncMock(), sender=sender,
            memory=AsyncMock(), vision=vision, persona="测试",
        )
        inc = Incoming(
            "group", "group:1", 2, "1", "小明", "", group_id=1,
            images=["https://example.com/cat.jpg"],
        )
        await service.handle(inc)
        await asyncio.gather(*list(service._background))
        rendered = service.history.render(1)
        self.assertIn("图片内容", rendered)
        self.assertIn("橘猫", rendered)
        self.assertEqual(vision.calls, 1)
        sender.send_group_text.assert_not_awaited()
        # 同一张图不重复扫
        await service.handle(inc)
        await asyncio.gather(*list(service._background))
        self.assertEqual(vision.calls, 1)

    async def test_image_memory_store_and_block(self):
        """图片记忆：同图去重更新、群友解释补全。"""
        import shutil
        import uuid
        from memory import MemoryStore

        folder = Path("data/tmp-test") / f"img-mem-{uuid.uuid4().hex}"
        folder.mkdir(parents=True, exist_ok=True)
        try:
            store = MemoryStore(folder / "bot.db")
            await store.init()
            await store.add_image_memory(
                "group:1", 2, "小明", "一只橘猫趴在键盘上", "img-key-1"
            )
            await store.add_image_memory(
                "group:1", 2, "小明", "一只橘猫在键盘上大笑", "img-key-1"
            )
            rows = await store.recent_image_memories("group:1")
            self.assertEqual(len(rows), 1)
            self.assertIn("大笑", rows[0]["description"])
            await store.update_latest_image_memory("group:1", "表示无语")
            rows = await store.recent_image_memories("group:1")
            self.assertIn("群友解释", rows[0]["description"])
            self.assertIn("无语", rows[0]["description"])
            await store.close()
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    async def test_curator_writes_per_user_notes(self):
        """记忆管家：把 [人] 行写进人物笔记，群事实仍写会话笔记。"""
        import shutil
        import uuid
        from unittest.mock import AsyncMock
        from chat import ChatService
        from config import Settings
        from memory import MemoryStore

        folder = Path("data/tmp-test") / f"curate-user-{uuid.uuid4().hex}"
        folder.mkdir(parents=True, exist_ok=True)

        class _LLM:
            async def consolidate(self, transcript, existing_notes="", **kwargs):  # noqa: ANN001, ANN003
                return (
                    "- [群] 本群最近在聊猫\n"
                    "- [人] 12345|小明|喜欢橘猫，常发猫图\n"
                    "- [人] 67890|小红|在准备考试"
                )

        try:
            store = MemoryStore(folder / "bot.db")
            await store.init()
            for index in range(4):
                await store.add_message(
                    "group:1", "user", "小明（QQ 12345）", f"第{index}条消息"
                )
            settings = Settings.from_dict(
                {
                    "deepseek": {"api_key": "test", "enable_tools": False},
                    "chat": {"memory": {"db_path": str(folder / "bot.db")}},
                }
            )
            service = ChatService(
                settings, llm=_LLM(), sender=AsyncMock(), memory=store,
                persona="测试",
            )
            await service._curate("group:1")
            user_notes = await store.recent_user_notes(12345, 5)
            self.assertTrue(user_notes)
            self.assertIn("橘猫", user_notes[0])
            self.assertTrue(await store.recent_user_notes(67890, 5))
            group_notes = await store.recent_notes("group:1", 5)
            self.assertTrue(any("本群最近在聊猫" in note for note in group_notes))
            await store.close()
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    async def test_group_fragment_merge(self):
        """短句/半句话不会立刻回：同一人连发的碎片先合并成一条。"""
        import asyncio
        from unittest.mock import AsyncMock
        from chat import ChatService
        from config import Settings

        settings = Settings.from_dict(
            {"chat": {"group": {"merge_window": 0.05}}}
        )
        service = ChatService(
            settings, llm=AsyncMock(), sender=AsyncMock(),
            memory=AsyncMock(), persona="测试",
        )
        first = Incoming(
            "group", "group:1", 2, "m1", "小明", "你觉得", group_id=1
        )
        second = Incoming(
            "group", "group:1", 2, "m2", "小明", "这个怎么样", group_id=1
        )
        self.assertTrue(service._looks_like_fragment(first))
        results = await asyncio.gather(
            service._merge_group_fragments(first),
            service._merge_group_fragments(second),
        )
        merged = [item for item in results if item is not None]
        self.assertEqual(len(merged), 1)
        self.assertIn("你觉得", merged[0].text)
        self.assertIn("这个怎么样", merged[0].text)
        self.assertEqual(merged[0].message_id, "m2")

    async def test_auto_search_block(self):
        """关键问题自动预搜；普通闲聊不搜。"""
        from unittest.mock import AsyncMock, patch
        from chat import ChatService
        from config import Settings

        settings = Settings.from_dict(
            {"chat": {"search": {"enabled": True, "auto": True, "min_chars": 4}}}
        )
        service = ChatService(
            settings, llm=AsyncMock(), sender=AsyncMock(),
            memory=AsyncMock(), persona="测试",
        )

        def inc(text: str) -> Incoming:
            return Incoming(
                "group", "group:1", 2, "1", "小明", text,
                at_self=True, group_id=1,
            )

        self.assertTrue(service._should_auto_search(inc("今天上海天气怎么样")))
        self.assertTrue(service._should_auto_search(inc("帮我查一下最新显卡价格")))
        self.assertFalse(service._should_auto_search(inc("哈哈哈")))
        self.assertFalse(service._should_auto_search(inc("在吗")))
        self.assertFalse(service._should_auto_search(inc("今天好累")))

        with patch(
            "chat.search_web",
            AsyncMock(return_value="摘要：上海天气今天多云\n来源：https://example.com"),
        ):
            block = await service._search_block(inc("今天上海天气怎么样"))
        self.assertIn("联网搜索结果", block)
        self.assertIn("example.com", block)
        with patch(
            "chat.search_web",
            AsyncMock(return_value="未找到可用搜索结果。"),
        ):
            fallback = await service._search_block(inc("🍬是什么梗"))
        self.assertIn("问群友", fallback)

    async def test_quoted_context_and_mentioned_user_notes(self):
        """引用消息取回原文；被 @ 的人如果有画像也会注入。"""
        import shutil
        import uuid
        from unittest.mock import AsyncMock
        from chat import ChatService
        from config import Settings
        from memory import MemoryStore

        class _Sender:
            async def get_message(self, message_id):  # noqa: ANN001
                return {
                    "message_id": message_id,
                    "message": [{"type": "text", "data": {"text": "我昨天发的那张猫图"}}],
                    "sender": {"card": "小红", "nickname": "小红"},
                }

        folder = Path("data/tmp-test") / f"quote-{uuid.uuid4().hex}"
        folder.mkdir(parents=True, exist_ok=True)
        try:
            store = MemoryStore(folder / "bot.db")
            await store.init()
            await store.add_user_note(12345, "喜欢橘猫，常发猫图", source="手动")
            settings = Settings.from_dict(
                {"chat": {"memory": {"db_path": str(folder / "bot.db")}}}
            )
            service = ChatService(
                settings, llm=AsyncMock(), sender=_Sender(), memory=store,
                persona="测试",
            )
            inc = Incoming(
                "group", "group:1", 2, "m2", "小明", "你看过这个吗",
                at_self=True, group_id=1, reply_to="m1",
                raw={"message": [
                    {"type": "at", "data": {"qq": "12345"}},
                    {"type": "text", "data": {"text": "你看过这个吗"}},
                ]},
            )
            quote = await service._quoted_context(inc)
            self.assertIn("引用消息", quote)
            self.assertIn("猫图", quote)
            notes, _ = await service._memory_notes(inc)
            self.assertIn("关于被@的 QQ 12345", notes)
            self.assertIn("橘猫", notes)
            await store.close()
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_reactive_group_config(self):
        """870 嘴臭高攻、固定记忆、低表情包概率；其他群不受影响。"""
        from config import PROJECT_ROOT, load_settings

        settings = load_settings(PROJECT_ROOT / "config/config.yaml")
        rules = settings.persona.group_rules.get(100000001, "")
        self.assertIn("嘴臭", rules)
        self.assertIn("不影响攻击性", rules)
        self.assertIn("不要用表情包代替文字", rules)
        pins = settings.chat.memory.pins.get("group:100000001", [])
        self.assertTrue(any("鱼姐" in pin for pin in pins))
        self.assertTrue(any("3871324658" in pin for pin in pins))
        self.assertLess(
            settings.chat.memes.group_chances.get(100000001, 1.0), 0.1
        )
        self.assertGreater(
            settings.chat.memes.group_cooldowns.get(100000001, 0), 0
        )
        # 其他群没有反应式攻击规则
        self.assertNotIn(100000002, settings.persona.group_rules)

    async def test_search_query_expansion(self):
        """搜索词去停用词，梗/黑话补“梗 意思 出处”。"""
        from unittest.mock import AsyncMock
        from chat import ChatService
        from config import Settings

        service = ChatService(
            Settings(), llm=AsyncMock(), sender=AsyncMock(),
            memory=AsyncMock(), persona="测试",
        )
        inc = Incoming("group", "group:100000001", 2, "1", "小明", "🍬是什么梗", at_self=True, group_id=100000001)
        query = service._search_query_text(inc)
        self.assertIn("唐", query)
        self.assertIn("梗", query)
        self.assertNotIn("你", query)

    def test_meme_privacy_and_pending_delete(self):
        """隐私图识别；主人说删只标记，确认后才真正删。"""
        import shutil
        import uuid
        from config import Settings
        from memes import MemeStore

        root = Path("data/tmp-meme-test") / uuid.uuid4().hex
        root.mkdir(parents=True)
        try:
            store = MemeStore(Settings().chat.memes)
            store.root = root
            store.pending_path = root / "pending.json"
            group_id = 100000001
            folder = root / str(group_id)
            folder.mkdir()
            image = folder / "abc.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20)
            self.assertTrue(store.looks_private("后台 token 用量截图"))
            self.assertTrue(store.mark_by_name(group_id, "abc.png", "测试", 3871324658))
            self.assertEqual(len(store.pending_deletions()), 1)
            self.assertEqual(store.purge_pending(), 1)
            self.assertFalse(image.exists())
            self.assertFalse(store.pending_deletions())
        finally:
            shutil.rmtree(root, ignore_errors=True)

    async def test_image_expression_inference(self):
        """图片描述 + 聊天上下文 -> 推断表情表达。"""
        from unittest.mock import AsyncMock
        from chat import ChatService
        from config import Settings

        settings = Settings.from_dict(
            {"vision": {"infer_expression": True, "expression_max_tokens": 40}}
        )
        llm = AsyncMock()
        llm.chat.return_value = "表达：轻蔑挑衅"
        service = ChatService(
            settings, llm=llm, sender=AsyncMock(), memory=AsyncMock(),
            persona="测试",
        )
        inc = Incoming(
            "group", "group:100000001", 2, "m1", "群友", "[图片]",
            at_self=True, group_id=100000001,
        )
        meaning = await service._infer_image_expression(
            inc, "一只斜眼笑的卡通猫，旁边写着“就这？”"
        )
        self.assertIn("轻蔑", meaning)

    def test_web_fetch_rejects_private_urls(self):
        """网页读取工具不能访问本机/内网地址。"""
        from web_fetch import _is_public_url

        self.assertFalse(_is_public_url("http://127.0.0.1:8080/")[0])
        self.assertFalse(_is_public_url("http://localhost/")[0])
        self.assertFalse(_is_public_url("file:///etc/passwd")[0])
        self.assertFalse(_is_public_url("http://10.0.0.1/")[0])

    async def test_penna_prompt_command(self):
        """Penna 轻量移植：/penna 在所有群可用。"""
        from unittest.mock import AsyncMock
        from chat import ChatService
        from config import Settings

        llm = AsyncMock()
        llm.chat.return_value = (
            "【正向提示词】\n1girl, white hair, neon city, cinematic lighting\n"
            "【负向提示词】\nlowres, bad anatomy\n"
            "【中文说明】\n雨夜霓虹街头，低机位，可调整发色和镜头。"
        )
        service = ChatService(
            Settings(), llm=llm, sender=AsyncMock(), memory=AsyncMock(),
            persona="测试",
        )
        inc = Incoming(
            "group", "group:100000001", 2, "1", "群友",
            "/penna 雨夜霓虹街头的白发少女", at_self=True, group_id=100000001,
        )
        reply = await service._penna_reply(inc)
        self.assertIn("正向提示词", reply)
        self.assertIn("white hair", reply)
        self.assertIn("负向提示词", reply)

    async def test_web_browse_summary(self):
        """搜索 + 打开来源页 + 模型总结。"""
        from unittest.mock import AsyncMock, patch
        from web_browse import search_and_summarize

        llm = AsyncMock()
        llm.chat.return_value = "结论：今天上海多云，来源：https://example.com"
        results = (
            "以下为外部搜索摘要\n上海天气\n今天多云\n来源：https://example.com/weather"
        )
        with patch("web_browse.search_web", AsyncMock(return_value=results)), patch(
            "web_browse.fetch_url", AsyncMock(return_value="多云，20-26 度。")
        ):
            answer = await search_and_summarize(llm, "今天上海天气")
        self.assertIn("今天上海多云", answer)
        self.assertTrue(llm.chat.await_count >= 1)

    async def test_followup_data_search(self):
        """对话里的“赛程表/发我”要补上下文，并直接走搜索总结。"""
        from unittest.mock import AsyncMock
        from chat import ChatService
        from config import Settings

        settings = Settings.from_dict(
            {"chat": {"search": {"enabled": True, "auto": True}}}
        )
        service = ChatService(
            settings, llm=AsyncMock(), sender=AsyncMock(), memory=AsyncMock(),
            persona="测试",
        )
        service.history.add(
            100000002, "蓝瑟大肥鱼", "[搜索总结：S16 全球总决赛 10月16日开打]"
        )
        inc = Incoming(
            "group", "group:100000002", 2, "m1", "摸鱼头子",
            "赛程内容直接文字发给我", at_self=True, group_id=100000002,
        )
        self.assertIn("英雄联盟世界赛", service._search_query_text(inc))
        self.assertTrue(service._should_direct_search(inc))

    async def test_neko_sticker_tools(self):
        """Bot 侧复用大肥鱼表情包知识库：搜索 + 发送标记。"""
        from unittest.mock import AsyncMock
        from chat import ChatService
        from config import Settings
        from tools import ToolContext, dispatch

        service = ChatService(
            Settings(), llm=AsyncMock(), sender=AsyncMock(), memory=AsyncMock(),
            persona="测试",
        )
        await service.neko.load()
        self.assertGreater(len(service.neko.stickers), 100)
        ctx = ToolContext(
            "group:1", service.memory, "大肥鱼", 2, "小明",
            group_id=1, neko=service.neko,
        )
        search = await dispatch("sticker_search", '{"query":"摸鱼"}', ctx)
        self.assertIn("摸鱼", search)
        send = await dispatch("sticker_send", '{"query":"摸鱼"}', ctx)
        self.assertTrue(send.startswith("[sticker:"))

    async def test_neko_auto_chance(self):
        """Neko 表情包独立 5% 自动穿插；不影响原图库概率。"""
        from unittest.mock import AsyncMock, patch
        from chat import ChatService
        from config import Settings

        settings = Settings.from_dict(
            {"chat": {"neko": {"enabled": True, "auto_chance": 0.05}}}
        )
        service = ChatService(
            settings, llm=AsyncMock(), sender=AsyncMock(), memory=AsyncMock(),
            persona="测试",
        )
        service._send_neko_sticker = AsyncMock(return_value=True)
        inc = Incoming(
            "group", "group:1", 2, "m1", "群友", "今天上班好累",
            at_self=True, group_id=1,
        )
        with patch("chat.random.random", return_value=0.01):
            self.assertTrue(await service._maybe_send_neko_auto(inc, "那就摸会儿鱼"))
        service._send_neko_sticker.assert_awaited_once()
        with patch("chat.random.random", return_value=0.99):
            self.assertFalse(await service._maybe_send_neko_auto(inc, "那就摸会儿鱼"))

    def test_memory_runtime_config(self):
        """上下文读取参数：最近消息更多、人物笔记单独注入、默认不拆条。"""
        from config import PROJECT_ROOT, load_settings

        settings = load_settings(PROJECT_ROOT / "config/config.yaml")
        self.assertGreaterEqual(settings.chat.memory.recent_limit, 16)
        self.assertGreaterEqual(settings.chat.memory.notes_limit, 8)
        self.assertGreaterEqual(settings.chat.memory.user_notes_limit, 8)
        self.assertGreater(
            settings.chat.reply.split_chars, settings.chat.reply.max_chars
        )

    def test_reply_diversity_tracking(self):
        """回复去重：记录、相似判断、避免重复提示、固定台词轮换。"""
        from unittest.mock import AsyncMock
        from chat import ChatService
        from config import Settings

        service = ChatService(
            Settings(), llm=AsyncMock(), sender=AsyncMock(),
            memory=AsyncMock(), persona="测试",
        )
        service._remember_bot_reply("group:1", "傻逼，你也配？")
        service._remember_bot_reply("group:1", "今天天气不错，出去走走")
        # _reply_is_repeated 的语义：调用方通常已经把「本条回复」记进历史，
        # 所以它会排除最后一条再比较——这里历史最后一条是别的句子，能正常比出旧句。
        self.assertTrue(service._reply_is_repeated("group:1", "傻逼，你也配？"))
        self.assertTrue(service._reply_is_repeated("group:1", "傻逼你也配？"))
        # 和「最后一条」相同的情况属于调用方已记入，按设计返回 False（不重复调模型）
        self.assertFalse(
            service._reply_is_repeated("group:1", "今天天气不错，出去走走")
        )
        block = service._avoid_repeat_block("group:1")
        self.assertIn("避免重复", block)
        self.assertIn("傻逼", block)
        first = service._pick_varied_line(("A", "B", "C"), "_probe_line")
        second = service._pick_varied_line(("A", "B", "C"), "_probe_line")
        self.assertNotEqual(first, second)

    def test_image_clarify_only_target_group(self):
        """100000001 看不懂图时反问；其他群不启用。"""
        from unittest.mock import AsyncMock
        from chat import ChatService
        from config import Settings

        service = ChatService(
            Settings(), llm=AsyncMock(), sender=AsyncMock(),
            memory=AsyncMock(), persona="测试",
        )
        self.assertTrue(service._image_description_unclear(""))
        self.assertTrue(service._image_description_unclear("画面无法识别"))
        self.assertTrue(service._image_description_unclear("看不清"))
        self.assertFalse(
            service._image_description_unclear("一只橘猫趴在键盘上，画面无文字")
        )

        def inc(group_id: int, text: str = "这张图是什么意思"):
            return Incoming(
                "group", f"group:{group_id}", 1, "1", "群友", text,
                at_self=True, group_id=group_id,
                images=["https://example.com/a.jpg"],
            )

        asked = service._image_clarify_reply(inc(100000001), "", has_source=True)
        self.assertIsNotNone(asked)
        self.assertTrue(
            any(word in asked for word in ("啥意思", "没看懂", "看不懂", "说清楚", "解释", "讲讲", "猜"))
        )
        self.assertIsNone(
            service._image_clarify_reply(inc(100000002), "", has_source=True)
        )
        self.assertIsNone(
            service._image_clarify_reply(
                inc(100000001), "一只橘猫趴在键盘上", has_source=True
            )
        )
        missing = service._image_clarify_reply(inc(100000001), "", has_source=False)
        self.assertIn("图", missing)

    def test_clean_fake_image_send(self):
        """模型文字里假装的“发送图片”动作要被清掉。"""
        cleaned = clean_reply("行，看清楚了（发送 my_angry 图片）\n装你个头")
        self.assertNotIn("发送", cleaned)
        self.assertNotIn("图片", cleaned)
        self.assertIn("看清楚了", cleaned)

    def test_clean_fixed_opening_and_cat_tail(self):
        """固定开场“主人，”和结尾“喵～/瞄～”要清掉，正常的括号动作保留。"""
        cleaned = clean_reply("主人，这话我可记着了，瞄～")
        self.assertNotIn("主人", cleaned)
        self.assertNotIn("瞄", cleaned)
        self.assertIn("记着了", cleaned)
        cleaned2 = clean_reply("今天好累喵～")
        self.assertNotIn("喵", cleaned2)
        self.assertIn("今天好累", cleaned2)
        # 正常角色动作和“哼”不算固定模板，别误删
        kept = clean_reply("（尾巴缩回来）别啃了")
        self.assertTrue(kept.startswith("（尾巴"))


if __name__ == "__main__":
    unittest.main()
