"""OneBot v11 适配层。

负责三件事：
1. 连接 NapCat / LLOneBot 的 WebSocket（本项目默认用「正向 WS」：本程序主动连过去）；
2. 把 OneBot 事件解析成统一的 Incoming 结构（文本、@、引用、群号……）；
3. 提供发送消息的 API，并处理 echo 回包、超时、断线重连、消息去重。
"""
from __future__ import annotations

import asyncio
import hmac
import itertools
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

try:  # websockets >= 14 的新 asyncio 客户端
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve
except ImportError:  # pragma: no cover - 兼容旧版本 websockets
    from websockets import connect  # type: ignore
    from websockets import serve  # type: ignore

from config import OneBotConfig

log = logging.getLogger("onebot")

# 消息段里不需要翻译成文字、但仍要占位的类型
_PLACEHOLDERS: dict[str, str] = {
    "image": "[图片]",
    "mface": "[表情]",
    "face": "[表情]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "json": "[卡片消息]",
    "xml": "[卡片消息]",
    "forward": "[合并转发]",
    "dice": "[骰子]",
    "rps": "[猜拳]",
    "poke": "[戳一戳]",
    "shake": "[窗口抖动]",
    "markdown": "[Markdown消息]",
}


class OneBotError(RuntimeError):
    """OneBot 调用失败（未连接 / retcode != 0 / 超时）。"""


@dataclass(slots=True)
class Incoming:
    """一条进来的消息，适配层与业务层之间的唯一数据结构。"""

    kind: str  # "group" / "private"
    session_id: str  # "group:123456" / "private:10001"
    user_id: int
    message_id: str
    nickname: str
    text: str
    at_self: bool = False
    reply_to: str | None = None
    group_id: int | None = None
    is_self: bool = False
    # Phase 3.3：图片 URL（OneBot image 段的 url 字段），供视觉模型下载
    images: list[str] = field(default_factory=list)
    # 原始 file 字段（本地路径 / file:// / base64://），url 缺失时靠它兜底识别
    image_files: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.nickname or str(self.user_id)


def _segments_to_text(
    message: Any, self_id: int | None
) -> tuple[str, bool, str | None, list[str], list[str]]:
    """把消息段数组拼成纯文本，同时取出「是否@我」「回复哪条」「图片 URL / 本地文件」。"""
    if isinstance(message, str):
        # 少数实现会直接给 CQ 码字符串，这里不做完整解析，只当文本
        return message.strip(), False, None, [], []

    parts: list[str] = []
    images: list[str] = []
    files: list[str] = []
    at_self = False
    reply_to: str | None = None
    for segment in message or []:
        if not isinstance(segment, dict):
            continue
        seg_type = str(segment.get("type") or "")
        data = segment.get("data") or {}
        if seg_type == "text":
            parts.append(str(data.get("text") or ""))
        elif seg_type == "at":
            qq = str(data.get("qq") or "")
            if self_id is not None and qq == str(self_id):
                # 只做标记，不往文本里塞"@你"，否则命令解析会被污染
                at_self = True
            else:
                parts.append(f"@{data.get('name') or qq} ")
        elif seg_type == "reply":
            reply_to = str(data.get("id") or "") or None
        elif seg_type in {"image", "mface"}:
            raw_file = str(data.get("file") or "").strip()
            url = str(data.get("url") or raw_file or "").strip()
            if url.startswith(("http://", "https://")):
                images.append(url)
            if raw_file:
                files.append(raw_file)
            # mface 是 QQ 商城表情/表情包，通常带 summary（如「[狗头]」）；
            # 把名字放进文本，即使视觉模型没配置，模型也知道发的是哪个表情。
            summary = str(data.get("summary") or "").strip().strip("[]【】 ")
            if seg_type == "mface":
                parts.append(f"[表情包：{summary}]" if summary else "[表情包]")
            elif "表情" in summary:
                parts.append("[表情包]")
            else:
                parts.append("[图片]")
        elif seg_type in _PLACEHOLDERS:
            parts.append(_PLACEHOLDERS[seg_type])
        else:
            parts.append("")
    return "".join(parts).strip(), at_self, reply_to, images, files


def parse_incoming(event: dict[str, Any], self_id: int | None) -> Incoming | None:
    """把 OneBot v11 的 message 事件转成 Incoming；非消息事件返回 None。"""
    message_type = str(event.get("message_type") or "")
    if message_type not in {"group", "private"}:
        return None

    bot_id = self_id or event.get("self_id")
    try:
        bot_id = int(bot_id) if bot_id is not None else None
    except (TypeError, ValueError):
        bot_id = None

    user_id = int(event.get("user_id") or 0)
    message_id = str(event.get("message_id") or "")
    if not message_id:
        return None

    sender = event.get("sender") or {}
    nickname = str(sender.get("card") or sender.get("nickname") or user_id)
    text, at_self, reply_to, images, image_files = _segments_to_text(event.get("message"), bot_id)
    if not text:
        text = "[空消息]"

    if message_type == "group":
        group_id = int(event.get("group_id") or 0)
        return Incoming(
            kind="group",
            session_id=f"group:{group_id}",
            user_id=user_id,
            message_id=message_id,
            nickname=nickname,
            text=text,
            at_self=at_self,
            reply_to=reply_to,
            group_id=group_id,
            is_self=bot_id is not None and user_id == bot_id,
            images=images,
            image_files=image_files,
            raw=event,
        )

    return Incoming(
        kind="private",
        session_id=f"private:{user_id}",
        user_id=user_id,
        message_id=message_id,
        nickname=nickname,
        text=text,
        at_self=False,
        reply_to=reply_to,
        is_self=bot_id is not None and user_id == bot_id,
        images=images,
        image_files=image_files,
        raw=event,
    )


def parse_poke(event: dict[str, Any], self_id: int | None) -> Incoming | None:
    """QQ「戳一戳」：notice/notify/poke，只有戳到机器人自己才返回事件。"""
    if str(event.get("post_type") or "") != "notice":
        return None
    notice_type = str(event.get("notice_type") or "")
    sub_type = str(event.get("sub_type") or "")
    if notice_type == "notify":
        if sub_type != "poke":
            return None
    elif notice_type != "poke":
        return None

    bot_id = self_id or event.get("self_id")
    try:
        bot_id = int(bot_id) if bot_id is not None else None
        target_id = int(event.get("target_id") or 0)
        user_id = int(event.get("user_id") or 0)
    except (TypeError, ValueError):
        return None
    if bot_id is None or target_id != bot_id:
        return None

    sender = event.get("sender") or {}
    nickname = str(sender.get("card") or sender.get("nickname") or user_id)
    stamp = str(event.get("time") or int(time.time()))
    message_id = f"poke:{stamp}:{user_id}:{target_id}"
    group_id = event.get("group_id")
    if group_id:
        try:
            gid = int(group_id)
        except (TypeError, ValueError):
            return None
        return Incoming(
            kind="group",
            session_id=f"group:{gid}",
            user_id=user_id,
            message_id=message_id,
            nickname=nickname,
            text="[戳一戳]",
            at_self=True,
            group_id=gid,
            raw=event,
        )
    return Incoming(
        kind="private",
        session_id=f"private:{user_id}",
        user_id=user_id,
        message_id=message_id,
        nickname=nickname,
        text="[戳一戳]",
        at_self=True,
        raw=event,
    )


class OneBotClient:
    """OneBot v11 WebSocket 客户端（正向连接 + 自动重连）。"""

    def __init__(self, cfg: OneBotConfig):
        self.cfg = cfg
        self.self_id: int | None = None
        self._handler: Callable[[Incoming], Awaitable[None]] | None = None
        self._ws: Any = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._echo = itertools.count(1)
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopped = asyncio.Event()

    # -- 生命周期 ---------------------------------------------------------
    def set_handler(self, handler: Callable[[Incoming], Awaitable[None]]) -> None:
        self._handler = handler

    async def run(self) -> None:
        """阻塞运行：正向主动连 NapCat，或反向监听 NapCat 来连。"""
        if self.cfg.mode == "reverse":
            await self._run_reverse()
        else:
            await self._run_forward()

    async def _run_forward(self) -> None:
        """正向 WS：连接 -> 收消息 -> 断了就重连，直到 close()。"""
        headers: dict[str, str] = {}
        if self.cfg.access_token:
            headers["Authorization"] = f"Bearer {self.cfg.access_token}"

        while not self._stopped.is_set():
            try:
                async with connect(
                    self.cfg.ws_url,
                    additional_headers=headers or None,
                    max_size=32 * 1024 * 1024,
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    self._ws = ws
                    log.info("已连接 OneBot 服务：%s", self.cfg.ws_url)
                    # 必须先启动收包循环，否则 call() 等回包时会死锁：
                    # 请求发出去了，但没人读 socket。
                    reader = asyncio.create_task(self._reader_loop(ws))
                    try:
                        await self._after_connect()
                        await reader
                    except asyncio.CancelledError:
                        raise
                    finally:
                        reader.cancel()
                        try:
                            await reader
                        except (asyncio.CancelledError, Exception):
                            pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 连接失败 / 被服务端断开
                log.warning("OneBot 连接断开：%s", exc)
            finally:
                self._ws = None
                self._fail_pending("连接已断开")

            if self._stopped.is_set():
                break
            log.info("%.1f 秒后重连……", self.cfg.reconnect_interval)
            try:
                await asyncio.wait_for(
                    self._stopped.wait(), timeout=self.cfg.reconnect_interval
                )
            except asyncio.TimeoutError:
                pass

    async def _run_reverse(self) -> None:
        """反向 WS：本进程监听，NapCat 用「WebSocket 客户端」连过来。"""

        async def handler(ws: Any) -> None:
            await self._handle_reverse(ws)

        async with serve(
            handler,
            self.cfg.reverse_host,
            self.cfg.reverse_port,
            max_size=32 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        ) as server:
            log.info(
                "反向 OneBot 已监听：ws://%s:%s",
                self.cfg.reverse_host,
                self.cfg.reverse_port,
            )
            await self._stopped.wait()
            server.close()
            await server.wait_closed()

    async def _handle_reverse(self, ws: Any) -> None:
        request = getattr(ws, "request", None)
        if request is not None:
            if self.cfg.access_token:
                supplied = request.headers.get("authorization", "")
                expected = f"Bearer {self.cfg.access_token}"
                if not hmac.compare_digest(supplied, expected):
                    log.warning("反向连接鉴权失败，已拒绝")
                    await ws.close(code=1008, reason="invalid token")
                    return
            self_id = request.headers.get("x-self-id")
            if self_id:
                try:
                    self.self_id = int(self_id)
                except (TypeError, ValueError):
                    pass

        old = self._ws
        self._ws = ws
        if old is not None and old is not ws:
            try:
                await old.close()
            except Exception:  # pragma: no cover
                pass
        log.info("NapCat 已反向连接（self_id=%s）", self.self_id)

        reader = asyncio.create_task(self._reader_loop(ws))
        try:
            await self._after_connect()
            await reader
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("反向连接异常：%s", exc)
        finally:
            reader.cancel()
            try:
                await reader
            except (asyncio.CancelledError, Exception):
                pass
            if self._ws is ws:
                self._ws = None
            self._fail_pending("反向连接断开")

    async def close(self) -> None:
        self._stopped.set()
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # pragma: no cover
                pass
        for task in list(self._tasks):
            task.cancel()

    # -- 内部 -------------------------------------------------------------
    async def _reader_loop(self, ws: Any) -> None:
        async for raw in ws:
            await self._on_raw(raw)

    async def _after_connect(self) -> None:
        try:
            info = await self.call("get_login_info")
        except OneBotError as exc:
            log.warning("获取登录信息失败（不影响收消息）：%s", exc)
            return
        if info:
            try:
                self.self_id = int(info.get("user_id") or 0) or None
            except (TypeError, ValueError):
                self.self_id = None
            log.info("机器人账号：%s（%s）", info.get("nickname"), info.get("user_id"))

    async def _on_raw(self, raw: str | bytes) -> None:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return

        # API 调用回包
        if "echo" in data:
            future = self._pending.get(str(data["echo"]))
            if future is not None and not future.done():
                future.set_result(data)
            return

        post_type = data.get("post_type")
        if post_type == "notice":
            poke = parse_poke(data, self.self_id)
            if poke is None or poke.is_self:
                return
            if self._dedup(poke.message_id):
                return
            if self._handler is None:
                return
            task = asyncio.create_task(self._safe_handle(poke))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return
        if post_type != "message":
            return

        incoming = parse_incoming(data, self.self_id)
        if incoming is None or incoming.is_self:
            return
        if self._dedup(incoming.message_id):
            return
        if self._handler is None:
            return

        task = asyncio.create_task(self._safe_handle(incoming))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _safe_handle(self, incoming: Incoming) -> None:
        assert self._handler is not None
        try:
            await self._handler(incoming)
        except Exception:  # 单条消息处理失败不能拖垮收消息循环
            log.exception("处理消息出错：%s", incoming.message_id)

    def _dedup(self, message_id: str) -> bool:
        if message_id in self._seen:
            return True
        self._seen[message_id] = time.monotonic()
        while len(self._seen) > 500:
            self._seen.popitem(last=False)
        return False

    def _fail_pending(self, reason: str) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(OneBotError(reason))
        # 避免 "exception was never retrieved" 警告
        for future in pending.values():
            future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)

    # -- 调用 -------------------------------------------------------------
    async def call(self, action: str, **params: Any) -> Any:
        ws = self._ws
        if ws is None:
            raise OneBotError("尚未连接 OneBot 服务")
        echo = f"req-{next(self._echo)}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[echo] = future
        try:
            await ws.send(
                json.dumps(
                    {"action": action, "params": params, "echo": echo},
                    ensure_ascii=False,
                )
            )
            data = await asyncio.wait_for(future, timeout=self.cfg.request_timeout)
        except asyncio.TimeoutError as exc:
            raise OneBotError(f"{action} 调用超时") from exc
        finally:
            self._pending.pop(echo, None)

        if data.get("retcode", 0) != 0 or data.get("status") == "failed":
            detail = data.get("message") or data.get("wording") or ""
            raise OneBotError(
                f"{action} 失败：retcode={data.get('retcode')} {str(detail)[:200]}"
            )
        return data.get("data")

    # -- 发送 -------------------------------------------------------------
    async def send_group_text(
        self, group_id: int, text: str, reply_to: str | None = None
    ) -> str | None:
        segments: list[dict[str, Any]] = []
        if reply_to:
            segments.append({"type": "reply", "data": {"id": str(reply_to)}})
        segments.append({"type": "text", "data": {"text": text}})
        data = await self.call("send_group_msg", group_id=group_id, message=segments)
        return _message_id_of(data)

    async def send_private_text(
        self, user_id: int, text: str, reply_to: str | None = None
    ) -> str | None:
        segments: list[dict[str, Any]] = []
        if reply_to:
            segments.append({"type": "reply", "data": {"id": str(reply_to)}})
        segments.append({"type": "text", "data": {"text": text}})
        data = await self.call("send_private_msg", user_id=user_id, message=segments)
        return _message_id_of(data)

    async def send_group_image(
        self, group_id: int, image: bytes, *, as_sticker: bool = False
    ) -> str | None:
        import base64

        encoded = base64.b64encode(image).decode("ascii")
        data: dict[str, Any] = {"file": "base64://" + encoded}
        if as_sticker:
            data["sub_type"] = 1
        result = await self.call(
            "send_group_msg", group_id=group_id,
            message=[{"type": "image", "data": data}],
        )
        return _message_id_of(result)

    async def send_private_image(self, user_id: int, image: bytes) -> str | None:
        """私聊发图（自画像用）。"""
        import base64

        encoded = base64.b64encode(image).decode("ascii")
        data = await self.call(
            "send_private_msg", user_id=user_id,
            message=[{"type": "image", "data": {"file": "base64://" + encoded}}],
        )
        return _message_id_of(data)

    async def get_image(self, file: str) -> dict[str, Any] | None:
        """按 file 取图片信息（NapCat 会把 url/本地路径补回来），识别兜底用。"""
        if not file:
            return None
        try:
            data = await self.call("get_image", file=str(file))
        except Exception:
            log.warning("get_image 失败：%s", str(file)[:80])
            return None
        return data if isinstance(data, dict) else None

    async def get_message(self, message_id: str) -> dict[str, Any] | None:
        """按 message_id 取消息详情（删除图库图片时用来找被引用的图）。"""
        data = await self.call("get_msg", message_id=str(message_id))
        return data if isinstance(data, dict) else None


def _message_id_of(data: Any) -> str | None:
    if isinstance(data, dict) and data.get("message_id") is not None:
        return str(data["message_id"])
    return None
