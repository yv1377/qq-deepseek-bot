"""本群表情包收藏：限量下载、内容去重、本地持久化、群间隔离。"""
from __future__ import annotations

import hashlib
import logging
import random
import json
import re
import time
import asyncio
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from config import MemeConfig, PROJECT_ROOT
from onebot import Incoming

log = logging.getLogger("memes")

# QQ 图片/表情包所属的官方 CDN。只下载这些域名，不访问群友随便贴的网址。
# multimedia.nt.qq.com.cn 是 QQNT 新版图片 CDN；gxh.vip.qq.com 是商城表情原图；
# qpic.cn 及其子域（gchat.qpic.cn / gxh.qpic.cn 等）是老图床。
_QQ_IMAGE_HOSTS = {
    "qpic.cn",
    "multimedia.nt.qq.com",
    "multimedia.nt.qq.com.cn",
    "gxh.vip.qq.com",
}


def is_qq_image_host(host: str) -> bool:
    host = (host or "").lower()
    return host in _QQ_IMAGE_HOSTS or host.endswith(".qpic.cn")


def image_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    return ""


# 隐私图/后台截图：命中关键词就不入库，已有图库也标记/删除
_PRIVACY_RE = re.compile(
    r"(身份证|银行卡|手机号|电话号码|住址|家庭地址|收货地址|快递单|"
    r"二维码|聊天记录|私聊截图|密码|验证码|支付|支付宝|微信|"
    r"定位|车牌|病历|诊断|体检|工资|合同|户口|护照|学生证|"
    r"裸露|色情|开盒|人肉|后台|管理面板|token|密钥)",
    re.I,
)


class MemeStore:
    def __init__(self, cfg: MemeConfig):
        self.cfg = cfg
        self.root = Path(cfg.directory)
        if not self.root.is_absolute():
            self.root = PROJECT_ROOT / self.root
        self.pending_path = self.root / "pending_deletions.json"
        self._last: dict[int, str] = {}
        self.vision = None
        # 视觉识别花费回调：(group_id, usage) -> None，由 ChatService 注入预算统计
        self.on_vision_usage = None
        self._group_locks: dict[int, asyncio.Lock] = {}
        # 已处理过的图片 URL / QQ 文件名：同一张图反复发时不再重复下载、识别、入库
        self._url_cache: OrderedDict[str, None] = OrderedDict()
        self._file_cache: OrderedDict[str, None] = OrderedDict()

    def metadata(self, path: Path) -> dict:
        try:
            return json.loads(path.with_suffix(path.suffix + ".json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    @staticmethod
    def looks_private(text: str) -> bool:
        """识别可能暴露隐私的后台图/截图/证件/聊天记录。"""
        return bool(_PRIVACY_RE.search(text or ""))

    async def save(self, group_id: int, content: bytes, *, source: str = "group",
                   context: str = "", origin: str = "") -> Path | None:
        suffix = image_type(content)
        if not suffix or len(content) > self.cfg.max_bytes:
            return None
        folder = self.root / str(group_id)
        path = folder / (hashlib.sha256(content).hexdigest() + suffix)
        async with self._group_locks.setdefault(group_id, asyncio.Lock()):
            if not path.exists():
                if len(self.items(group_id)) >= self.cfg.max_per_group:
                    return None
                folder.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        meta = self.metadata(path)
        if meta.get("description"):
            if self.looks_private(str(meta.get("description") or "")):
                log.warning("图库发现隐私图，自动删除：群 %s 文件 %s", group_id, path.name)
                self.delete_by_name(group_id, path.name)
                return None
            return path
        description = ""
        if self.vision is not None and getattr(self.vision, "enabled", False):
            try:
                hook = self.on_vision_usage
                if hook is not None:
                    description = await self.vision.describe_bytes(
                        content,
                        on_usage=lambda usage, gid=group_id: hook(gid, usage),
                    )
                else:
                    description = await self.vision.describe_bytes(content)
            except Exception:
                log.warning("图库图片识别失败：群 %s", group_id)
        if self.looks_private(description + " " + context):
            log.warning("图库拒绝隐私图：群 %s（%s）", group_id, description[:40])
            try:
                if path.exists():
                    self.delete_by_name(group_id, path.name)
            except OSError:
                pass
            return None
        meta.update(source=source, context=context[:300], origin=origin,
                    description=description, animated=suffix == ".gif")
        path.with_suffix(path.suffix + ".json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return path

    def items(self, group_id: int) -> list[dict]:
        result = []
        for path in (self.root / str(group_id)).glob("*.*"):
            if path.suffix.lower() in {".png", ".jpg", ".gif", ".webp"} and path.is_file() and not path.is_symlink():
                result.append({**self.metadata(path), "name": path.name, "group_id": group_id})
        return result

    # -- 删除 -------------------------------------------------------------
    def _folder(self, group_id: int) -> Path:
        return self.root / str(max(0, int(group_id)))

    def _safe_member(self, group_id: int, name: str) -> Path | None:
        """只允许删本群图库目录下的图片文件，防目录穿越。"""
        folder = self._folder(group_id)
        path = folder / name
        try:
            if not path.is_file() or path.is_symlink():
                return None
            if path.resolve().parent != folder.resolve():
                return None
        except OSError:
            return None
        if path.suffix.lower() not in {".png", ".jpg", ".gif", ".webp"}:
            return None
        return path

    def delete_by_name(self, group_id: int, name: str) -> bool:
        """按文件名删除（WebUI 用）。图片和旁边的 JSON 一起删。"""
        path = self._safe_member(group_id, name)
        if path is None:
            return False
        meta = path.with_suffix(path.suffix + ".json")
        try:
            path.unlink()
        except OSError:
            return False
        try:
            if meta.is_file():
                meta.unlink()
        except OSError:
            pass
        if self._last.get(group_id) == path.name:
            self._last.pop(group_id, None)
        return True

    def delete_by_content(self, group_id: int, content: bytes) -> Path | None:
        """按内容哈希删除（群里回复图片删除用）：同图只存了一份，删它即可。"""
        suffix = image_type(content)
        if not suffix:
            return None
        digest = hashlib.sha256(content).hexdigest()
        folder = self._folder(group_id)
        path = folder / (digest + suffix)
        if not path.is_file():
            candidates = [
                p for p in folder.glob(digest + ".*")
                if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".gif", ".webp"}
            ]
            path = candidates[0] if candidates else None
        if path is None or not self.delete_by_name(group_id, path.name):
            return None
        return path

    # -- 待删除标记（主人说删图时先标记，不立即删） ----------------------
    def _load_pending(self) -> list[dict]:
        try:
            data = json.loads(self.pending_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save_pending(self, items: list[dict]) -> None:
        self.pending_path.parent.mkdir(parents=True, exist_ok=True)
        self.pending_path.write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def mark_by_name(
        self, group_id: int, name: str, reason: str = "", requester: int = 0
    ) -> bool:
        """把一个表情包标记为待删除；真正删除要主人之后确认。"""
        if self._safe_member(group_id, name) is None:
            return False
        items = self._load_pending()
        if any(int(item.get("group_id", 0)) == int(group_id) and item.get("name") == name
               for item in items):
            return True
        items.append({
            "group_id": int(group_id),
            "name": name,
            "reason": str(reason or "")[:120],
            "requester": int(requester or 0),
            "created_at": time.time(),
        })
        self._save_pending(items)
        log.info("表情包已标记待删除：群 %s %s（申请人 %s）", group_id, name, requester)
        return True

    def mark_last(self, group_id: int, reason: str = "", requester: int = 0) -> str | None:
        name = self._last.get(group_id)
        if not name:
            return None
        return name if self.mark_by_name(group_id, name, reason, requester) else None

    async def mark_urls(
        self, group_id: int, urls: list[str], reason: str = "",
        requester: int = 0, limit: int = 2,
    ) -> tuple[int, int, int]:
        """下载目标图片并按哈希标记待删。返回（标记数，图库没有，失败数）。"""
        marked = missing = failed = 0
        async with httpx.AsyncClient(
            timeout=15, follow_redirects=False, trust_env=False
        ) as client:
            for url in urls[:limit]:
                try:
                    parsed = urlsplit(url)
                    if parsed.scheme not in {"http", "https"} or not is_qq_image_host(
                        parsed.hostname or ""
                    ):
                        failed += 1
                        continue
                    response = await client.get(
                        url,
                        headers={
                            "User-Agent": (
                                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                            )
                        },
                    )
                    content = response.content or b""
                    if (
                        response.status_code != 200
                        or not content
                        or len(content) > self.cfg.max_bytes * 2
                        or not image_type(content)
                    ):
                        failed += 1
                        continue
                    suffix = image_type(content)
                    digest = hashlib.sha256(content).hexdigest()
                    folder = self._folder(group_id)
                    candidates = [
                        p for p in folder.glob(digest + ".*")
                        if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".gif", ".webp"}
                    ]
                    if not candidates:
                        missing += 1
                        continue
                    if self.mark_by_name(
                        group_id, candidates[0].name, reason, requester
                    ):
                        marked += 1
                    else:
                        failed += 1
                except Exception:
                    failed += 1
        return marked, missing, failed

    def pending_deletions(self) -> list[dict]:
        return self._load_pending()

    def purge_pending(self) -> int:
        """主人确认后真正删除所有待删表情包；返回删除数量。"""
        items = self._load_pending()
        removed = 0
        remaining: list[dict] = []
        for item in items:
            group_id = int(item.get("group_id", 0))
            name = str(item.get("name") or "")
            if self.delete_by_name(group_id, name):
                removed += 1
            else:
                remaining.append(item)
        self._save_pending(remaining)
        log.info("待删表情包清理完成：删除 %s 张，剩余 %s 张", removed, len(remaining))
        return removed


    def delete_last(self, group_id: int) -> str | None:
        """删除这个群最近一次发出去的表情包（“删掉刚才那张”）。"""
        name = self._last.get(group_id)
        if not name:
            return None
        return name if self.delete_by_name(group_id, name) else None

    async def delete_urls(
        self, group_id: int, urls: list[str], limit: int = 2
    ) -> tuple[int, int, int]:
        """下载目标图片并按哈希从图库删除。返回（删除数，图库没有，失败数）。"""
        deleted = missing = failed = 0
        async with httpx.AsyncClient(
            timeout=15, follow_redirects=False, trust_env=False
        ) as client:
            for url in urls[:limit]:
                try:
                    parsed = urlsplit(url)
                    if parsed.scheme not in {"http", "https"} or not is_qq_image_host(
                        parsed.hostname or ""
                    ):
                        failed += 1
                        continue
                    response = await client.get(
                        url,
                        headers={
                            "User-Agent": (
                                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                            )
                        },
                    )
                    content = response.content or b""
                    if (
                        response.status_code != 200
                        or not content
                        or len(content) > self.cfg.max_bytes * 2
                        or not image_type(content)
                    ):
                        failed += 1
                        continue
                    if self.delete_by_content(group_id, content) is not None:
                        deleted += 1
                    else:
                        missing += 1
                except Exception:
                    failed += 1
        return deleted, missing, failed

    def candidates(self, incoming: Incoming) -> list[str]:
        result = []
        segments = incoming.raw.get("message") or []
        if not isinstance(segments, list):
            return result
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            data = segment.get("data") or {}
            if not isinstance(data, dict):
                continue
            kind = segment.get("type")
            # 默认只收「表情/表情包/动图表情」，普通照片不再一股脑入库；
            # collect_all_images=true 时才连普通图片一起收。
            if kind != "mface" and not (
                kind == "image"
                and (
                    self.cfg.collect_all_images
                    or str(data.get("sub_type")) in {"1", "7"}
                    or "表情" in str(data.get("summary") or "")
                )
            ):
                continue
            url = str(data.get("url") or "")
            try:
                parsed = urlsplit(url)
                host = (parsed.hostname or "").lower()
                port = parsed.port
            except ValueError:
                continue
            # 仅下载 QQ 图片 CDN；不跟随重定向，不访问任意群友提供的网址。
            if parsed.scheme in {"http", "https"} and is_qq_image_host(host or "") and port in {None, 80, 443}:
                result.append(url)
        return list(dict.fromkeys(result))[:2]

    async def collect(self, incoming: Incoming) -> list[str]:
        descriptions = []
        if incoming.group_id is None:
            return descriptions
        folder = self.root / str(incoming.group_id)
        async with httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=False) as client:
            for url in self.candidates(incoming):
                if len(self.items(incoming.group_id)) >= self.cfg.max_per_group:
                    return descriptions
                url_key, file_key = self._source_keys(incoming, url)
                # 同一张图（同 URL 或同 QQ 文件名）只处理一次，避免重复下载和识别
                if url_key in self._url_cache or (
                    file_key and file_key in self._file_cache
                ):
                    continue
                try:
                    content = bytearray()
                    async with client.stream("GET", url) as response:
                        response.raise_for_status()
                        async for block in response.aiter_bytes():
                            content.extend(block)
                            if len(content) > self.cfg.max_bytes:
                                raise ValueError("图片超过大小限制")
                    path = await self.save(incoming.group_id, bytes(content), context=incoming.text)
                    if path:
                        self._remember_source(url_key, file_key)
                        meta = self.metadata(path)
                        if meta.get("description"):
                            descriptions.append(meta["description"])
                except Exception:
                    log.warning("表情包收集失败：群 %s", incoming.group_id)
        return descriptions

    def _source_keys(self, incoming: Incoming, url: str) -> tuple[str, str]:
        """返回（图片 URL, 群号+QQ 文件名），用于同图去重。"""
        file = ""
        for segment in incoming.raw.get("message") or []:
            if not isinstance(segment, dict):
                continue
            data = segment.get("data") or {}
            if isinstance(data, dict) and str(data.get("url") or "") == url:
                file = str(data.get("file") or "")
                break
        file_key = f"{incoming.group_id}:{file}" if file else ""
        return url, file_key

    def _remember_source(self, url: str, file_key: str) -> None:
        for cache, key in ((self._url_cache, url), (self._file_cache, file_key)):
            if not key:
                continue
            cache[key] = None
            cache.move_to_end(key)
            while len(cache) > 800:
                cache.popitem(last=False)

    def choose(
        self,
        group_id: int,
        context: str = "",
        *,
        any_if_no_match: bool = False,
        force: bool = False,
        chance: float | None = None,
    ) -> bytes | None:
        """按概率选一张本群表情包。

        context 命中识别描述/标签时优先发相关的那张；
        any_if_no_match=True 时，没匹配到就随机发一张（否则不发）；
        force=True 时跳过概率（群友明确要表情/图片时用）；
        chance 不为 None 时用调用方给的概率（支持按群单独设置）。
        """
        effective_chance = self.cfg.chance if chance is None else chance
        if not force and random.random() >= effective_chance:
            return None
        folder = self.root / str(group_id)
        paths = [p for p in folder.glob("*.*")
                 if p.suffix.lower() in {".png", ".jpg", ".gif", ".webp"}
                 and p.is_file() and not p.is_symlink()
                 and p.stat().st_size <= self.cfg.max_bytes]
        if not paths:
            return None
        if context:
            def keywords(text):
                words = set(re.findall(r"[a-zA-Z]{3,}", text.lower()))
                for part in re.findall(r"[\u4e00-\u9fff]+", text):
                    words.update(part[i:i+2] for i in range(len(part)-1))
                return words
            query = keywords(context)
            scores = {}
            for candidate in paths:
                meta = self.metadata(candidate)
                # 只使用识别描述或人工标签配图，不把群友原话当作视觉识别结果。
                text = str(meta.get("description") or "") + " " + str(meta.get("tags") or "")
                scores[candidate] = len(query & keywords(text))
            best = max(scores.values(), default=0)
            if best:
                paths = [p for p in paths if scores[p] == best]
            elif not any_if_no_match:
                return None
            # best == 0 且允许兜底：保留全部图片，随机选一张
        alternatives = [p for p in paths if p.name != self._last.get(group_id)]
        if alternatives:
            paths = alternatives
        path = random.choice(paths)
        data = path.read_bytes()
        if not image_type(data):
            return None
        self._last[group_id] = path.name
        return data
