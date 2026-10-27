"""Phase 4.4 官方 QQ 适配层测试（不连真网关）。

覆盖：事件转换、openid 哈希映射、Hello/Identify、事件分发与去重、
REST 发送（token 缓存 / 群 / 私聊）、错误处理。
运行：python scripts/official_test.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from adapters.official_qq import (  # noqa: E402
    GROUP_EVENT,
    OfficialError,
    OfficialQQClient,
    stable_id,
    to_incoming,
)
from config import OfficialConfig, Settings  # noqa: E402

PASSED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if not condition:
        raise AssertionError(f"FAIL: {name} {detail}")
    PASSED += 1
    print(f"  ok  {name}")


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self) -> None:
        return None


def group_data() -> dict:
    return {
        "id": "msg-1",
        "content": "<@!123> 在吗",
        "group_openid": "GROUP_OPENID_1",
        "author": {"member_openid": "MEMBER_1", "username": "小明"},
    }


def c2c_data() -> dict:
    return {
        "id": "msg-2",
        "content": "你好",
        "author": {"user_openid": "USER_1", "username": "小张"},
    }


async def run() -> None:
    print("[1] 配置与 id 映射")
    saved = {
        key: os.environ.get(key)
        for key in ("OFFICIAL_APP_ID", "OFFICIAL_APP_SECRET")
    }
    try:
        os.environ["OFFICIAL_APP_ID"] = "app-123"
        os.environ["OFFICIAL_APP_SECRET"] = "secret-456"
        settings = Settings.from_dict({"official": {"enabled": True}})
        check("配置从环境变量读取", settings.official.configured)
        check("默认 intents 群+私聊", settings.official.intents == 1 << 25)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    check("openid 哈希稳定", stable_id("group", "G1") == stable_id("group", "G1"))
    check(
        "不同命名空间不冲突",
        stable_id("group", "G1") != stable_id("user", "G1"),
    )

    print("[2] 事件转换")
    incoming = to_incoming(GROUP_EVENT, group_data())
    assert incoming is not None
    check("群事件 kind", incoming.kind == "group" and incoming.at_self)
    check("去掉 @ 标记", incoming.text == "在吗")
    check("会话 id 由 openid 哈希", incoming.session_id == f"group:{stable_id('group', 'GROUP_OPENID_1')}")
    check("群号与用户号都可用", incoming.group_id is not None and incoming.user_id > 0)
    check("保留原始事件", incoming.raw["group_openid"] == "GROUP_OPENID_1")

    private = to_incoming("C2C_MESSAGE_CREATE", c2c_data())
    assert private is not None
    check("私聊事件", private.kind == "private" and private.text == "你好")
    check("未知事件返回 None", to_incoming("AT_MESSAGE_CREATE", c2c_data()) is None)

    print("[3] 网关握手与事件分发")
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/app/getAppAccessToken":
            return httpx.Response(
                200, json={"access_token": "gateway-token", "expires_in": "7200"}
            )
        if request.url.path.startswith("/v2/"):
            return httpx.Response(200, json={"id": "sent-1"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    cfg = OfficialConfig(
        enabled=True,
        app_id="app-123",
        app_secret="secret-456",
        api_base="https://api.test",
        token_url="https://token.test/app/getAppAccessToken",
        gateway_url="wss://gateway.test/websocket",
    )
    client = OfficialQQClient(cfg, http=http)
    received = []

    async def on_message(item):  # noqa: ANN001
        received.append(item)

    client.set_handler(on_message)
    token = await client._access_token()
    client._gateway_token = token
    ws = FakeWS()
    await client._on_payload(
        ws, json.dumps({"op": 10, "d": {"heartbeat_interval": 60000}})
    )
    identify = next(item for item in ws.sent if item["op"] == 2)
    check("Hello 后发送 Identify", identify["d"]["token"] == "QQBot gateway-token")
    check("Identify 带 intents", identify["d"]["intents"] == cfg.intents)
    client._stop_heartbeat()

    await client._on_payload(
        ws,
        json.dumps({"op": 0, "t": GROUP_EVENT, "s": 1, "d": group_data()}),
    )
    await asyncio.sleep(0)
    check("事件交给 handler", len(received) == 1 and received[0].text == "在吗")
    await client._on_payload(
        ws,
        json.dumps({"op": 0, "t": GROUP_EVENT, "s": 2, "d": group_data()}),
    )
    await asyncio.sleep(0)
    check("重复消息去重", len(received) == 1)

    print("[4] REST 发送")
    incoming = received[0]
    client.remember_target(incoming)
    first = await client.send_group_text(incoming.group_id, "在呢", reply_to=incoming.message_id)
    check("群消息发送成功", first == "sent-1")
    send_requests = [
        request for request in calls if request.url.path.startswith("/v2/groups/")
    ]
    check("发送路径正确", send_requests and "GROUP_OPENID_1" in str(send_requests[0].url))
    check(
        "Authorization 头",
        send_requests[0].headers.get("authorization") == "QQBot gateway-token",
    )
    payload = json.loads(send_requests[0].content)
    check("消息内容与引用", payload["content"] == "在呢" and payload["msg_id"] == "msg-1")

    token_calls_before = sum(
        1 for request in calls if request.url.path == "/app/getAppAccessToken"
    )
    await client.send_group_text(incoming.group_id, "再一句")
    token_calls_after = sum(
        1 for request in calls if request.url.path == "/app/getAppAccessToken"
    )
    check("token 有缓存", token_calls_before == token_calls_after == 1)

    private_incoming = to_incoming("C2C_MESSAGE_CREATE", c2c_data())
    assert private_incoming is not None
    client.remember_target(private_incoming)
    await client.send_private_text(private_incoming.user_id, "私聊回复")
    private_requests = [
        request for request in calls if request.url.path.startswith("/v2/users/")
    ]
    check("私聊发送路径", private_requests and "USER_1" in str(private_requests[0].url))

    try:
        await client.send_group_text(999999, "未知群")
    except OfficialError:
        check("未知目标报错", True)
    else:
        check("未知目标报错", False)

    await client.close()
    await http.aclose()
    print(f"\n官方 QQ 适配层测试通过：{PASSED} 项检查")


if __name__ == "__main__":
    asyncio.run(run())
