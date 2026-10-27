"""Phase 4.1 WebUI 测试：不连 QQ / 不占端口，用 httpx ASGITransport 直接打 FastAPI。

运行：
    python scripts/webui_test.py
"""
from __future__ import annotations

import asyncio
import socket
import sys
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from chat import ChatService  # noqa: E402
from config import Settings  # noqa: E402
from memory import MemoryStore  # noqa: E402
from onebot import parse_incoming  # noqa: E402
from webui import create_app, start_webui  # noqa: E402

PASSED = 0
TOKEN = "secret-token"


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if not condition:
        raise AssertionError(f"FAIL: {name} {detail}")
    PASSED += 1
    print(f"  ok  {name}")


class StubLLM:
    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return "在呢在呢"

    async def complete(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return {"role": "assistant", "content": "在呢在呢"}

    async def summarize(self, old_summary, transcript):  # noqa: ANN001
        return "摘要"


class StubSender:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._next_id = 7000

    async def send_group_text(self, group_id, text, reply_to=None):  # noqa: ANN001
        self._next_id += 1
        self.sent.append(
            {"kind": "group", "target": group_id, "text": text, "id": str(self._next_id)}
        )
        return str(self._next_id)

    async def send_private_text(self, user_id, text, reply_to=None):  # noqa: ANN001
        self._next_id += 1
        self.sent.append(
            {"kind": "private", "target": user_id, "text": text, "id": str(self._next_id)}
        )
        return str(self._next_id)


def free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def make_settings(db_path: Path) -> Settings:
    return Settings.from_dict(
        {
            "deepseek": {"api_key": "test-key"},
            "onebot": {"ws_url": "ws://127.0.0.1:3001"},
            "persona": {"name": "大肥鱼", "file": "config/persona.md"},
            "web": {"enabled": True, "host": "127.0.0.1", "port": 8080, "token": TOKEN},
            "chat": {
                "group": {
                    "wake_words": ["大肥鱼"],
                    "ambient_chance": 0.0,
                    "cooldown": 0.0,
                    "per_minute": 100,
                },
                "reply": {"chunk_delay": 0.0},
                "memory": {"db_path": str(db_path), "summary_trigger": 100},
                "anti_spam": {"enabled": False},  # 本测试只关心 WebUI/静音
                "admin": {"qq": []},
            },
            "log_level": "WARNING",
        }
    )


def group_event(
    text: str, *, mid: int, group: int = 123456, user: int = 20001
) -> dict:    return {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": mid,
        "group_id": group,
        "user_id": user,
        "self_id": 999,
        "message": [
            {"type": "at", "data": {"qq": "999"}},
            {"type": "text", "data": {"text": text}},
        ],
        "sender": {"user_id": user, "nickname": "小明", "card": "小明"},
    }


async def main() -> None:
    data_dir = ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    tag = uuid.uuid4().hex[:8]
    db_path = data_dir / f"webui-{tag}.db"

    settings = make_settings(db_path)
    memory = MemoryStore(db_path)
    await memory.init()
    sender = StubSender()
    service = ChatService(
        settings, llm=StubLLM(), sender=sender, memory=memory, persona="你是大肥鱼。"
    )
    app = create_app(service, settings)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {TOKEN}"}

        print("[1] 鉴权与页面")
        check(
            "无 token 被拒绝",
            (await client.get("/api/status")).status_code == 401,
        )
        status = await client.get("/api/status", headers=headers)
        check("带 token 可访问", status.status_code == 200)
        body = status.json()
        check("状态包含模型", body["model"] == "deepseek-chat")
        page = await client.get("/")
        check("首页可访问", page.status_code == 200 and "控制台" in page.text)

        print("[2] 会话与记忆")
        await memory.add_note("group:123456", "本群周五有活动")
        await service.handle(parse_incoming(group_event("在吗", mid=1), 999))
        sessions = (await client.get("/api/sessions", headers=headers)).json()
        check(
            "会话列表包含群",
            any(row["session_id"] == "group:123456" for row in sessions["sessions"]),
        )
        detail = (
            await client.get(
                "/api/session", params={"session_id": "group:123456"}, headers=headers
            )
        ).json()
        check("会话详情有消息", len(detail["messages"]) == 2)
        check("会话详情有笔记", "本群周五有活动" in detail["notes"])

        await memory.add_user_note(20001, "小明喜欢猫")
        user_notes = (
            await client.get(
                "/api/user_notes", params={"user_id": 20001}, headers=headers
            )
        ).json()
        check("用户笔记可查", "小明喜欢猫" in user_notes["notes"])

        print("[3] 静音与恢复")
        sent_before = len(sender.sent)
        await client.post(
            "/api/mute", json={"group_id": 123456, "muted": True}, headers=headers
        )
        check("静音写入运行状态", service.is_group_muted(123456))
        await service.handle(parse_incoming(group_event("在吗", mid=2), 999))
        check("静音后不回复", len(sender.sent) == sent_before)
        status = (await client.get("/api/status", headers=headers)).json()
        check("状态显示静音群", status["muted_groups"] == [123456])
        await client.post(
            "/api/mute", json={"group_id": 123456, "muted": False}, headers=headers
        )
        check("恢复后不再静音", not service.is_group_muted(123456))
        await service.handle(parse_incoming(group_event("在吗", mid=3), 999))
        check("恢复后正常回复", len(sender.sent) > sent_before)

        print("[4] 运行时参数")
        runtime = (
            await client.post(
                "/api/runtime",
                json={"ambient_chance": 0.5, "proactive_enabled": True},
                headers=headers,
            )
        ).json()
        check("运行时参数生效", runtime["ambient_chance"] == 0.5)
        check(
            "主动发言可运行时开启",
            settings.chat.proactive.enabled and service.ambient.chance == 0.5,
        )
        invalid = (
            await client.post(
                "/api/runtime", json={"ambient_chance": 2}, headers=headers
            )
        )
        check("非法概率被拒绝", invalid.status_code == 422)

        print("[5] 清空会话")
        await client.post(
            "/api/session/reset",
            json={"session_id": "group:123456"},
            headers=headers,
        )
        check("会话消息已清空", await memory.count_messages("group:123456") == 0)
        check("会话笔记已清空", await memory.recent_notes("group:123456") == [])

    print("[6] 真实 uvicorn 启停")
    settings.web.port = free_port()
    server = await start_webui(settings, service)
    check("WebUI 服务启动", server is not None)
    if server is not None:
        base_url = f"http://127.0.0.1:{settings.web.port}"
        async with httpx.AsyncClient(base_url=base_url, timeout=5) as real_client:
            real_status = await real_client.get(
                "/api/status", headers={"Authorization": f"Bearer {TOKEN}"}
            )
            check("真实 HTTP 状态接口", real_status.status_code == 200)
            real_page = await real_client.get("/")
            check("真实 HTTP 页面", "控制台" in real_page.text)
        await server.stop()

    await service.shutdown()
    await memory.close()
    for path in data_dir.glob(f"webui-{tag}.db*"):
        try:
            path.unlink()
        except OSError:
            pass

    print(f"\nWebUI 测试通过：{PASSED} 项检查")


if __name__ == "__main__":
    asyncio.run(main())
