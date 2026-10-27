"""聊天可靠性回归测试，不连接 QQ 或模型 API。"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chat import ChatService
from config import Settings
from onebot import Incoming


class ChatRegressionTests(unittest.IsolatedAsyncioTestCase):
    def service(self, sender=None, llm=None):
        settings = Settings()
        settings.chat.private.cooldown = 0
        settings.chat.reply.chunk_delay = 0
        settings.chat.reply.split_chars = 4
        settings.deepseek.enable_tools = False
        memory = AsyncMock()
        memory.get_summary.return_value = ""
        memory.recent_notes.return_value = []
        memory.recent_user_notes.return_value = []
        memory.recent_messages.return_value = []
        memory.count_messages.return_value = 0
        return ChatService(settings, llm=llm or AsyncMock(),
                           sender=sender or AsyncMock(), memory=memory, persona="测试群友")

    def incoming(self):
        return Incoming(kind="private", session_id="private:1", user_id=1,
                        message_id="10", nickname="小明", text="你好")

    async def test_failed_delivery_is_not_remembered(self):
        sender = AsyncMock()
        sender.send_private_text.side_effect = RuntimeError("模拟断线")
        service = self.service(sender=sender)
        service.llm.chat.return_value = "收到你好"
        await service.handle(self.incoming())
        service.memory.add_message.assert_not_awaited()
        self.assertEqual(service.stats["replied"], 0)
        self.assertEqual(service.stats["errors"], 1)

    async def test_partial_delivery_only_remembers_sent_text(self):
        sender = AsyncMock()
        sender.send_private_text.side_effect = ["11", RuntimeError("模拟断线")]
        service = self.service(sender=sender)
        service.llm.chat.return_value = "第一段。第二段。第三段。"
        await service.handle(self.incoming())
        self.assertEqual(sender.send_private_text.await_count, 2)
        self.assertEqual(service.memory.add_message.await_args_list[-1].args[-1], "第一段。")
        self.assertEqual(service.stats["replied"], 1)

    async def test_tool_limit_uses_results_in_final_answer(self):
        service = self.service()
        service.settings.deepseek.enable_tools = True
        service.llm.complete.return_value = {
            "tool_calls": [{"id": "calc", "function": {
                "name": "calculator", "arguments": '{"expression":"6*7"}'}}]}
        service.llm.chat.return_value = "答案是42"
        answer = await service._generate([{"role": "user", "content": "6乘7"}], self.incoming())
        self.assertEqual(answer, "答案是42")
        self.assertEqual(service.llm.complete.await_count, 2)
        messages = service.llm.chat.await_args.args[0]
        self.assertEqual(sum(m["role"] == "tool" for m in messages), 2)
        self.assertIn("42", messages[-1]["content"])

    async def test_shutdown_waits_for_task_cleanup(self):
        service = self.service()
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def background():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        task = asyncio.create_task(background())
        service._background.add(task)
        await started.wait()
        await service.shutdown()
        self.assertTrue(cleaned.is_set())
        self.assertTrue(task.done())


if __name__ == "__main__":
    unittest.main()
