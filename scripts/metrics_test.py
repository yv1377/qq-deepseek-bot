"""Phase 4.3 用量/耗时统计测试（不联网）。

覆盖：用量记录与聚合、按天补零、清理、LLM usage 回调、`/status` 展示、WebUI `/api/usage`。
运行：python scripts/metrics_test.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from chat import ChatService  # noqa: E402
from config import DeepSeekConfig, Settings  # noqa: E402
from llm import DeepSeekClient  # noqa: E402
from memory import MemoryStore  # noqa: E402
from metrics import MetricsStore  # noqa: E402
from onebot import parse_incoming  # noqa: E402
from webui import create_app  # noqa: E402

PASSED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if not condition:
        raise AssertionError(f"FAIL: {name} {detail}")
    PASSED += 1
    print(f"  ok  {name}")


class StubLLM:
    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return "在呢"

    async def complete(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return {"role": "assistant", "content": "在呢"}

    async def summarize(self, old_summary, transcript):  # noqa: ANN001
        return "摘要"


class StubSender:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_group_text(self, group_id, text, reply_to=None):  # noqa: ANN001
        self.sent.append(text)
        return "1"

    async def send_private_text(self, user_id, text, reply_to=None):  # noqa: ANN001
        self.sent.append(text)
        return "1"


def make_settings(db_path: Path) -> Settings:
    return Settings.from_dict(
        {
            "deepseek": {"api_key": "test-key"},
            "persona": {"name": "大肥鱼", "file": "config/persona.md"},
            "web": {"enabled": True, "token": "test-token"},
            "metrics": {"enabled": True, "db_path": str(db_path)},
            "chat": {
                "group": {"cooldown": 0.0, "ambient_chance": 0.0},
                "admin": {"qq": [10001]},
                "memory": {"db_path": str(db_path), "summary_trigger": 100},
            },
            "log_level": "WARNING",
        }
    )


def group_event(text: str, *, mid: int = 1, user: int = 10001) -> dict:
    return {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": mid,
        "group_id": 123456,
        "user_id": user,
        "self_id": 999,
        "message": [{"type": "text", "data": {"text": text}}],
        "sender": {"user_id": user, "nickname": "管理员"},
    }


async def run() -> None:
    data_dir = ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    tag = uuid.uuid4().hex[:8]
    db_path = data_dir / f"metrics-{tag}.db"
    bot_db = data_dir / f"metrics-bot-{tag}.db"

    print("[1] 配置")
    settings = make_settings(db_path)
    check("配置解析 metrics", settings.metrics.enabled and settings.metrics.keep_days == 30)

    print("[2] 记录与聚合")
    metrics = MetricsStore(db_path)
    metrics.init()
    metrics.record(
        kind="chat",
        model="deepseek-chat",
        prompt_tokens=100,
        completion_tokens=50,
        duration_ms=1200,
    )
    metrics.record(
        kind="summary",
        model="deepseek-chat",
        prompt_tokens=200,
        completion_tokens=80,
        duration_ms=800,
        ok=False,
    )
    today = metrics.today_summary()
    check("调用次数", today["calls"] == 2)
    check("token 汇总", today["prompt_tokens"] == 300 and today["completion_tokens"] == 130)
    check("总 token", today["total_tokens"] == 430)
    check("平均耗时", today["avg_duration_ms"] == 1000.0)
    check("错误计数", today["errors"] == 1)

    daily = metrics.daily(3)
    check("按天补零", len(daily) == 3)
    check("今天桶有数据", daily[-1]["calls"] == 2 and daily[-1]["total_tokens"] == 430)
    check("历史桶为 0", daily[0]["calls"] == 0)

    three_days_ago = today["date"]
    old_ts = time.time() - 3 * 86400
    metrics.record(kind="chat", model="deepseek-chat", total_tokens=10, ts=old_ts)
    check("旧记录可按天查到", any(row["total_tokens"] == 10 for row in metrics.daily(7)))
    removed = metrics.prune(1)
    check("按保留天数清理", removed == 1)
    check("清理后旧记录消失", all(row["total_tokens"] != 10 for row in metrics.daily(7)))

    print("[3] LLM usage 回调")
    records: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "你好"}}],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 7,
                    "total_tokens": 19,
                },
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        llm = DeepSeekClient(
            DeepSeekConfig(api_key="test-key", max_retries=0), http=http
        )
        llm.set_usage_listener(records.append)
        check("chat 返回文本", await llm.chat([{"role": "user", "content": "hi"}]) == "你好")
        await llm.summarize("", "小明喜欢猫")

    check("记录两次调用", len(records) == 2)
    check("token 字段解析", records[0]["prompt_tokens"] == 12 and records[0]["total_tokens"] == 19)
    check("区分调用类型", records[0]["kind"] == "chat" and records[1]["kind"] == "summary")
    check("耗时非负", all(record["duration_ms"] >= 0 for record in records))

    print("[4] /status 展示")
    memory = MemoryStore(bot_db)
    await memory.init()
    sender = StubSender()
    service = ChatService(
        make_settings(bot_db),
        llm=StubLLM(),
        sender=sender,
        memory=memory,
        persona="你是大肥鱼。",
        metrics=metrics,
    )
    await service.handle(parse_incoming(group_event("/status"), 999))
    check("状态包含今日用量", any("今日模型调用" in text for text in sender.sent))

    print("[5] WebUI /api/usage")
    app_settings = make_settings(bot_db)
    app_settings.web.token = "test-token"
    app = create_app(service, app_settings, metrics=metrics)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/usage?days=7", headers={"Authorization": "Bearer test-token"}
        )
        check("用量接口可访问", response.status_code == 200)
        body = response.json()
        check("返回今日汇总", body["today"]["calls"] == 2)
        check("返回 7 天数据", len(body["daily"]) == 7)

    no_metrics_app = create_app(service, app_settings, metrics=None)
    transport2 = httpx.ASGITransport(app=no_metrics_app)
    async with httpx.AsyncClient(transport=transport2, base_url="http://test") as client:
        body = (
            await client.get(
                "/api/usage", headers={"Authorization": "Bearer test-token"}
            )
        ).json()
        check("未启用时返回 enabled=false", body["enabled"] is False)

    await service.shutdown()
    await memory.close()
    metrics.close()
    for path in data_dir.glob(f"metrics-{tag}.db*"):
        try:
            path.unlink()
        except OSError:
            pass
    for path in data_dir.glob(f"metrics-bot-{tag}.db*"):
        try:
            path.unlink()
        except OSError:
            pass

    print(f"\n用量统计测试通过：{PASSED} 项检查")


if __name__ == "__main__":
    asyncio.run(run())
