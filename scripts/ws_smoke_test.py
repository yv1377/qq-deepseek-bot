"""OneBot WebSocket 适配层联调测试（不连真 QQ）。

起一个假的 NapCat WebSocket 服务端，让 OneBotClient 连上来，验证：
登录信息获取、事件解析、@ 触发、回复发送（含引用）、echo 回包、优雅退出。

运行：
    python scripts/ws_smoke_test.py
"""
from __future__ import annotations

import asyncio
import json
import socket
import sys
import uuid
from pathlib import Path

from websockets.asyncio.server import serve

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from chat import ChatService  # noqa: E402
from config import Settings  # noqa: E402
from memory import MemoryStore  # noqa: E402
from onebot import OneBotClient  # noqa: E402


class StubLLM:
    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return "在呢在呢，咋了"

    async def complete(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return {"role": "assistant", "content": "在呢在呢，咋了"}

    async def summarize(self, old_summary, transcript):  # noqa: ANN001
        return "摘要"


def free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def make_settings(port: int, db_path: Path) -> Settings:
    return Settings.from_dict(
        {
            "deepseek": {"api_key": "test-key"},
            "onebot": {"ws_url": f"ws://127.0.0.1:{port}", "access_token": ""},
            "persona": {"name": "大肥鱼", "file": "config/persona.md"},
            "chat": {
                "group": {
                    "wake_words": ["大肥鱼"],
                    "ambient_chance": 0.0,
                    "cooldown": 0.0,
                    "per_minute": 100,
                },
                "reply": {"split_chars": 500, "quote": True, "chunk_delay": 0.0},
                "memory": {"db_path": str(db_path), "summary_trigger": 100},
                "admin": {"qq": []},
            },
            "log_level": "WARNING",
        }
    )


async def wait_until(predicate, timeout: float = 5.0) -> bool:  # noqa: ANN001
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(0.02)
    return False


async def main() -> None:
    data_dir = ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    tag = uuid.uuid4().hex[:8]
    db_path = data_dir / f"ws-{tag}.db"
    port = free_port()

    sends: list[dict] = []
    send_done = asyncio.Event()
    event = {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": 101,
        "group_id": 123456,
        "user_id": 20001,
        "self_id": 999,
        "message": [
            {"type": "at", "data": {"qq": "999"}},
            {"type": "text", "data": {"text": "在吗"}},
        ],
        "sender": {"user_id": 20001, "nickname": "小明", "card": "小明"},
    }

    async def ws_handler(ws):  # noqa: ANN001
        async for raw in ws:
            payload = json.loads(raw)
            action = payload.get("action")
            echo = payload.get("echo")
            if action == "get_login_info":
                await ws.send(
                    json.dumps(
                        {
                            "status": "ok",
                            "retcode": 0,
                            "data": {"user_id": 999, "nickname": "大肥鱼"},
                            "echo": echo,
                        }
                    )
                )
                await ws.send(json.dumps(event, ensure_ascii=False))
            elif action == "send_group_msg":
                sends.append(payload)
                await ws.send(
                    json.dumps(
                        {
                            "status": "ok",
                            "retcode": 0,
                            "data": {"message_id": 777},
                            "echo": echo,
                        }
                    )
                )
                send_done.set()

    server = await serve(ws_handler, "127.0.0.1", port)
    settings = make_settings(port, db_path)
    memory = MemoryStore(db_path)
    await memory.init()
    client = OneBotClient(settings.onebot)
    service = ChatService(
        settings, llm=StubLLM(), sender=client, memory=memory, persona="你是大肥鱼。"
    )
    client.set_handler(service.handle)

    run_task = asyncio.create_task(client.run())
    try:
        await asyncio.wait_for(send_done.wait(), timeout=8.0)
        assert client.self_id == 999, f"self_id 解析失败：{client.self_id}"
        assert len(sends) == 1, f"应该发出 1 条消息，实际 {len(sends)}"

        params = sends[0]["params"]
        assert params["group_id"] == 123456
        segments = params["message"]
        assert segments[0] == {"type": "reply", "data": {"id": "101"}}, segments[0]
        assert "在呢" in segments[1]["data"]["text"], segments

        async def memory_ready() -> bool:
            return await memory.count_messages("group:123456") == 2

        assert await wait_until(memory_ready), "记忆没有写入"
        print("  ok  WebSocket 连接与登录信息")
        print("  ok  事件解析与 @ 触发")
        print("  ok  引用回复与 echo 回包")
        print("  ok  回复写入记忆")
    finally:
        await client.close()
        await asyncio.wait_for(run_task, timeout=5.0)
        server.close()
        await server.wait_closed()
        await service.shutdown()
        await memory.close()
        for path in data_dir.glob(f"ws-{tag}.db*"):
            try:
                path.unlink()
            except OSError:
                pass

    print("\nWebSocket 适配层测试通过")


if __name__ == "__main__":
    asyncio.run(main())
