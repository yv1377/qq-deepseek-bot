"""大肥鱼表情包知识库（dsh-nekowhale-stickers 的 Bot 轻量适配）。

桌面端安装原插件；Bot 侧不跑 Node，直接读同一份 metadata、
按情绪/场景检索，并从 kiriyuki.com 拉取对应 GIF。与普通图片记忆完全独立。
"""
from __future__ import annotations

import asyncio
import json
import random
import re
from pathlib import Path
from urllib.parse import quote

import httpx

SITE = "https://kiriyuki.com"
DEFAULT_META = Path(__file__).resolve().parent.parent / "data/neko/nekowhale.json"
REMOTE_META = f"{SITE}/meme/kit/nekowhale.json"
ATTRIBUTION = (
    "表情包作者：B站 可以叫我云大人（space.bilibili.com/103739），"
    "kiriyuki.com 制作，欢迎约原稿同款表情包！"
)


def _terms(text: str) -> list[str]:
    text = (text or "").lower()
    terms: list[str] = []
    for part in re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", text):
        if len(part) >= 2:
            terms.append(part)
        if re.fullmatch(r"[\u4e00-\u9fff]+", part):
            terms.extend(part[i : i + 2] for i in range(len(part) - 1))
    return list(dict.fromkeys(terms))[:20]


class NekoStickerStore:
    def __init__(self, path: Path | str = DEFAULT_META, *, enabled: bool = True):
        self.path = Path(path)
        self.enabled = enabled
        self.stickers: list[dict] = []
        self._recent: list[str] = []
        self._http: httpx.AsyncClient | None = None
        self._loaded = False

    async def load(self) -> None:
        if self._loaded or not self.enabled:
            return
        data = None
        try:
            data = json.loads(await asyncio.to_thread(self.path.read_text, encoding="utf-8"))
        except (OSError, ValueError):
            try:
                async with httpx.AsyncClient(
                    timeout=20, follow_redirects=True, trust_env=False
                ) as client:
                    response = await client.get(REMOTE_META)
                    response.raise_for_status()
                    data = response.json()
                await asyncio.to_thread(
                    self.path.parent.mkdir, parents=True, exist_ok=True
                )
                await asyncio.to_thread(
                    self.path.write_text,
                    json.dumps(data, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception:
                data = None
        if isinstance(data, dict):
            self.stickers = [
                item for item in (data.get("stickers") or []) if item.get("id")
            ]
        self._loaded = True

    @staticmethod
    def _name(sticker: dict) -> str:
        return str(sticker.get("id") or "").split("/", 1)[-1]

    @staticmethod
    def image_url(sticker: dict) -> str:
        raw_id = str(sticker.get("id") or "")
        pack, _, stem = raw_id.partition("/")
        if not stem:
            stem = raw_id
        return (
            f"{SITE}/media/gif/{quote(pack, safe='')}/"
            f"{quote(stem, safe='')}.gif?h={quote(str(sticker.get('hash') or ''))}"
        )

    def _score(self, sticker: dict, query: str) -> int:
        q = query.strip().lower()
        if not q:
            return 0
        fields = [
            str(sticker.get("id") or ""),
            str(sticker.get("emotion") or ""),
            " ".join(str(x) for x in (sticker.get("tags") or [])),
            str(sticker.get("use") or ""),
            str(sticker.get("desc") or ""),
            str(sticker.get("text") or ""),
            str(sticker.get("meme") or ""),
            str(sticker.get("en") or ""),
            str(sticker.get("dir") or ""),
        ]
        hay = " ".join(fields).lower()
        if q in str(sticker.get("emotion") or "").lower():
            return 12
        if q in str(sticker.get("id") or "").lower():
            return 10
        score = 0
        for term in _terms(q):
            if term in hay:
                score += 2
            if term in str(sticker.get("emotion") or "").lower():
                score += 3
            if term in str(sticker.get("tags") or []):
                score += 3
        return score

    def search(self, query: str, limit: int = 5) -> list[dict]:
        if not self.stickers:
            return []
        scored = [(self._score(item, query), item) for item in self.stickers]
        scored.sort(key=lambda pair: -pair[0])
        top = [item for score, item in scored if score > 0][:limit]
        if not top and query.strip():
            return []
        if not query.strip():
            top = random.sample(self.stickers, min(limit, len(self.stickers)))
        return top

    def pick(self, query: str = "") -> dict | None:
        candidates = self.search(query, limit=8)
        if not candidates and query.strip():
            # 退化为标签包含任一关键词的模糊匹配
            q = query.strip()
            candidates = [
                item for item in self.stickers
                if any(q in str(tag) for tag in (item.get("tags") or []))
            ][:8]
        if not candidates:
            candidates = self.search("", limit=8)
        if not candidates:
            return None
        fresh = [item for item in candidates if item.get("id") not in self._recent]
        chosen = random.choice(fresh or candidates)
        self._recent.append(str(chosen.get("id")))
        self._recent = self._recent[-30:]
        return chosen

    @staticmethod
    def line(sticker: dict) -> str:
        tags = "、".join(str(tag) for tag in (sticker.get("tags") or [])[:5])
        direction = str(sticker.get("dir") or "无")
        return (
            f"{NekoStickerStore._name(sticker)}｜{sticker.get('emotion') or ''}｜"
            f"{tags}｜{direction}"
        )

    async def fetch_image(self, sticker: dict) -> bytes | None:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=25, follow_redirects=True, trust_env=False
            )
        try:
            response = await self._http.get(
                self.image_url(sticker),
                headers={"User-Agent": "Mozilla/5.0 dsh-nekowhale-bot"},
            )
            response.raise_for_status()
            data = response.content or b""
            if len(data) > 5_000_000 or not data.startswith(b"GIF"):
                return None
            return data
        except Exception:
            return None

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
