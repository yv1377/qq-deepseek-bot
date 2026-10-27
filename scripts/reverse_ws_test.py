"""Phase 4.5 反向 WebSocket 联调测试（本地真实端口，不连 QQ）。

模拟 NapCat 作为客户端连到机器人的反向 WS 服务：
- 鉴权失败会被拒绝；
- 连接后机器人发 get_login_info，NapCat 回包；
- NapCat 推送群 @ 事件，机器人回复 send_group_msg；
- 校验引用回复与记忆写入。

运行：python scripts/reverse_ws_test.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import sys
import uuid
from pathlib import Path

from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from chat import ChatService  # noqa: E402
from config import Settings  # noqa: E402
from memory import MemoryStore  # noqa: E402
from onebot import OneBotClient  # noqa: E402

PASSED = 0
TOKEN = "reverse-token"


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
        return {"role": "assistant", "content": "在呢"}

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
            "onebot": {
                "mode": "reverse",
                "reverse_host": "127.0.0.1",
                "reverse_port": port,
                "access_token": TOKEN,
                "request_timeout": 5,
            },
            "persona": {"name": "大肥鱼", "file": "config/persona.md"},
            "chat": {
                "group": {"cooldown": 0.0, "ambient_chance": 0.0},
                "reply": {"split_chars": 200, "quote": True, "chunk_delay": 0.0},
                "memory": {"db_path": str(db_path), "summary_trigger": 100},
                "admin": {"qq": []},
            },
            "log_level": "WARNING",
        }
    )


def event() -> dict:
    return {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": 501,
        "group_id": 123456,
        "user_id": 20001,
        "self_id": 999,
        "message": [
            {"type": "at", "data": {"qq": "999"}},
            {"type": "text", "data": {"text": "在吗"}},
        ],
        "sender": {"user_id": 20001, "nickname": "小明", "card": "小明"},
    }


async def fake_napcat(port: int, results: dict) -> None:
    """扮演 NapCat：连机器人、回 API、推事件、收发送请求。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    ws = None
    while ws is None:
        try:
            ws = await connect(
                f"ws://127.0.0.1:{port}",
                additional_headers={
                    "Authorization": f"Bearer {TOKEN}",
                    "X-Self-ID": "999",
                },
            )
        except OSError:
            if loop.time() > deadline:
                raise
            await asyncio.sleep(0.05)
    try:
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
                await ws.send(json.dumps(event(), ensure_ascii=False))
            elif action == "send_group_msg":
                results["send"] = payload
                await ws.send(
                    json.dumps(
                        {
                            "status": "ok",
                            "retcode": 0,
                            "data": {"message_id": 888},
                            "echo": echo,
                        }
                    )
                )
                results["done"].set()
                break
    finally:
        await ws.close()


async def wait_count(memory, session_id: str, expect: int, timeout: float = 3.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await memory.count_messages(session_id) == expect:
            return True
        await asyncio.sleep(0.02)
    return False


async def run() -> None:
    logging.disable(logging.CRITICAL)  # 测试里会故意用错 token
    data_dir = ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    tag = uuid.uuid4().hex[:8]
    db_path = data_dir / f"reverse-{tag}.db"
    port = free_port()

    settings = make_settings(port, db_path)
    memory = MemoryStore(db_path)
    await memory.init()
    client = OneBotClient(settings.onebot)
    service = ChatService(
        settings, llm=StubLLM(), sender=client, memory=memory, persona="你是大肥鱼。"
    )
    client.set_handler(service.handle)
    run_task = asyncio.create_task(client.run())
    results = {"done": asyncio.Event(), "send": None}

    try:
        # 1) 无 token 被拒（等端口起来再连）
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        bad = None
        while bad is None:
            try:
                bad = await connect(f"ws://127.0.0.1:{port}")
            except OSError:
                if loop.time() > deadline:
                    raise
                await asyncio.sleep(0.05)
        rejected = False
        try:
            await asyncio.wait_for(bad.recv(), timeout=2)
        except asyncio.TimeoutError:
            rejected = False
        except Exception:
            rejected = True
        finally:
            await bad.close()
        check("无 token 连接被拒绝", rejected)

        # 2) 正常反向连接 + 事件 + 回复
        napcat = asyncio.create_task(fake_napcat(port, results))
        await asyncio.wait_for(results["done"].wait(), timeout=8)
        await napcat

        send = results["send"]
        check("收到 send_group_msg", send is not None)
        segments = send["params"]["message"]
        check("首条引用原消息", segments[0] == {"type": "reply", "data": {"id": "501"}})
        check("回复内容正确", segments[1]["data"]["text"] == "在呢在呢")
        check("bot self_id 从 X-Self-ID 读取", client.self_id == 999)
        check(
            "记忆写入用户与回复",
            await wait_count(memory, "group:123456", 2),
        )
    finally:
        await client.close()
        try:
            await asyncio.wait_for(run_task, timeout=5)
        except asyncio.TimeoutError:
            run_task.cancel()
        await service.shutdown()
        await memory.close()
        for path in data_dir.glob(f"reverse-{tag}.db*"):
            try:
                path.unlink()
            except OSError:
                pass

    print(f"\n反向 WebSocket 测试通过：{PASSED} 项检查")


if __name__ == "__main__":
    asyncio.run(run())
