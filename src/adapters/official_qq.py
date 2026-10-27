"""Phase 4.4：QQ 机器人开放平台适配层（实验性）。

官方接口与 NapCat 的差别：
- 只能收到「群里 @ 机器人」和「私聊」消息，群里唤醒词 / 闲聊 / 主动发言不会触发；
- 群里/用户用 openid 标识，本项目内部用稳定哈希（int）当 group_id / user_id，
  发送时再映射回 openid；
- 需要 AppID / AppSecret，并在开放平台配置 IP 白名单与消息权限。

协议流程（QQ 机器人 API v2）：
1. POST /app/getAppAccessToken 换 access_token；
2. WebSocket 网关：收 Hello(op10) → 发 Identify(op2) → 心跳(op1)；
3. op0 的 GROUP_AT_MESSAGE_CREATE / C2C_MESSAGE_CREATE 转成 Incoming；
4. 发送走 REST：/v2/groups/{group_openid}/messages 或 /v2/users/{user_openid}/messages。

本适配器不实现 resume/shard，断线直接重连（官方网关允许重新 identify）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable

import httpx

try:
    from websockets.asyncio.client import connect
except ImportError:  # pragma: no cover
    from websockets import connect  # type: ignore

from config import OfficialConfig
from onebot import Incoming

log = logging.getLogger("official")

GROUP_EVENT = "GROUP_AT_MESSAGE_CREATE"
C2C_EVENT = "C2C_MESSAGE_CREATE"
_EVENT_TYPES = {GROUP_EVENT, C2C_EVENT}
_MENTION = re.compile(r"<@!?\d+>")
_PREFIX = "QQBot"


class OfficialError(RuntimeError):
    """官方接口调用失败。"""


def stable_id(namespace: str, value: str) -> int:
    """openid -> 稳定的 int（会话/群/用户内部 id，发送时再映射回去）。"""
    digest = hashlib.sha1(f"{namespace}:{value}".encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def to_incoming(event_type: str, data: dict[str, Any]) -> Incoming | None:
    """把官方事件转成统一的 Incoming；不认识的类型返回 None。"""
    if event_type not in _EVENT_TYPES:
        return None
    message_id = str(data.get("id") or "")
    if not message_id:
        return None
    author = data.get("author") or {}
    content = _MENTION.sub("", str(data.get("content") or "")).strip()
    if not content:
        content = "[空消息]"

    if event_type == GROUP_EVENT:
        group_openid = str(data.get("group_openid") or "")
        member_openid = str(author.get("member_openid") or author.get("id") or "")
        if not group_openid or not member_openid:
            return None
        return Incoming(
            kind="group",
            session_id=f"group:{stable_id('group', group_openid)}",
            user_id=stable_id("user", member_openid),
            message_id=message_id,
            nickname=str(author.get("username") or "群友"),
            text=content,
            at_self=True,  # 官方群消息本身就是 @ 机器人才会推送
            group_id=stable_id("group", group_openid),
            images=[],
            raw=data,
        )

    user_openid = str(author.get("user_openid") or author.get("id") or "")
    if not user_openid:
        return None
    return Incoming(
        kind="private",
        session_id=f"private:{stable_id('user', user_openid)}",
        user_id=stable_id("user", user_openid),
        message_id=message_id,
        nickname=str(author.get("username") or "私聊用户"),
        text=content,
        at_self=False,
        images=[],
        raw=data,
    )


class OfficialQQClient:
    """官方 QQ 机器人客户端：WebSocket 收事件 + REST 发消息。"""

    def __init__(
        self, cfg: OfficialConfig, http: httpx.AsyncClient | None = None
    ):
        self.cfg = cfg
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(cfg.request_timeout, connect=10.0)
        )
        self._handler: Callable[[Incoming], Awaitable[None]] | None = None
        self._group_openids: dict[int, str] = {}
        self._user_openids: dict[int, str] = {}
        self._token = ""
        self._token_expires = 0.0
        self._gateway_token = ""
        self._ws: Any = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._seq: int | None = None
        self._msg_seq = 0
        self._stopped = asyncio.Event()

    # -- 生命周期 ---------------------------------------------------------
    def set_handler(self, handler: Callable[[Incoming], Awaitable[None]]) -> None:
        self._handler = handler

    async def run(self) -> None:
        if not self.cfg.configured:
            raise OfficialError(
                "官方接口未配置完整：需要 official.enabled + app_id + app_secret"
            )
        while not self._stopped.is_set():
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("官方网关断开/失败：%s", exc)
            finally:
                self._ws = None
                self._stop_heartbeat()
            if self._stopped.is_set():
                break
            await asyncio.sleep(self.cfg.reconnect_interval)

    async def close(self) -> None:
        self._stopped.set()
        self._stop_heartbeat()
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # pragma: no cover
                pass
        for task in list(self._tasks):
            task.cancel()

    # -- WebSocket --------------------------------------------------------
    async def _connect_once(self) -> None:
        token = await self._access_token()
        self._gateway_token = token
        headers = {"Authorization": f"{_PREFIX} {token}"}
        async with connect(
            self.cfg.gateway_url,
            additional_headers=headers,
            max_size=8 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        ) as ws:
            self._ws = ws
            log.info("已连接官方 QQ 网关：%s", self.cfg.gateway_url)
            async for raw in ws:
                await self._on_payload(ws, raw)

    async def _on_payload(self, ws: Any, raw: str | bytes) -> None:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return
        op = payload.get("op")
        if op == 10:  # Hello
            interval = float((payload.get("d") or {}).get("heartbeat_interval") or 30000) / 1000
            await ws.send(
                json.dumps(
                    {
                        "op": 2,
                        "d": {
                            "token": f"{_PREFIX} {self._gateway_token}",
                            "intents": self.cfg.intents,
                            "shard": [0, 1],
                        },
                    }
                )
            )
            self._start_heartbeat(ws, interval)
        elif op == 0:  # Dispatch
            self._seq = payload.get("s") or self._seq
            event_type = str(payload.get("t") or "")
            if event_type in _EVENT_TYPES:
                await self._dispatch(event_type, payload.get("d") or {})
        elif op == 7:  # Reconnect
            log.info("官方网关要求重连")
            await ws.close()
        elif op == 9:  # Invalid Session
            log.warning("官方网关会话失效，重新获取 token")
            self._token = ""
            await ws.close()

    def _start_heartbeat(self, ws: Any, interval: float) -> None:
        self._stop_heartbeat()
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(ws, max(5.0, interval))
        )

    def _stop_heartbeat(self) -> None:
        task, self._heartbeat_task = self._heartbeat_task, None
        if task is not None:
            task.cancel()

    async def _heartbeat_loop(self, ws: Any, interval: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                await ws.send(json.dumps({"op": 1, "d": self._seq}))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - 连接断开时由外层重连
            log.debug("心跳停止：%s", exc)

    # -- 事件分发 ---------------------------------------------------------
    async def _dispatch(self, event_type: str, data: dict[str, Any]) -> None:
        incoming = to_incoming(event_type, data)
        if incoming is None or self._dedup(incoming.message_id):
            return
        self.remember_target(incoming)
        if self._handler is None:
            return
        task = asyncio.create_task(self._safe_handle(incoming))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _safe_handle(self, incoming: Incoming) -> None:
        assert self._handler is not None
        try:
            await self._handler(incoming)
        except Exception:  # pragma: no cover
            log.exception("处理官方消息出错：%s", incoming.message_id)

    def remember_target(self, incoming: Incoming) -> None:
        """记住内部 id 与 openid 的映射，发送时要用。"""
        if incoming.kind == "group" and incoming.group_id is not None:
            openid = str(incoming.raw.get("group_openid") or "")
            if openid:
                self._group_openids[incoming.group_id] = openid
        elif incoming.kind == "private":
            author = incoming.raw.get("author") or {}
            openid = str(author.get("user_openid") or author.get("id") or "")
            if openid:
                self._user_openids[incoming.user_id] = openid

    def _dedup(self, message_id: str) -> bool:
        if message_id in self._seen:
            return True
        self._seen[message_id] = time.monotonic()
        while len(self._seen) > 500:
            self._seen.popitem(last=False)
        return False

    # -- Token / REST -----------------------------------------------------
    async def _access_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expires - 60:
            return self._token
        response = await self._http.post(
            self.cfg.token_url,
            json={"appId": self.cfg.app_id, "clientSecret": self.cfg.app_secret},
        )
        if response.status_code != 200:
            raise OfficialError(
                f"获取 access_token 失败：HTTP {response.status_code} "
                f"{response.text[:200]}"
            )
        data = response.json()
        token = str(data.get("access_token") or "")
        if not token:
            raise OfficialError(f"access_token 返回异常：{str(data)[:200]}")
        self._token = token
        self._token_expires = now + float(data.get("expires_in") or 7200)
        return token

    async def _api(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        token = await self._access_token()
        response = await self._http.request(
            method,
            f"{self.cfg.api_base}{path}",
            headers={
                "Authorization": f"{_PREFIX} {token}",
                "X-Union-Appid": self.cfg.app_id,
            },
            json=json_body,
        )
        if response.status_code not in {200, 201, 202, 204}:
            raise OfficialError(
                f"{method} {path} 失败：HTTP {response.status_code} "
                f"{response.text[:200]}"
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    # -- 发送（实现 SenderLike） ------------------------------------------
    async def send_group_text(
        self, group_id: int, text: str, reply_to: str | None = None
    ) -> str | None:
        openid = self._group_openids.get(int(group_id))
        if not openid:
            raise OfficialError("未知群：请先收到该群一条 @ 消息")
        self._msg_seq += 1
        payload: dict[str, Any] = {
            "content": text,
            "msg_type": 0,
            "msg_seq": self._msg_seq,
        }
        if reply_to:
            payload["msg_id"] = reply_to
        data = await self._api(
            "POST", f"/v2/groups/{openid}/messages", json_body=payload
        )
        return str(data.get("id") or "") or None

    async def send_private_text(
        self, user_id: int, text: str, reply_to: str | None = None
    ) -> str | None:
        openid = self._user_openids.get(int(user_id))
        if not openid:
            raise OfficialError("未知用户：请先收到该用户一条私聊")
        self._msg_seq += 1
        payload: dict[str, Any] = {
            "content": text,
            "msg_type": 0,
            "msg_seq": self._msg_seq,
        }
        if reply_to:
            payload["msg_id"] = reply_to
        data = await self._api(
            "POST", f"/v2/users/{openid}/messages", json_body=payload
        )
        return str(data.get("id") or "") or None

    async def aclose(self) -> None:
        await self._http.aclose()
