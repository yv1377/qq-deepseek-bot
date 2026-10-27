"""Phase 3.3：图片理解。

流程：QQ 图片 URL → 下载 → 转 data URI → 调 OpenAI 兼容的视觉模型 → 一句话描述。
描述会被拼进当前用户消息，DeepSeek 看到的就不是「[图片]」而是「[图片内容：一只橘猫…]」。

DeepSeek-V4.1-Flash（deepseek-flash）已支持图片输入，默认复用 DEEPSEEK_API_KEY；
也仍可指到其他 OpenAI 兼容的视觉服务。GIF/动态 WebP 会均匀抽最多四帧，
按时间顺序一起交给模型，让它理解「动作变化」；发送和图库仍保留原动图。

注意：默认关闭思考（thinking disabled）。深度思考会先消耗大量推理 token，
把 max_tokens 占满后正文为空，图片描述就丢了。

未配置、下载失败或模型报错时都返回空字符串，上层自动降级为 [图片] 占位。
"""
from __future__ import annotations

import base64
import io
import logging
from collections import OrderedDict
from typing import Any, Callable

import httpx
from PIL import Image

from config import VisionConfig

log = logging.getLogger("vision")

# 动图最多抽几帧交给模型
_MAX_FRAMES = 4

_MIME_BY_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


class VisionError(RuntimeError):
    """图片下载或视觉模型调用失败。"""


class VisionClient:
    def __init__(
        self, cfg: VisionConfig, http: httpx.AsyncClient | None = None
    ):
        self.cfg = cfg
        self.enabled = cfg.configured
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(cfg.timeout, connect=10.0)
        )
        # 图片 URL -> 描述，按 LRU 限制条数，避免同一张图反复花钱
        self._cache: OrderedDict[str, str] = OrderedDict()
        # 某些 OpenAI 兼容服务不认 DeepSeek 的 thinking 字段；遇到 400 后记住并降级
        self._thinking_supported = True
        if not self.enabled:
            log.info("图片理解未启用（配置 vision.enabled / base_url / model 后生效）")
        elif not cfg.api_key:
            log.info("图片理解未配置 API Key，若为本地模型可忽略")

    async def describe(
        self,
        urls: list[str],
        *,
        on_usage: Callable[[dict[str, Any]], None] | None = None,
    ) -> str:
        """把最多 max_images 张图片描述成文本；任何一张失败都跳过。"""
        if not self.enabled or not urls:
            return ""
        descriptions: list[str] = []
        for url in urls[: self.cfg.max_images]:
            cached = self._cache.get(url)
            if cached is not None:
                descriptions.append(cached)
                continue
            try:
                text = await self._describe_one(url, on_usage=on_usage)
            except Exception as exc:  # 图片只是增强，失败不能影响聊天
                log.warning("图片理解失败（%s）：%s", _short(url), exc)
                continue
            if text:
                descriptions.append(text)
                self._remember(url, text)
            else:
                log.warning("图片理解返回空描述（%s）", _short(url))
        return "\n".join(descriptions)

    async def _describe_one(
        self,
        url: str,
        *,
        on_usage: Callable[[dict[str, Any]], None] | None = None,
    ) -> str:
        if url.startswith("data:image/"):
            header, _, payload = url.partition(",")
            try:
                data = base64.b64decode(payload, validate=False)
            except (ValueError, TypeError) as exc:
                raise VisionError("data URI 不是合法 base64") from exc
            mime = header[5:].split(";")[0].strip() or "image/jpeg"
        else:
            data, mime = await self._download(url)
        if not data:
            raise VisionError("图片内容为空")
        if _is_animated(data):
            return await self.describe_bytes(data, on_usage=on_usage)
        return await self._describe_uris(
            [_data_uri(data, _sniff_mime(data) or mime)], on_usage=on_usage
        )

    async def describe_bytes(
        self,
        data: bytes,
        *,
        on_usage: Callable[[dict[str, Any]], None] | None = None,
    ) -> str:
        """动图（GIF / 动态 WebP / APNG）均匀抽最多四帧；保留原图供图库和发送使用。"""
        if not self.enabled:
            return ""
        if not data:
            return ""
        if len(data) > self.cfg.max_image_bytes:
            raise VisionError(
                f"图片过大（{len(data) / 1024 / 1024:.1f} MB > "
                f"{self.cfg.max_image_bytes / 1024 / 1024:.1f} MB）"
            )
        uris: list[str] = []
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 40_000_000:
                raise VisionError("图片像素过大")
            frames = int(getattr(image, "n_frames", 1) or 1)
            indexes = sorted({round(i * (frames - 1) / (_MAX_FRAMES - 1)) for i in range(_MAX_FRAMES)})
            for index in indexes:
                image.seek(index)
                frame = image.convert("RGB")
                frame.thumbnail((768, 768))
                buffer = io.BytesIO()
                frame.save(buffer, format="JPEG", quality=80)
                uris.append(
                    "data:image/jpeg;base64,"
                    + base64.b64encode(buffer.getvalue()).decode("ascii")
                )
        if not uris:
            raise VisionError("动图抽帧失败")
        return await self._describe_uris(uris, on_usage=on_usage)

    async def _describe_uris(
        self,
        uris: list[str],
        *,
        on_usage: Callable[[dict[str, Any]], None] | None = None,
    ) -> str:
        instruction = str(self.cfg.prompt)
        if len(uris) > 1:
            instruction += " 以上是同一张动图的连续多帧，按时间顺序概括动作变化。"
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": instruction},
                        *[
                            {"type": "image_url", "image_url": {"url": uri}}
                            for uri in uris
                        ],
                    ],
                }
            ],
            "temperature": 0.2,
            "max_tokens": self.cfg.max_tokens,
            "stream": False,
        }
        if self.cfg.disable_thinking and self._thinking_supported:
            payload["thinking"] = {"type": "disabled"}
        headers = (
            {"Authorization": f"Bearer {self.cfg.api_key}"}
            if self.cfg.api_key
            else {}
        )
        endpoint = f"{self.cfg.base_url.rstrip('/')}/chat/completions"
        response = await self._post(endpoint, payload, headers)
        if (
            response.status_code == 400
            and "thinking" in payload
            and "thinking" in response.text.lower()
        ):
            # 换成非 DeepSeek 的兼容服务时可能不认 thinking 字段：去掉重试一次
            self._thinking_supported = False
            payload.pop("thinking", None)
            response = await self._post(endpoint, payload, headers)
        if response.status_code != 200:
            raise VisionError(
                f"HTTP {response.status_code}: {response.text[:200]}"
            )
        data = response.json()
        usage = data.get("usage") or {}
        if on_usage is not None and isinstance(usage, dict) and usage:
            try:
                on_usage(usage)
            except Exception:  # 预算统计失败不能影响识别
                log.exception("视觉用量回调失败")
        try:
            choice = data["choices"][0]
            content = choice["message"].get("content")
        except (KeyError, IndexError, TypeError) as exc:
            raise VisionError(f"返回格式异常：{str(data)[:200]}") from exc
        text = _content_to_text(content).strip()
        if not text:
            reasoning = choice.get("message", {}).get("reasoning_content") or ""
            finish = choice.get("finish_reason")
            log.warning(
                "视觉模型没有返回正文（finish_reason=%s，reasoning %s 字）；"
                "如果是思考模型，请保持 vision.disable_thinking: true 或调大 max_tokens",
                finish,
                len(reasoning),
            )
        return text

    async def _post(
        self,
        endpoint: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> httpx.Response:
        try:
            return await self._http.post(endpoint, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise VisionError(f"视觉模型请求失败：{exc}") from exc

    async def _download(self, url: str) -> tuple[bytes, str]:
        if not url.startswith(("http://", "https://")):
            raise VisionError("不是可下载的图片 URL")
        try:
            response = await self._http.get(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                    )
                },
            )
        except httpx.HTTPError as exc:
            raise VisionError(f"下载图片失败：{exc}") from exc
        if response.status_code != 200:
            raise VisionError(f"下载图片失败：HTTP {response.status_code}")
        content = response.content or b""
        if not content:
            raise VisionError("图片内容为空")
        if len(content) > self.cfg.max_image_bytes:
            raise VisionError(
                f"图片过大（{len(content) / 1024 / 1024:.1f} MB > "
                f"{self.cfg.max_image_bytes / 1024 / 1024:.1f} MB）"
            )
        header_type = (
            response.headers.get("content-type", "").split(";")[0].strip().lower()
        )
        mime = _sniff_mime(content) or (
            header_type if header_type.startswith("image/") else ""
        )
        if not mime:
            raise VisionError(f"不是图片类型：{header_type or '未知'}")
        return content, mime

    def _remember(self, url: str, text: str) -> None:
        if self.cfg.cache_size <= 0:
            return
        self._cache[url] = text
        self._cache.move_to_end(url)
        while len(self._cache) > self.cfg.cache_size:
            self._cache.popitem(last=False)

    async def aclose(self) -> None:
        await self._http.aclose()


def _sniff_mime(data: bytes) -> str:
    """按文件头判断真实图片类型，避免 CDN 的 content-type 不准。"""
    for magic, mime in _MIME_BY_MAGIC:
        if data.startswith(magic):
            return mime
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    return ""


def _is_animated(data: bytes) -> bool:
    """GIF / 动态 WebP / APNG 返回 True（需要抽帧）；普通静态图返回 False。"""
    if data.startswith((b"GIF87a", b"GIF89a")):
        return True
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        try:
            with Image.open(io.BytesIO(data)) as image:
                return int(getattr(image, "n_frames", 1) or 1) > 1
        except Exception:  # 坏图交给静态分支，由视觉模型或降级处理
            return False
    # APNG：PNG 里带 acTL 分块
    if data.startswith(b"\x89PNG\r\n\x1a\n") and b"acTL" in data[:1024]:
        return True
    return False


def _data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime or 'image/jpeg'};base64," + base64.b64encode(data).decode("ascii")


def _content_to_text(content: Any) -> str:
    """兼容 content 为字符串或内容块数组两种返回格式。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in {"text", "output_text"}:
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    return ""


def _short(url: str, limit: int = 80) -> str:
    return url if len(url) <= limit else url[: limit - 3] + "..."
