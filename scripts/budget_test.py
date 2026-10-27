"""频率与花费保护测试：回复突发限流 + 每群每日花费上限。

    python -m unittest scripts.budget_test -v
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from budget import BudgetStore, estimate_cny, is_peak  # noqa: E402
from chat import ChatService  # noqa: E402
from config import BudgetConfig, Settings  # noqa: E402
from memory import MemoryStore  # noqa: E402
from onebot import Incoming  # noqa: E402


def temp_dir(name: str) -> Path:
    path = ROOT / "data" / "tmp-test" / f"{name}-{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    return path


class FakeLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        hook = kwargs.get("on_usage")
        if hook:
            hook({"prompt_tokens": 1000, "completion_tokens": 100})
        return "收到"

    async def complete(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        hook = kwargs.get("on_usage")
        if hook:
            hook({"prompt_tokens": 1000, "completion_tokens": 100})
        return {"role": "assistant", "content": "收到"}

    async def summarize(self, old_summary, transcript, **kwargs):  # noqa: ANN001, ANN003
        hook = kwargs.get("on_usage")
        if hook:
            hook({"prompt_tokens": 500, "completion_tokens": 100})
        return "摘要"


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_group_text(self, group_id, text, reply_to=None):  # noqa: ANN001
        self.sent.append(text)
        return f"mid-{len(self.sent)}"

    async def send_private_text(self, user_id, text, reply_to=None):  # noqa: ANN001
        self.sent.append(text)
        return f"mid-{len(self.sent)}"

    async def send_group_image(self, group_id, image):  # noqa: ANN001
        return "img-1"


def make_settings(
    db_path: Path,
    *,
    budget: float = 1.0,
    burst: int = 0,
    limits: dict[int, float] | None = None,
    admin: list[int] | None = None,
) -> Settings:
    settings = Settings.from_dict(
        {
            "deepseek": {"api_key": "test-key", "enable_tools": False},
            "persona": {"name": "蓝色大肥鱼", "file": "config/persona-whale.md"},
            "chat": {
                "group": {
                    "ambient_chance": 0,
                    "cooldown": 0,
                    "per_minute": 100,
                    "burst_limit": burst,
                    "burst_window": 60,
                    "burst_pause": 15,
                },
                "memory": {"db_path": str(db_path)},
                "budget": {
                    "enabled": True,
                    "daily_cny_per_group": budget,
                    "group_limits": limits or {},
                    "db_path": str(db_path.with_name("budget.db")),
                },
                "admin": {"qq": admin or []},
            },
        }
    )
    return settings


def incoming(
    mid: str = "1",
    *,
    user: int = 42,
    text: str = "在吗",
    kind: str = "group",
    group_id: int | None = 123,
) -> Incoming:
    return Incoming(
        kind=kind,
        session_id=f"group:{group_id}" if kind == "group" else f"private:{user}",
        user_id=user,
        message_id=mid,
        nickname="群友",
        text=text,
        at_self=True,
        group_id=group_id if kind == "group" else None,
    )


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    def test_price_estimate_and_peak(self):
        cfg = BudgetConfig(
            enabled=True,
            input_cny_per_million=1.1,
            cached_input_cny_per_million=0.022,
            output_cny_per_million=4.35,
            peak_multiplier=2.0,
        )
        usage = {
            "prompt_tokens": 1000,
            "completion_tokens": 100,
            "prompt_cache_hit_tokens": 400,
        }
        off_peak = datetime(2026, 10, 21, 16, 0, tzinfo=timezone.utc)  # 周三 16 点
        peak = datetime(2026, 10, 21, 2, 0, tzinfo=timezone.utc)  # 周三 02 点
        self.assertFalse(is_peak(off_peak))
        self.assertTrue(is_peak(peak))
        cost_off = estimate_cny(usage, cfg, off_peak)
        cost_peak = estimate_cny(usage, cfg, peak)
        self.assertAlmostEqual(cost_off, (600 * 1.1 + 400 * 0.022 + 100 * 4.35) / 1e6)
        self.assertAlmostEqual(cost_peak, cost_off * 2)
        # 周末不算高峰
        self.assertFalse(
            is_peak(datetime(2026, 10, 24, 2, 0, tzinfo=timezone.utc))
        )

    async def test_store_persists_daily_spend(self):
        folder = temp_dir("budget-store")
        try:
            cfg = BudgetConfig(
                enabled=True,
                daily_cny_per_group=0.3,
                db_path=str(folder / "budget.db"),
                input_cny_per_million=1.0,
                cached_input_cny_per_million=0.0,
                output_cny_per_million=2.0,
                peak_multiplier=1.0,
            )
            store = BudgetStore(cfg)
            spent = store.add_usage(123, {"prompt_tokens": 100_000, "completion_tokens": 50_000})
            self.assertGreater(spent, 0)
            self.assertAlmostEqual(store.today_cny(123), spent, places=9)
            self.assertAlmostEqual(store.remaining(123), 0.3 - spent, places=9)
            store.close()
            again = BudgetStore(cfg)
            self.assertAlmostEqual(again.today_cny(123), spent, places=9)
            again.close()
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    async def test_group_limit_override(self):
        """按群单独额度：100000001 可以比全局 0.3 更高。"""
        folder = temp_dir("budget-limit")
        try:
            cfg = BudgetConfig(
                enabled=True,
                daily_cny_per_group=0.3,
                group_limits={123: 1.0},
                db_path=str(folder / "budget.db"),
                input_cny_per_million=1.0,
                cached_input_cny_per_million=0.0,
                output_cny_per_million=1.0,
                peak_multiplier=1.0,
            )
            store = BudgetStore(cfg)
            self.assertEqual(store.limit_for(123), 1.0)
            self.assertEqual(store.limit_for(999), 0.3)
            store.add_usage(123, {"prompt_tokens": 500_000, "completion_tokens": 0})
            self.assertGreater(store.remaining(123), 0)
            self.assertEqual(store.group_summary(123)["limit_cny"], 1.0)
            store.close()
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    async def test_reset_today_only_one_group(self):
        """重置额度只清目标群，其他群和配置都不动。"""
        folder = temp_dir("budget-reset")
        try:
            cfg = BudgetConfig(
                enabled=True,
                daily_cny_per_group=0.3,
                group_limits={123: 1.0},
                db_path=str(folder / "budget.db"),
                input_cny_per_million=1.0,
                cached_input_cny_per_million=0.0,
                output_cny_per_million=1.0,
                peak_multiplier=1.0,
            )
            store = BudgetStore(cfg)
            store.add_usage(123, {"prompt_tokens": 500_000, "completion_tokens": 0})
            store.add_usage(999, {"prompt_tokens": 500_000, "completion_tokens": 0})
            other_before = store.today_cny(999)
            self.assertGreater(store.today_cny(123), 0)
            store.reset_today(123)
            self.assertEqual(store.today_cny(123), 0.0)
            self.assertEqual(store.today_cny(999), other_before)
            self.assertNotIn(123, store.spent_groups())
            self.assertIn(999, store.spent_groups())
            self.assertEqual(store.limit_for(123), 1.0)
            store.close()
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    async def test_budget_uses_group_limit(self):
        folder = temp_dir("budget-limit-chat")
        memory = MemoryStore(folder / "bot.db")
        await memory.init()
        try:
            settings = make_settings(
                folder / "bot.db", budget=0.3, limits={123: 2.0}
            )
            llm = FakeLLM()
            sender = FakeSender()
            service = ChatService(settings, llm=llm, sender=sender, memory=memory)
            # 群 123 花了 0.5（高峰时段翻倍约 1.1）：超过全局 0.3，但没超过专属 2.0，应该继续回
            service._record_budget(123, {"prompt_tokens": 500_000, "completion_tokens": 0})
            self.assertTrue(service._budget_allows(123))
            # 群 999 没有专属额度，0.4 已经超 0.3，应该拦
            service._record_budget(999, {"prompt_tokens": 400_000, "completion_tokens": 0})
            self.assertFalse(service._budget_allows(999))
            await service.shutdown()
        finally:
            await memory.close()
            shutil.rmtree(folder, ignore_errors=True)

    async def test_budget_blocks_reply_and_notifies_once(self):
        folder = temp_dir("budget-chat")
        memory = MemoryStore(folder / "bot.db")
        await memory.init()
        try:
            settings = make_settings(folder / "bot.db", budget=0.000001)
            llm = FakeLLM()
            sender = FakeSender()
            service = ChatService(settings, llm=llm, sender=sender, memory=memory)
            # 先记一笔花费，直接顶到上限
            service._record_budget(123, {"prompt_tokens": 1_000_000, "completion_tokens": 0})
            await service.handle(incoming("1"))
            self.assertEqual(llm.calls, 0)
            self.assertTrue(any("额度" in text for text in sender.sent))
            # 同一天只提醒一次
            before = len(sender.sent)
            await service.handle(incoming("2"))
            self.assertEqual(len(sender.sent), before)
            self.assertEqual(llm.calls, 0)
            await service.shutdown()
        finally:
            await memory.close()
            shutil.rmtree(folder, ignore_errors=True)

    async def test_usage_is_recorded_per_group(self):
        folder = temp_dir("budget-record")
        memory = MemoryStore(folder / "bot.db")
        await memory.init()
        try:
            settings = make_settings(folder / "bot.db", budget=1.0)
            llm = FakeLLM()
            sender = FakeSender()
            service = ChatService(settings, llm=llm, sender=sender, memory=memory)
            await service.handle(incoming("1"))
            self.assertEqual(llm.calls, 1)
            self.assertGreater(service._budget.today_cny(123), 0)
            await service.shutdown()
        finally:
            await memory.close()
            shutil.rmtree(folder, ignore_errors=True)

    async def test_reset_all_today_clears_every_group(self):
        """reset_all_today 清掉今天所有群；没有花费时返回空字典。"""
        folder = temp_dir("budget-reset-all")
        try:
            cfg = BudgetConfig(
                enabled=True,
                daily_cny_per_group=0.3,
                db_path=str(folder / "budget.db"),
                input_cny_per_million=1.0,
                cached_input_cny_per_million=0.0,
                output_cny_per_million=1.0,
                peak_multiplier=1.0,
            )
            store = BudgetStore(cfg)
            store.add_usage(123, {"prompt_tokens": 500_000, "completion_tokens": 0})
            store.add_usage(999, {"prompt_tokens": 500_000, "completion_tokens": 0})
            before = store.reset_all_today()
            self.assertEqual(set(before), {123, 999})
            self.assertEqual(store.spent_groups(), {})
            self.assertEqual(store.today_cny(123), 0.0)
            self.assertEqual(store.reset_all_today(), {})
            store.close()
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    async def test_owner_resets_today_budget_from_chat(self):
        """主人发命令/大白话都能重置额度；普通群友和疑问句不触发。"""
        folder = temp_dir("budget-reset-chat")
        memory = MemoryStore(folder / "bot.db")
        await memory.init()
        try:
            settings = make_settings(folder / "bot.db", budget=0.3, admin=[10001])
            llm = FakeLLM()
            sender = FakeSender()
            service = ChatService(settings, llm=llm, sender=sender, memory=memory)
            try:
                service._record_budget(
                    123, {"prompt_tokens": 500_000, "completion_tokens": 0}
                )
                service._record_budget(
                    999, {"prompt_tokens": 500_000, "completion_tokens": 0}
                )

                # 1) 群里发命令：只清本群，不经过模型
                await service.handle(incoming("1", user=10001, text="/重置额度"))
                self.assertEqual(service._budget.today_cny(123), 0.0)
                self.assertGreater(service._budget.today_cny(999), 0)
                self.assertTrue(any("已重置" in text for text in sender.sent))
                self.assertEqual(llm.calls, 0)

                # 2) 群里说「重置额度 全部」：清全部群
                service._record_budget(
                    123, {"prompt_tokens": 500_000, "completion_tokens": 0}
                )
                await service.handle(
                    incoming("2", user=10001, text="/重置额度 全部")
                )
                self.assertEqual(service._budget.today_cny(123), 0.0)
                self.assertEqual(service._budget.today_cny(999), 0.0)
                self.assertTrue(any("全部群" in text for text in sender.sent))

                # 3) 私聊一句大白话：默认清全部群
                service._record_budget(
                    999, {"prompt_tokens": 500_000, "completion_tokens": 0}
                )
                await service.handle(
                    incoming(
                        "3", user=10001, kind="private", text="把今天的额度重置一下"
                    )
                )
                self.assertEqual(service._budget.today_cny(999), 0.0)

                # 4) 普通群友发同样命令：不生效
                service._record_budget(
                    123, {"prompt_tokens": 500_000, "completion_tokens": 0}
                )
                await service.handle(incoming("4", user=20001, text="/重置额度"))
                self.assertGreater(service._budget.today_cny(123), 0)

                # 5) 疑问句不当成命令，仍按普通聊天交给模型（并正常产生新花费）
                service._budget.reset_today(123)
                calls_before = llm.calls
                await service.handle(
                    incoming("5", user=10001, text="这个额度是怎么算的？")
                )
                self.assertGreater(llm.calls, calls_before)
                self.assertGreater(service._budget.today_cny(123), 0)
            finally:
                await service.shutdown()
        finally:
            await memory.close()
            shutil.rmtree(folder, ignore_errors=True)


class BurstTests(unittest.IsolatedAsyncioTestCase):
    async def test_burst_pauses_and_resumes(self):
        folder = temp_dir("burst")
        memory = MemoryStore(folder / "bot.db")
        await memory.init()
        try:
            settings = make_settings(folder / "bot.db", burst=2)
            llm = FakeLLM()
            sender = FakeSender()
            service = ChatService(settings, llm=llm, sender=sender, memory=memory)
            self.assertTrue(service._burst_allows(123))
            service._note_reply(123)
            service._note_reply(123)
            self.assertFalse(service._burst_allows(123))
            self.assertGreater(service._burst_paused_until[123], 0)
            # 暂停期内直接跳过，不调模型
            await service.handle(incoming("1"))
            self.assertEqual(llm.calls, 0)
            # 暂停结束、旧回复过期后恢复
            service._burst_paused_until[123] = 0
            service._reply_stamps[123].clear()
            self.assertTrue(service._burst_allows(123))
            await service.shutdown()
        finally:
            await memory.close()
            shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
